"""The per-person assistants (enrich, normalize) and their runs, end to end against a fake
Firecrawl and fake models, plus the ask assistant.

The fakes count every call, so these tests pin down the credit discipline: what runs, what is
skipped and why, and that nothing is ever overwritten.
"""

import dataclasses
import json
from contextlib import ExitStack
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic_ai import ModelMessage, ModelResponse
from pydantic_ai.messages import ModelRequest, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app import db as db_pg
from app import embedder
from app import people as people_mod
from app.agents import fakes
from app.assistants import normalize as normalize_assistant
from app.assistants import runner
from app.assistants.enrich import assistant as enrich_workflow
from app.assistants.enrich import budget as enrich_budget
from app.assistants.enrich.providers import PROVIDERS
from app.history import Source
from app.integrations.firecrawl import (
    SCRAPE_CREDITS,
    SEARCH_CREDITS,
    AlexandriaResult,
    AsyncFirecrawl,
    FirecrawlError,
    NotFound,
    ScrapeResult,
    SearchHit,
)
from app.mongo import db
from app.tools import company, site, store
from app.urls import url_key
from tests.conftest import import_people

FIX = Path(__file__).parent / "fixtures" / "firecrawl"
LINKEDIN_CREDITS = 5    # fullenrich/people/match
PDL_CREDITS = 1         # people-data-labs/person/enrich
GITHUB_CREDITS = 5      # github-com/repositories/user

PEOPLE = """Name,Company,Location,LinkedIn,Website,Bio
Ada Okafor,PayRail,"Lagos, Nigeria",https://www.linkedin.com/in/adaokafor?,www.adaokafor.dev,Builds payments infrastructure
Bo Chen,Leafy,Berlin,https://linkedin.com/in/bochen,https://bochen.me,Climate founder
Cy Diaz,,,,,Doctor turned investor
Dee Evans,,,https://www.linkedin.com/in/exchange-fail,,Operator
"""

EXPECTED_PASSAGE = """\
# Facts from data providers; each line names the property it belongs to.
# LinkedIn profile (fullenrich/people/match)
about: Founder of PayRail. Previously Paystack and Stripe.
role: CEO & Co-founder
experience: CEO & Co-founder at PayRail (2021–present); Product Lead at Paystack (2018–2021)
education: BSc Computer Science, University of Lagos
skills: Payments; Fintech
languages: English; Yoruba
# Personal site https://adaokafor.dev (site_reader)
one_liner: Builds payments infrastructure."""


def written(result: dict) -> dict:
    """Property → value an item wrote (set or append changes)."""
    return {c["key"]: c["value"] for c in result.get("changes", [])
            if c["action"] in ("set", "append")}


PAGES = {
    "https://adaokafor.dev": ("site_ada.md", SCRAPE_CREDITS, None,
                              ["https://github.com/adaok", "https://adaokafor.dev/notes",
                               "https://rails.substack.com", "https://twitter.com/adaokafor"]),
    "https://payrail.io": ("company_payrail.md", SCRAPE_CREDITS, None,
                           ["https://payrail.io/careers", "https://docs.payrail.io"]),
    "https://bochen.me": ("site_ada.md", SCRAPE_CREDITS, None, []),
}


