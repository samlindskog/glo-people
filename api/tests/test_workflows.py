"""The enrichment workflow end to end against a fake Firecrawl and fake models.

The fakes count every call, so these tests pin down the credit discipline: what runs, what is
skipped and why, and that nothing is ever overwritten.
"""

from contextlib import ExitStack
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic_ai import ModelMessage, ModelResponse
from pydantic_ai.messages import ModelRequest, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app import embeddings, jobs
from app.mongo import db
from app.workflows import runner
from app.workflows.enrich import budget as enrich_budget
from app.workflows.enrich import company, linkedin_reader, site_reader
from app.workflows.enrich.urls import url_key
from app.workflows.firecrawl import (
    SCRAPE_CREDITS,
    SEARCH_CREDITS,
    AsyncFirecrawl,
    FirecrawlError,
    NotFound,
    ScrapeResult,
    SearchHit,
)

FIX = Path(__file__).parent / "fixtures" / "firecrawl"
LINKEDIN_CREDITS = 10   # a "base" provider

PEOPLE = """Name,Company,Location,LinkedIn,Website,Bio
Ada Okafor,PayRail,"Lagos, Nigeria",https://www.linkedin.com/in/adaokafor?,www.adaokafor.dev,Builds payments infrastructure
Bo Chen,Leafy,Berlin,https://linkedin.com/in/bochen,https://bochen.me,Climate founder
Cy Diaz,,,,,Doctor turned investor
Dee Evans,,,https://www.linkedin.com/in/exchange-fail,,Operator
"""