class FakeFirecrawl(AsyncFirecrawl):
    calls: ClassVar[list[tuple[str, str]]] = []

    async def scrape(self, url, formats=("markdown",), only_main_content=True,
                     expected_credits=SCRAPE_CREDITS, timeout_ms=60_000):
        self.budget.check_scrape(expected_credits)
        FakeFirecrawl.calls.append(("scrape", url))
        match = next((k for k in PAGES if url_key(k) == url_key(url)), None)
        if "exchange-fail" in url:
            raise FirecrawlError("Firecrawl HTTP 500: All scraping engines failed to retrieve "
                                 "content from this URL. Engines tried: [exchange].")
        if match is None:
            raise NotFound(f"no fixture for {url}")
        name, credits, provider, links = PAGES[match]
        self.budget.charge("scrape", url, credits)
        return ScrapeResult(url=url, markdown=(FIX / name).read_text(), links=links,
                            status_code=200, title=name, credits=credits, provider=provider)

    async def alexandria(self, provider, capability, options, expected_credits):
        self.budget.check_scrape(expected_credits)
        target = f"{provider}/{capability}"
        FakeFirecrawl.calls.append((target, json.dumps(options, sort_keys=True)))
        if target == "fullenrich/people/match":
            url = options["linkedin_url"]
            if "exchange-fail" in url:
                raise FirecrawlError("fullenrich/people/match: upstream_error provider timeout")
            if url_key(url) in (url_key("https://www.linkedin.com/in/adaokafor"),
                                url_key("https://www.linkedin.com/in/nosummary"),
                                url_key("https://www.linkedin.com/in/nolastname")):
                self.budget.charge("scrape", target, LINKEDIN_CREDITS)
                return AlexandriaResult(provider, capability,
                                        json.loads((FIX / "fullenrich_ada.json").read_text()),
                                        LINKEDIN_CREDITS)
            if url_key(url) == url_key("https://www.linkedin.com/in/nameless"):
                # a paid match whose record carries no name at all
                self.budget.charge("scrape", target, LINKEDIN_CREDITS)
                data = json.loads((FIX / "fullenrich_ada.json").read_text())
                data["people"][0].update(full_name=None, first_name=None, last_name=None)
                return AlexandriaResult(provider, capability, data, LINKEDIN_CREDITS)
            return AlexandriaResult(provider, capability, {"people": []}, 0)
        if target == "people-data-labs/person/enrich":
            assert options["min_likelihood"] == 8
            if url_key(options["profile"]) == url_key("https://www.linkedin.com/in/adaokafor"):
                self.budget.charge("scrape", target, PDL_CREDITS)
                return AlexandriaResult(provider, capability,
                                        json.loads((FIX / "pdl_ada.json").read_text()), PDL_CREDITS)
            if url_key(options["profile"]) == url_key("https://www.linkedin.com/in/nosummary"):
                self.budget.charge("scrape", target, PDL_CREDITS)   # a record without an about text
                data = json.loads((FIX / "pdl_ada.json").read_text())
                data["data"]["summary"] = None
                return AlexandriaResult(provider, capability, data, PDL_CREDITS)
            if url_key(options["profile"]) == url_key("https://www.linkedin.com/in/nolastname"):
                self.budget.charge("scrape", target, PDL_CREDITS)   # PDL knows only the first name
                data = json.loads((FIX / "pdl_ada.json").read_text())
                data["data"].update(full_name="ada", last_name=None)
                return AlexandriaResult(provider, capability, data, PDL_CREDITS)
            raise NotFound("not_found")
        if target == "github-com/repositories/user":
            if url_key(options["url"]) == url_key("https://github.com/adaok"):
                self.budget.charge("scrape", target, GITHUB_CREDITS)
                return AlexandriaResult(provider, capability,
                                        json.loads((FIX / "github_adaok.json").read_text()),
                                        GITHUB_CREDITS)
            raise NotFound("not_found")
        raise FirecrawlError(f"no fake for {target}")

    async def search(self, query, limit=5):
        self.budget.check_search()
        FakeFirecrawl.calls.append(("search", query))
        self.budget.charge("search", query, SEARCH_CREDITS)
        if "payrail" in query.lower():
            return [SearchHit("https://payrail.io", "PayRail", "payments infrastructure")]
        return []


# --- fake models: deterministic structured outputs, exercising the agents' tools -------------------

model_calls: dict[str, int] = {}


def _prompt(messages: list[ModelMessage]) -> str:
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart):
                    return str(p.content)
    return ""


def _tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [p for m in messages if isinstance(m, ModelRequest) for p in m.parts
            if isinstance(p, ToolReturnPart)]


def _out(info: AgentInfo, data: dict) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, data)])


def site_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    model_calls["site"] = model_calls.get("site", 0) + 1
    prompt = _prompt(messages)
    personal = "Site: https://adaokafor.dev" in prompt
    return _out(info, {
        "is_personal": personal,
        "github_urls": ["https://github.com/adaok"] if personal else [],
        "related_links": [{"url": "https://rails.substack.com", "kind": "newsletter"}]
        if personal else [],
        "one_liner": "Builds payments infrastructure." if personal else None})