PAGES = {
    "https://www.linkedin.com/in/adaokafor": ("linkedin_ada.md", LINKEDIN_CREDITS, "fullenrich",
                                              ["https://adaokafor.dev", "https://payrail.io",
                                               "https://github.com/adaok"]),
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


def linkedin_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    model_calls["linkedin"] = model_calls.get("linkedin", 0) + 1
    return _out(info, {
        "headline": "Building payments infrastructure for African merchants · CEO at PayRail",
        "location": "Lagos, Nigeria", "summary": "Founder of PayRail.",
        "current_company": {"name": "PayRail", "website": "https://payrail.io"},
        "personal_sites": ["https://adaokafor.dev"], "github_url": "https://github.com/adaok",
        "other_links": ["https://twitter.com/adaokafor"]})


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


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embeddings.set_embedder(embeddings.FakeEmbedder(512))
    FakeFirecrawl.calls = []
    model_calls.clear()
    monkeypatch.setattr(runner, "AsyncFirecrawl",
                        lambda budget: FakeFirecrawl(api_key="fake", budget=budget))
    with ExitStack() as stack:
        stack.enter_context(linkedin_reader.agent.override(model=FunctionModel(linkedin_model)))
        stack.enter_context(site_reader.agent.override(model=FunctionModel(site_model)))
        stack.enter_context(company.agent.override(model=FunctionModel(company_model)))
        yield


@pytest.fixture
async def people(writer):
    r = await writer.post("/api/people/import", data={"tier": "member"},
                          files={"file": ("p.csv", PEOPLE.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    await jobs.drain()
    return {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}


async def run_for(client, ids, **options):
    r = await client.post("/api/workflows/enrich/runs", json={"person_ids": ids,
                                                              "options": options})
    assert r.status_code == 201, r.text
    await runner.drain()
    return (await client.get(f"/api/workflows/runs/{r.json()['id']}")).json()


async def test_workflow_listing_and_permissions(reader, writer):
    specs = (await reader.get("/api/workflows")).json()
    assert specs[0]["key"] == "enrich" and specs[0]["selector_field"] == "linkedin"
    assert "mode" in specs[0]["options_schema"]["properties"]
    r = await reader.post("/api/workflows/enrich/runs", json={"person_ids": ["x"]})
    assert r.status_code == 403
    assert (await reader.get("/api/workflows/runs")).json() == {"items": [], "has_more": False}
    r = await writer.post("/api/workflows/enrich/runs", json={"person_ids": []})
    assert r.status_code == 422
    r = await writer.post("/api/workflows/enrich/runs", json={"person_ids": ["x"],
                                                              "options": {"max_credits": 0}})
    assert r.status_code == 422
    r = await writer.post("/api/workflows/nope/runs", json={"person_ids": ["x"]})
    assert r.status_code == 404


async def test_full_ladder_writes_history_entries(writer, reader, people):
    run = await run_for(writer, [people["Ada Okafor"]])
    assert run["status"] == "done" and run["done"] == 1 and run["failed"] == 0, run
    item = run["items"][0]
    assert item["status"] == "done", item
    res = item["result"]
    assert res["linkedin"] == "matched" and res["company"] == "resolved"
    assert res["written"] == {
        "linkedin_headline": "Building payments infrastructure for African merchants · CEO at PayRail",
        "github": "https://github.com/adaok",
        "related_links": ["https://twitter.com/adaokafor", "https://rails.substack.com"],
        "company_website": "https://payrail.io",
        "company_summary": "PayRail provides payments infrastructure for merchants in West "
                           "Africa, with next-day local settlement.",
    }
    # Her site is already on the record (www.adaokafor.dev): not re-added.
    assert "website" not in res["written"]
    assert item["credits"] == LINKEDIN_CREDITS + 2 * SCRAPE_CREDITS == run["credits_used"]
    assert [c for c in FakeFirecrawl.calls if c[0] == "search"] == []
    assert model_calls == {"linkedin": 1, "site": 1, "company": 2}

    person = (await reader.get(f"/api/people/{people['Ada Okafor']}")).json()
    assert person["github"] == "https://github.com/adaok"
    src = person["history"]["github"][0]["src"]
    assert src == {"kind": "agent", "by": "enrich/linkedin_reader",
                   "ref": f"run:{run['id']}/item:{item['id']}"}
    assert person["history"]["company_summary"][0]["src"]["by"] == "enrich/company"
    assert len(person["history"]["website"]) == 1 and len(person["history"]["company"]) == 1
    fl = {f["key"]: f for f in (await reader.get("/api/fields")).json()}
    assert fl["company_summary"]["embed"] and not fl["github"]["embed"]
    assert not fl["related_links"]["embed"]


async def test_rerun_is_skipped_within_ttl_and_refresh_appends_nothing(writer, reader, people):
    first = await run_for(writer, [people["Ada Okafor"]])
    second = await run_for(writer, [people["Ada Okafor"]])
    assert second["items"][0]["status"] == "skipped" and second["credits_used"] == 0
    assert second["items"][0]["result"]["skipped"][0]["step"] == "all"
    before = (await reader.get(f"/api/people/{people['Ada Okafor']}")).json()["history"]
    third = await run_for(writer, [people["Ada Okafor"]], refresh=True)
    assert third["items"][0]["status"] == "done" and third["credits_used"] == first["credits_used"]
    after = (await reader.get(f"/api/people/{people['Ada Okafor']}")).json()["history"]
    assert {k: len(v) for k, v in after.items()} == {k: len(v) for k, v in before.items()}
    assert third["items"][0]["result"]["written"] == {}


async def test_linkedin_not_found_falls_back_to_the_website(writer, reader, people):
    run = await run_for(writer, [people["Bo Chen"]])
    item = run["items"][0]
    res = item["result"]
    assert item["status"] == "done" and res["linkedin"] == "not_found"
    assert "linkedin" in res["not_found"] and "github" in res["not_found"]
    assert model_calls.get("linkedin", 0) == 0 and model_calls["site"] == 1
    steps = {s["step"]: s["reason"] for s in res["skipped"]}
    assert "no data provider" in steps["linkedin"]
    assert "frugal mode does not search" in steps["github"]
    # The LinkedIn miss cost nothing: one site scrape plus the one company search frugal allows.
    assert item["credits"] == SCRAPE_CREDITS + SEARCH_CREDITS
    # Company "Leafy": no candidate site and the search finds nothing -> unresolved, nothing written
    assert res["company"] == "unresolved" and "company" in res["not_found"]
    assert ("search", "Leafy official website") in FakeFirecrawl.calls
    person = (await reader.get(f"/api/people/{people['Bo Chen']}")).json()
    assert "company_summary" not in person and "github" not in person


async def test_provider_route_failure_counts_as_not_found(writer, people):
    run = await run_for(writer, [people["Dee Evans"]])
    item = run["items"][0]
    assert item["status"] == "done" and item["credits"] == 0
    assert item["result"]["linkedin"] == "not_found" and "linkedin" in item["result"]["not_found"]
    assert "could not resolve" in item["result"]["skipped"][0]["reason"]


async def test_nothing_known_costs_nothing(writer, people):
    run = await run_for(writer, [people["Cy Diaz"]])
    item = run["items"][0]
    assert item["status"] == "done" and item["credits"] == 0 and model_calls == {}
    assert {s["step"] for s in item["result"]["skipped"]} == {"linkedin", "sites", "github",
                                                               "company"}
    assert FakeFirecrawl.calls == []


async def test_item_budget_is_respected(writer, people, monkeypatch):
    monkeypatch.setitem(enrich_budget.CAPS["frugal"], "max_credits", 5)
    run = await run_for(writer, [people["Ada Okafor"]])
    item = run["items"][0]
    assert item["status"] == "done"
    steps = {s["step"]: s["reason"] for s in item["result"]["skipped"]}
    assert "credit cap" in steps["linkedin"]             # 40 expected > 5: never attempted
    assert ("scrape", "https://www.linkedin.com/in/adaokafor") not in FakeFirecrawl.calls
    assert item["credits"] <= 5 and "linkedin_headline" not in item["result"]["written"]


async def test_run_credit_cap_stops_the_run(writer, people):
    # Concurrency stays at its default: the reservation of in-flight items is what stops Bo.
    run = await run_for(writer, [people["Ada Okafor"], people["Bo Chen"]], max_credits=1)
    statuses = {it["person_id"]: it["status"] for it in run["items"]}
    assert statuses[people["Ada Okafor"]] == "done" and statuses[people["Bo Chen"]] == "skipped"
    assert run["summary"]["stopped_reason"] == "credit cap" and run["status"] == "done"
    skipped = next(it for it in run["items"] if it["status"] == "skipped")
    assert skipped["error"] == "run credit cap reached"


async def test_bulk_selector_and_listing(writer, people):
    r = await writer.post("/api/workflows/enrich/runs", json={"all_with_field": "linkedin"})
    assert r.status_code == 201 and r.json()["total"] == 3
    await runner.drain()
    runs = (await writer.get("/api/workflows/runs")).json()["items"]
    assert runs[0]["id"] == r.json()["id"] and runs[0]["username"] == "wendy"
    assert runs[0]["status"] == "done" and runs[0]["done"] == 3
    acts = (await writer.get("/api/activity?action=workflow.run")).json()["items"]
    assert acts[0]["meta"]["people"] == 3 and acts[0]["meta"]["workflow"] == "enrich"


async def test_reconcile_fails_interrupted_runs(writer, people):
    from sqlalchemy import insert

    from app import db as pg
    async with pg.engine().begin() as conn:
        rid = (await conn.execute(insert(pg.workflow_runs).values(
            workflow="enrich", status="running", total=1).returning(pg.workflow_runs.c.id))
        ).scalar_one()
        await conn.execute(insert(pg.workflow_run_items).values(
            run_id=rid, workflow="enrich", person_id=people["Cy Diaz"], status="running"))
    await runner.reconcile()
    run = (await writer.get(f"/api/workflows/runs/{rid}")).json()
    assert run["status"] == "failed" and run["error"] == "interrupted by restart"
    assert run["items"][0]["status"] == "failed"
    assert (await writer.post(f"/api/workflows/runs/{rid}/cancel")).status_code == 409


async def test_unconfigured_firecrawl_fails_fast(writer, people, monkeypatch):
    monkeypatch.setattr(runner, "AsyncFirecrawl", lambda budget: AsyncFirecrawl(api_key="",
                                                                                 budget=budget))
    run = await run_for(writer, [people["Ada Okafor"]])
    item = run["items"][0]
    assert item["status"] == "failed" and "FIRECRAWL_API_KEY" in item["error"]
    assert run["failed"] == 1 and model_calls == {}