def company_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Fetch the candidate site if given, else search once (when allowed), then decide."""
    model_calls["company"] = model_calls.get("company", 0) + 1
    prompt = _prompt(messages)
    returns = _tool_returns(messages)
    fetched = [r for r in returns if r.tool_name == "fetch_page"]
    if not fetched:
        if "Candidate website: none known" not in prompt:
            url = prompt.split("Candidate website: ")[1].split("\n")[0]
            return ModelResponse(parts=[ToolCallPart("fetch_page", {"url": url})])
        searched = [r for r in returns if r.tool_name == "web_search"]
        if "Web search available: yes" in prompt and not searched:
            company_name = prompt.split("Company: ")[1].split("\n")[0]
            return ModelResponse(parts=[ToolCallPart("web_search",
                                                     {"query": f"{company_name} official website"})])
        if searched and "payrail.io" in str(searched[0].content):
            return ModelResponse(parts=[ToolCallPart("fetch_page", {"url": "https://payrail.io"})])
        return _out(info, {"website": None, "summary": None, "confidence": "low",
                           "evidence_url": None})
    page = str(fetched[0].content)
    if "PayRail" in page:
        return _out(info, {"website": "https://payrail.io",
                           "summary": "PayRail provides payments infrastructure for merchants in "
                                      "West Africa, with next-day local settlement.",
                           "confidence": "high", "evidence_url": "https://payrail.io"})
    return _out(info, {"website": None, "summary": None, "confidence": "low",
                       "evidence_url": None})


def storage_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """The shared fake storage agent, counted."""
    model_calls["storage"] = model_calls.get("storage", 0) + 1
    return fakes.storage(messages, info)


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    FakeFirecrawl.calls = []
    model_calls.clear()
    monkeypatch.setattr(enrich_workflow, "AsyncFirecrawl",
                        lambda budget: FakeFirecrawl(api_key="fake", budget=budget))
    monkeypatch.setattr(normalize_assistant, "AsyncFirecrawl",
                        lambda budget: FakeFirecrawl(api_key="fake", budget=budget))
    with ExitStack() as stack:
        from app.tools import normalize as normalize_tool
        from app.tools import repair as repair_tool
        stack.enter_context(repair_tool.agent.override(model=fakes.model_for(repair_tool.agent)))
        stack.enter_context(normalize_tool.agent.override(model=fakes.model_for(normalize_tool.agent)))
        stack.enter_context(site.agent.override(model=FunctionModel(site_model)))
        stack.enter_context(company.agent.override(model=FunctionModel(company_model)))
        stack.enter_context(store.agent.override(model=FunctionModel(storage_model)))
        yield


@pytest.fixture
async def people(writer):
    await import_people(writer, PEOPLE, "p.csv", "member")
    return {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}


async def run_for(client, ids, **options):
    options = {"providers": ["fullenrich"], **options}      # the older tests describe FullEnrich
    r = await client.post("/api/assistants/enrich/runs", json={"person_ids": ids,
                                                              "options": options})
    assert r.status_code == 201, r.text
    await runner.drain()
    return (await client.get(f"/api/assistants/runs/{r.json()['id']}")).json()




async def test_full_ladder_writes_history_entries(writer, reader, people):
    run = await run_for(writer, [people["Ada Okafor"]])
    assert run["status"] == "done" and run["done"] == 1 and run["failed"] == 0, run
    item = run["items"][0]
    assert item["status"] == "done", item
    res = item["result"]
    assert res["linkedin"] == "matched" and res["linkedin_provider"] == "fullenrich"
    assert res["company"] == "resolved" and res["github"] == "matched"
    assert res["storage"] == "stored" and res["passage"] == EXPECTED_PASSAGE
    assert written(res) == {
        "linkedin_headline": "Building payments infrastructure for African merchants · CEO at PayRail",
        "github": "https://github.com/adaok",
        "github_summary": "Ada Okafor — payments rails. 6 public repos, 120 followers, at PayRail. "
                          "Top repos: rails-sdk (★48), settle (★12). Languages: Python, Go.",
        "related_links": ["https://twitter.com/adaokafor", "https://rails.substack.com"],
        "company_website": "https://payrail.io",
        # the employer's own description from the paid record: no page fetched, no model call
        "company_description": "Payments infrastructure for merchants across West Africa.",
        # the provider record and the site's one-liner, filed by the storage agent
        "about": "Founder of PayRail. Previously Paystack and Stripe.",
        "role": "CEO & Co-founder",
        "experience": "CEO & Co-founder at PayRail (2021–present); Product Lead at Paystack "
                      "(2018–2021)",
        "education": "BSc Computer Science, University of Lagos",      # FullEnrich dates no degrees
        "skills": ["Payments", "Fintech"],
        "languages": ["English", "Yoruba"],
        "one_liner": "Builds payments infrastructure.",
        "enrichment_passage": EXPECTED_PASSAGE,
        "record_kind": "Person",          # the closing review classifies the record
    }
    # Her own site, location and company are already on the record: not re-added; the provider
    # dump never reaches notes.
    assert not {"location", "company", "notes"} & set(written(res))
    assert res["stored"]["skills"] == {"provider": "fullenrich/people/match",
                                       "evidence": "skills: Payments; Fintech"}
    assert res["stored"]["one_liner"]["provider"] == "site_reader"
    assert res["providers"] == {"fullenrich": "matched"} and res.get("dropped") is None
    assert item["credits"] == LINKEDIN_CREDITS + GITHUB_CREDITS + SCRAPE_CREDITS   # her site only
    assert item["credits"] == run["credits_used"]
    assert [c for c in FakeFirecrawl.calls if c[0] == "search"] == []
    assert model_calls == {"site": 1, "storage": 1}   # none for LinkedIn, GitHub or the company
    assert "facts filed through the storage agent" in res["text"]
    assert "enrichment_passage" not in res["text"]

    person = (await reader.get(f"/api/people/{people['Ada Okafor']}")).json()
    assert person["github"] == "https://github.com/adaok"
    src = person["history"]["github"][0]["src"]
    assert src == {"kind": "agent", "by": "enrich/linkedin_reader",
                   "ref": f"run:{run['id']}/item:{item['id']}"}
    assert person["history"]["company_description"][0]["src"]["by"] == "enrich/linkedin_reader"
    assert person["history"]["github_summary"][0]["src"]["by"] == "enrich/github"
    assert person["history"]["about"][0]["src"]["by"] == "enrich/storage"
    assert person["history"]["enrichment_passage"][0]["src"]["by"] == "enrich/storage"
    assert person["enrichment_passage"] == EXPECTED_PASSAGE
    assert [e["v"] for e in person["history"]["website"]] == ["www.adaokafor.dev"]
    assert person["company_website"] == "https://payrail.io"
    assert len(person["history"]["company"]) == 1
    fl = {f["key"]: f for f in (await reader.get("/api/properties")).json()}
    assert fl["company_description"]["embeddable"] and fl["github_summary"]["embeddable"]
    assert not fl["github"]["embeddable"] and not fl["related_links"]["embeddable"]
    assert fl["about"]["embeddable"] and fl["enrichment_passage"]["embeddable"]   # the whole profile, searchable


async def test_rerun_is_skipped_within_ttl_and_refresh_appends_nothing(writer, reader, people):
    first = await run_for(writer, [people["Ada Okafor"]])
    second = await run_for(writer, [people["Ada Okafor"]])
    assert second["items"][0]["status"] == "skipped" and second["credits_used"] == 0
    assert second["items"][0]["result"]["skipped"][0]["step"] == "all"
    before = (await reader.get(f"/api/people/{people['Ada Okafor']}")).json()["history"]
    third = await run_for(writer, [people["Ada Okafor"]], refresh=True)
    assert third["items"][0]["status"] == "done" and third["credits_used"] == first["credits_used"]
    # the employer's site is never fetched: the provider describes it, and it is not a personal site
    assert ("scrape", "https://payrail.io") not in FakeFirecrawl.calls
    assert third["items"][0]["result"]["storage"] == "unchanged"
    after = (await reader.get(f"/api/people/{people['Ada Okafor']}")).json()["history"]
    assert {k: len(v) for k, v in after.items()} == {k: len(v) for k, v in before.items()}
    assert written(third["items"][0]["result"]) == {}


async def test_linkedin_not_found_falls_back_to_the_website(writer, reader, people):
    run = await run_for(writer, [people["Bo Chen"]])
    item = run["items"][0]
    res = item["result"]
    assert item["status"] == "done" and res["linkedin"] == "not_found"
    assert "linkedin" in res["not_found"] and "github" in res["not_found"]
    assert model_calls["site"] == 1
    steps = {s["step"]: s["reason"] for s in res["skipped"]}
    assert steps["linkedin"] == "FullEnrich has no record for this profile"
    assert res["providers"] == {"fullenrich": "not_found"}
    assert steps["github"] == "no GitHub link on the profile or sites"
    # The LinkedIn miss cost nothing: one site scrape plus the one company search allowed.
    assert item["credits"] == SCRAPE_CREDITS + SEARCH_CREDITS
    # Company "Leafy": no candidate site and the search finds nothing -> unresolved, nothing written
    assert res["company"] == "unresolved" and "company" in res["not_found"]
    assert ("search", "Leafy official website") in FakeFirecrawl.calls   # the one company search
    person = (await reader.get(f"/api/people/{people['Bo Chen']}")).json()
    assert "company_description" not in person and "github" not in person
    assert "related_links" not in person          # links on a non-personal site are not his
    assert any("not the person's own site" in s["reason"] for s in res["skipped"])


async def test_storage_failure_is_a_skipped_step_and_the_item_stays_done(writer, reader, people):
    def broken(messages, info):
        raise RuntimeError("model down")

    with store.agent.override(model=FunctionModel(broken)):
        run = await run_for(writer, [people["Ada Okafor"]])
    item = run["items"][0]
    assert item["status"] == "done", item
    res = item["result"]
    assert res["storage"] == "error" and res["passage"] == EXPECTED_PASSAGE
    assert "linkedin_headline" in written(res) and "about" not in written(res)
    assert any(s["step"] == "storage" and "model down" in s["reason"] for s in res["skipped"])
    person = (await reader.get(f"/api/people/{people['Ada Okafor']}")).json()
    assert "enrichment_passage" not in person and "about" not in person


async def test_redo_pays_linkedin_again_only_when_the_facts_were_not_filed(writer, reader, people):
    """An item redone after an interruption: with the headline on the record but no filed
    passage, the lookup is paid once more and the facts are filed; with both, nothing is paid."""
    ada = people["Ada Okafor"]
    src = Source("agent", "enrich/linkedin_reader", "run:0/item:0")
    await people_mod.update(None, ada, {"linkedin_headline": "old headline"}, source=src)
    run = await run_for(writer, [ada])
    res = run["items"][0]["result"]
    assert res["linkedin"] == "matched" and res["storage"] == "stored"
    assert sum(c[0] == "fullenrich/people/match" for c in FakeFirecrawl.calls) == 1
    # now both entries are on the record; wipe the run so the TTL skip does not apply
    await runner.abort_all()
    async with db_pg.engine().begin() as conn:
        for t in (db_pg.workflow_run_events, db_pg.workflow_run_items, db_pg.workflow_runs):
            await conn.execute(t.delete())
    FakeFirecrawl.calls = []
    run = await run_for(writer, [ada])
    res = run["items"][0]["result"]
    # the filed passage on the record is the proof the ladder completed: a free skip, with or
    # without the run history (a database copied to another stack keeps the record, not the runs)
    assert run["items"][0]["status"] == "skipped" and run["credits_used"] == 0
    assert res["skipped"][0]["step"] == "all" and "use refresh" in res["skipped"][0]["reason"]
    assert FakeFirecrawl.calls == []
    # refresh runs the ladder again but pays only for what the record does not settle
    run = await run_for(writer, [ada], refresh=True)
    assert run["items"][0]["status"] == "done"
    assert not any(c[0] == "fullenrich/people/match" for c in FakeFirecrawl.calls) or run["credits_used"] > 0


async def test_pdl_first_asks_fullenrich_only_for_gaps(writer, reader, people):
    # FullEnrich alone is the default since PDL bills 60 a match; PDL first is an opt-in ladder
    pdl_first = {"providers": ["people-data-labs", "fullenrich"], "provider_policy": "gaps"}
    specs = {s["key"]: s for s in (await writer.get("/api/assistants")).json()}
    props = specs["enrich"]["options_schema"]["properties"]
    assert props["providers"]["default"] == ["fullenrich"]
    assert props["provider_policy"]["default"] == "gaps"
    # PDL has an about text: FullEnrich is not asked
    run = await run_for(writer, [people["Ada Okafor"]], **pdl_first)
    item = run["items"][0]
    res = item["result"]
    assert res["providers"] == {"people-data-labs": "matched", "fullenrich": "skipped"}
    assert res["linkedin_provider"] == "people-data-labs"
    assert not any(c[0] == "fullenrich/people/match" for c in FakeFirecrawl.calls)
    assert any("not asked" in s["reason"] for s in res["skipped"] if s["step"] == "linkedin")
    w = written(res)
    assert w["about"] == "Payments builder. Ex-Paystack, ex-Stripe." and w["interests"]
    assert w["linkedin_headline"] == "CEO at PayRail"
    # PDL has no description of the employer: the company is researched (payrail.io, 1 page)
    assert res["company"] == "resolved" and model_calls["company"] >= 1
    assert item["credits"] == PDL_CREDITS + GITHUB_CREDITS + 2 * SCRAPE_CREDITS
    # a PDL record without an about text is a gap: FullEnrich is asked and fills it
    await import_people(writer, "Name,LinkedIn\nNia Summers,https://www.linkedin.com/in/nosummary\n",
                        "n.csv", "plus_one")
    nia = str((await db().people.find_one({"name.v": "Nia Summers"}))["_id"])
    FakeFirecrawl.calls = []
    run = await run_for(writer, [nia], **pdl_first)
    res = run["items"][0]["result"]
    assert res["providers"] == {"people-data-labs": "matched", "fullenrich": "matched"}
    assert res["linkedin_provider"] == "people-data-labs"       # the first match stays the profile
    assert res["stored"]["about"]["provider"] == "fullenrich/people/match"
    assert written(res)["about"] == "Founder of PayRail. Previously Paystack and Stripe."
    assert run["credits_used"] >= PDL_CREDITS + LINKEDIN_CREDITS
    # a record without a full name whose PDL profile has none either: the name is a gap too, and
    # the second provider supplies it
    await import_people(writer, "Name,LinkedIn\nAda,https://www.linkedin.com/in/nolastname\n",
                        "a.csv", "plus_one")
    bare = str((await db().people.find_one({"name.v": "Ada"}))["_id"])
    run = await run_for(writer, [bare], **pdl_first)
    res = run["items"][0]["result"]
    assert res["providers"] == {"people-data-labs": "matched", "fullenrich": "matched"}
    assert res["linkedin_provider"] == "people-data-labs" and written(res)["name"] == "Ada Okafor"
    assert (await reader.get(f"/api/people/{bare}")).json()["name"] == "Ada Okafor"


async def test_a_second_provider_adds_its_facts_to_the_passage(writer, reader, people):
    run = await run_for(writer, [people["Ada Okafor"]],
                        providers=["fullenrich", "people-data-labs"], provider_policy="all")
    item = run["items"][0]
    res = item["result"]
    assert item["status"] == "done", item
    assert res["providers"] == {"fullenrich": "matched", "people-data-labs": "matched"}
    assert res["linkedin_provider"] == "fullenrich"            # the first match is the profile
    assert item["credits"] == LINKEDIN_CREDITS + PDL_CREDITS + GITHUB_CREDITS + SCRAPE_CREDITS
    w = written(res)
    assert w["interests"] == ["Fintech Policy", "Open Source"]              # PDL only
    assert w["skills"] == ["Payments", "Fintech", "Python", "Go"]           # unioned, fold-deduped
    assert w["languages"] == ["English", "Yoruba"]
    assert w["about"] == "Founder of PayRail. Previously Paystack and Stripe."   # first text wins
    assert ("# LinkedIn profile (people-data-labs/person/enrich)\n"
            "interests: Fintech Policy; Open Source\n# Personal site") in res["passage"]
    assert res["stored"]["skills"]["provider"] == ("fullenrich/people/match, "
                                                   "people-data-labs/person/enrich")
    dropped = [s["reason"] for s in res["skipped"] if s["step"] == "storage"]
    assert dropped == [("people-data-labs/person/enrich also stated about, role, "
                        "experience, education; the first source's values were kept")]
    assert model_calls == {"site": 1, "storage": 1}
    # a person PDL does not know: a free miss, FullEnrich's facts are filed as usual
    run = await run_for(writer, [people["Bo Chen"]], providers=["people-data-labs"])
    res = run["items"][0]["result"]
    assert res["providers"] == {"people-data-labs": "not_found"} and res["linkedin"] == "not_found"
    assert run["credits_used"] == SCRAPE_CREDITS + SEARCH_CREDITS


async def test_max_new_properties_reaches_the_storage_tool(writer, people, monkeypatch):
    seen: list[store.StoreOptions] = []
    original = store.store

    async def spy(ctx, passage, opts=None):
        seen.append(opts)
        return await original(ctx, passage, opts)

    monkeypatch.setattr(store, "store", spy)
    await run_for(writer, [people["Ada Okafor"]], max_new_properties=3)
    assert [(o.max_new, o.overflow) for o in seen] == [(3, "ignore")]
    await run_for(writer, [people["Bo Chen"]])
    assert len(seen) == 1                      # Bo: no profile, no personal site, no passage
    specs = {s["key"]: s for s in (await writer.get("/api/assistants")).json()}
    assert specs["enrich"]["options_schema"]["properties"]["max_new_properties"]["default"] == 0








async def test_normalise_enriches_a_record_without_a_full_name_first(writer, reader):
    await import_people(writer, "Name,LinkedIn\nAda,https://www.linkedin.com/in/adaokafor\nCy Diaz,\n",
                        "p.csv", "plus_one")
    ids = {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}
    r = await writer.post("/api/assistants/normalize/runs",
                          json={"person_ids": [ids["Ada"], ids["Cy Diaz"]]})
    assert r.status_code == 201, r.text
    await runner.drain()
    run = (await writer.get(f"/api/assistants/runs/{r.json()['id']}")).json()
    by = {i["label"]: i for i in run["items"]}
    ada, cy = by["Ada"], by["Cy Diaz"]
    res = ada["result"]
    assert ada["status"] == "done", ada
    assert res["enrich"]["linkedin"] == "matched" and res["enrich"]["storage"] == "stored"
    assert res["enrich"]["linkedin_provider"] == "fullenrich"
    assert written(res)["name"] == "Ada Okafor" and written(res)["about"]
    assert res["review"]["kind"] == "Person" and res["review"]["repaired"]
    assert "Enriched: LinkedIn profile matched" in res["text"]
    # FullEnrich, the GitHub tool and one company page (the record names no employer description)
    assert ada["credits"] == LINKEDIN_CREDITS + GITHUB_CREDITS + SCRAPE_CREDITS
    person = (await reader.get(f"/api/people/{ids['Ada']}")).json()
    assert person["name"] == "Ada Okafor" and person["record_kind"] == "Person"
    assert person["history"]["name"][-1]["src"]["by"] == "enrich/linkedin_reader"
    # a full name, no LinkedIn: nothing to enrich, nothing paid
    assert cy["result"]["enrich"] is None and cy["credits"] == 0
    # a record marked Unidentifiable while its name was bare is re-judged once the name is full
    await people_mod.update(None, ids["Cy Diaz"], {"record_kind": "Unidentifiable"},
                            source=Source("agent", "repair", "run:0/item:0"))
    r = await writer.post("/api/assistants/normalize/runs", json={"person_ids": [ids["Cy Diaz"]]})
    await runner.drain()
    item = (await writer.get(f"/api/assistants/runs/{r.json()['id']}")).json()["items"][0]
    assert item["result"]["review"]["kind"] == "Person" and item["credits"] == 0
    assert (await reader.get(f"/api/people/{ids['Cy Diaz']}")).json()["record_kind"] == "Person"
    assert "Enriched" not in cy["result"]["text"]
    specs = {s["key"]: s for s in (await writer.get("/api/assistants")).json()}
    assert specs["normalize"]["options_schema"]["properties"]["enrich"]["default"] is True
    assert "provider" not in specs["normalize"]["options_schema"]["properties"]






async def test_provider_error_is_a_skipped_step(writer, people):
    run = await run_for(writer, [people["Dee Evans"]])
    item = run["items"][0]
    assert item["status"] == "done" and item["credits"] == 0
    assert item["result"]["linkedin"] == "error" and "linkedin" not in item["result"]["not_found"]
    assert "provider timeout" in item["result"]["skipped"][0]["reason"]


def test_company_fetch_allowlist_skips_social_and_directory_hosts():
    from app.tools.company import _allow, allow_hosts
    deps = type("D", (), {"notes": {}})()
    allow_hosts(deps, [SearchHit("https://www.linkedin.com/company/tuttiai", "", ""),
                       SearchHit("https://www.crunchbase.com/organization/tutti", "", ""),
                       SearchHit("https://www.withtutti.com/about", "", "")])
    assert deps.notes["company_allowed"] == {"withtutti.com"}
    assert _allow("https://withtutti.com/team", deps.notes["company_allowed"])
    assert not _allow("https://www.linkedin.com/company/tuttiai", deps.notes["company_allowed"])


async def test_nothing_known_costs_nothing(writer, people):
    run = await run_for(writer, [people["Cy Diaz"]])
    item = run["items"][0]
    assert item["status"] == "done" and item["credits"] == 0 and model_calls == {}
    assert {s["step"] for s in item["result"]["skipped"]} == {"linkedin", "sites", "github",
                                                               "company", "storage"}
    assert item["result"]["storage"] == "skipped" and "passage" not in item["result"]
    assert FakeFirecrawl.calls == []


async def test_item_budget_is_respected(writer, people, monkeypatch):
    monkeypatch.setitem(enrich_budget.CAPS, "max_credits", 4)
    run = await run_for(writer, [people["Ada Okafor"]])
    item = run["items"][0]
    assert item["status"] == "done"
    steps = {s["step"]: s["reason"] for s in item["result"]["skipped"]}
    assert "credit cap" in steps["linkedin"]             # 5 expected > 4: never attempted
    assert not any(c[0] == "fullenrich/people/match" for c in FakeFirecrawl.calls)
    assert item["credits"] <= 4 and "linkedin_headline" not in written(item["result"])


async def test_run_credit_cap_pauses_and_a_raised_cap_resumes(writer, reader, people):
    # Concurrency stays at its default: the reservation of in-flight items is what holds Bo back;
    # once Ada's real spend exceeds the cap the run pauses, Bo still queued.
    run = await run_for(writer, [people["Ada Okafor"], people["Bo Chen"]], max_credits=1)
    statuses = {it["person_id"]: it["status"] for it in run["items"]}
    assert statuses[people["Ada Okafor"]] == "done" and statuses[people["Bo Chen"]] == "queued"
    assert run["status"] == "paused" and run["pause_reason"] == "credit cap"
    assert run["done"] == 1 and run["credits_used"] == LINKEDIN_CREDITS + GITHUB_CREDITS + SCRAPE_CREDITS
    active = (await reader.get("/api/assistants/runs?status=active")).json()["items"]
    assert [r["id"] for r in active] == [run["id"]]
    assert (await writer.post(f"/api/assistants/runs/{run['id']}/pause")).status_code == 409
    # resuming with a higher cap finishes the rest
    r = await writer.post(f"/api/assistants/runs/{run['id']}/resume", json={"options": {"max_credits": 200}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running" and r.json()["params"]["options"]["max_credits"] == 200
    await runner.wait(run["id"])
    run = (await writer.get(f"/api/assistants/runs/{run['id']}")).json()
    assert run["status"] == "done" and run["done"] == 2 and run["pause_reason"] is None
    assert {it["status"] for it in run["items"]} == {"done"}
    kinds = [e["kind"] for e in await runner.events(run["id"])]
    assert kinds.count("run") >= 4 and "item" in kinds and "step" in kinds   # started, pausing, paused, resumed, done
    assert (await writer.post(f"/api/assistants/runs/{run['id']}/resume")).status_code == 409
    acts = (await writer.get("/api/activity?action=assistant.")).json()["items"]
    assert {a["action"] for a in acts} >= {"assistant.run", "assistant.pause", "assistant.resume"}


async def test_run_cap_above_one_item_lets_a_bigger_batch_finish(writer, people):
    # cap 100 < concurrency (3) x per-item cap (60): items wait for real spend instead of being
    # skipped on worst-case reservations.
    run = await run_for(writer, [people["Ada Okafor"], people["Bo Chen"], people["Cy Diaz"],
                                 people["Dee Evans"]], max_credits=100)
    assert {it["status"] for it in run["items"]} == {"done"}, run["items"]
    assert "stopped_reason" not in run["summary"]
    traces = {it["trace_id"] for it in run["items"]}
    assert None not in traces and len(traces) == 4    # one Logfire trace per person








async def test_unconfigured_firecrawl_fails_fast(writer, people, monkeypatch):
    monkeypatch.setattr(enrich_workflow, "AsyncFirecrawl",
                        lambda budget: AsyncFirecrawl(api_key="", budget=budget))
    run = await run_for(writer, [people["Ada Okafor"]])
    item = run["items"][0]
    assert item["status"] == "failed" and "FIRECRAWL_API_KEY" in item["error"]
    assert run["failed"] == 1 and model_calls == {}


async def test_mcp_enrich_person_runs_the_ladder_once(writer, client_factory, people):
    from tests.test_mcp import call, init
    key = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    err, res = await call(c, key, "enrich_person", {"id": people["Ada Okafor"],
                                                   "options": {"providers": ["fullenrich"], "max_credits": 30}})
    assert not err, res
    assert res["status"] == "done" and res["credits"] > 0 and res["linkedin"] == "matched"
    assert "linkedin_headline" in res["written"] and res["review"]["kind"] == "Person"
    assert res["run_id"] and res["text"].startswith("LinkedIn profile matched")
    # the same person again is a TTL skip, free
    err, again = await call(c, key, "enrich_person", {"id": people["Ada Okafor"]})
    assert not err and again["status"] == "skipped" and again["credits"] == 0
    err, _ = await call(c, key, "enrich_person", {"id": "000000000000000000000000"})
    assert err


async def test_a_provider_that_overbilled_is_not_asked_again(writer, people, monkeypatch):
    from app.integrations import firecrawl as fc
    monkeypatch.setitem(fc.OBSERVED_PRICES, "people-data-labs/person/enrich", 60)
    monkeypatch.setitem(PROVIDERS, "people-data-labs",
                        dataclasses.replace(PROVIDERS["people-data-labs"], credits=1))  # the listed price
    run = await run_for(writer, [people["Ada Okafor"]], providers=["people-data-labs", "fullenrich"])
    res = run["items"][0]["result"]
    assert res["providers"]["people-data-labs"] == "skipped" and res["providers"]["fullenrich"] == "matched"
    reasons = {s["step"]: s["reason"] for s in res["skipped"]}
    assert "billed 60 credits" in reasons["linkedin"]
    assert not any(c[0] == "people-data-labs/person/enrich" for c in FakeFirecrawl.calls)
