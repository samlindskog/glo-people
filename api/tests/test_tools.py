"""The tools (plan, store, normalize) and the ask assistant with deterministic fake models, their
REST and MCP surfaces."""

import pytest
from pydantic_ai import ModelResponse
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app import embedder, properties
from app.agents.fakes import _retried
from app.assistants import runner
from app.config import get_settings
from app.history import Source
from app.mongo import db
from app.tools import invoke
from app.tools import plan as plan_tool
from app.tools import repair as repair_tool
from tests.conftest import import_people
from tests.test_mcp import call, init

CSV = """ID,Name,Bio,MRR,Stage,Notes
1,Ada,payments infrastructure for merchants,"$12,000",seed,met at demo day
2,Bo,climate venture investor,500,Series A,
3,Cy,payments and lending,,Pre-seed w/o revenue,
"""


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    monkeypatch.setattr(get_settings(), "fake_agents", True)


@pytest.fixture
async def people_by_name(writer):
    await import_people(writer, CSV, "a.csv", "member")
    return {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}


# --- plan + ask ---------------------------------------------------------------------------------------

async def test_plan_is_an_executable_request(people_by_name, reader):
    r = await reader.post("/api/search/plan", json={"question": "payments founders with mrr over 10000",
                                                    "options": {"threshold": 0.0}})
    assert r.status_code == 200, r.text
    plan = r.json()
    req = plan["request"]
    assert req["semantic"]["queries"] == [{"field": "bio", "query": "payments founders with mrr over 10000"}]
    assert req["filters"] == [{"field": "mrr", "op": "gte", "value": 10000}]
    assert req["semantic"]["threshold"] == 0.0 and req["limit"] == 12 and plan["rationale"]
    run = (await reader.post("/api/search", json=req)).json()
    assert [h["person"]["name"] for h in run["results"]] == ["Ada"]
    assert (await reader.post("/api/search/plan", json={"question": "x", "options": {"k": 0}})
            ).status_code == 422


async def test_ask_answers_in_text_from_the_planned_search(people_by_name, reader):
    r = await reader.post("/api/search/ask", json={"question": "payments founders with mrr over 10000",
                                                   "options": {"threshold": 0.0}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "Ada" in body["text"] and body["changes"] == []
    assert [h["person"]["name"] for h in body["results"]] == ["Ada"] and body["total"] == 1
    assert body["citations"][0]["person_id"] == people_by_name["Ada"]
    # the executed request is an ordinary search: saved in the history and re-runnable
    req = body["request"]
    assert req["semantic"]["queries"][0]["field"] == "bio" and req["semantic"]["threshold"] == 0.0
    again = (await reader.post("/api/search", json=req)).json()
    assert [h["id"] for h in again["results"]] == [h["id"] for h in body["results"]]
    mine = (await reader.get("/api/searches")).json()["items"]
    saved = next(s for s in mine if s["id"] == body["search_id"])
    assert saved["params"]["ask"].startswith("payments") and saved["source"] == "ui"
    # no hits: a plain answer, no model call needed
    r = await reader.post("/api/search/ask", json={"question": "robotics with mrr over 99999999"})
    assert r.status_code == 200 and r.json()["total"] == 0
    assert "No one" in r.json()["text"]


async def test_invalid_plan_is_retried_then_fails_clearly(people_by_name, reader):
    attempts = []

    def flaky(messages, info):
        retried = _retried(messages)
        attempts.append(retried)
        if not retried:   # first answer: a filter on an unknown property and a passage on a number
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
                "semantic": [{"field": "mrr", "query": "x"}],
                "filters": [{"field": "nonexistent", "op": "eq", "value": 1}]})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "semantic": [{"field": "bio", "query": "climate"}], "filters": []})])

    with plan_tool.agent.override(model=FunctionModel(flaky)):
        r = await reader.post("/api/search/ask", json={"question": "climate", "options": {"threshold": 0}})
    assert r.status_code == 200, r.text
    assert attempts == [False, True]
    assert r.json()["results"][0]["person"]["name"] == "Bo"

    def hopeless(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "semantic": [], "filters": [{"field": "stage", "op": "eq", "value": "Nonsense"}]})])

    with plan_tool.agent.override(model=FunctionModel(hopeless)):
        r = await reader.post("/api/search/plan", json={"question": "anything"})
    assert r.status_code == 502 and "agent failed" in r.text


# --- store -------------------------------------------------------------------------------------------

def by_change(result: dict) -> dict:
    return {(c["key"], c["action"]): c for c in result["changes"]}


async def test_store_files_facts_under_properties(people_by_name, writer, reader):
    ada = people_by_name["Ada"]
    r = await writer.post(f"/api/people/{ada}/store?wait=true", json={
        "passage": "notes: follows up next week. mrr: 20k. sectors: payments; lending. "
                   "stage: seed round closed. bio: payments infrastructure for merchants. "
                   "Hi there. new favourite tool: lathe"})
    assert r.status_code == 200, r.text
    res = r.json()
    by = by_change(res)
    assert by[("notes", "set")] == {"key": "notes", "action": "set", "value": "follows up next week",
                                    "previous": "met at demo day", "flag": None}
    assert by[("mrr", "set")]["value"] == "20k" and by[("mrr", "normalized")]["value"] == 20000
    assert by[("sectors", "append")]["value"] == ["payments", "lending"]
    assert by[("stage", "set")]["value"] == "seed round closed"
    assert by[("stage", "flagged")]["flag"] == "unmapped"
    assert by[("favourite_tool", "property_created")]["value"] == "The person's favourite tool"
    assert by[("favourite_tool", "set")]["value"] == "lathe"
    assert ("bio", "set") not in by and res["confirmed"] == ["bio"]        # restated, not written
    assert res["ignored"] == ["Hi there"]
    assert res["conflicts"] == ["notes", "mrr", "stage"]
    assert res["created_properties"] == ["favourite_tool"] and res["redirected"] == {}
    # the clean-up pass mapped the enum text the parser could not read
    norm = res["normalization"]
    assert norm["agent_called"] and by_change(norm)[("stage", "normalized")]["value"] == "Seed"
    person = (await reader.get(f"/api/people/{ada}")).json()
    assert person["norm"]["mrr"] == 20000 and person["norm"]["stage"] == "Seed"
    assert [e["v"] for e in person["history"]["notes"]] == ["met at demo day", "follows up next week"]
    assert len(person["history"]["bio"]) == 1                                   # confirmed: untouched
    assert person["history"]["notes"][-1]["src"] == {"kind": "ui", "by": "wendy", "ref": "agent:store"}
    prop = await properties.get("favourite_tool")
    assert prop.type == "text" and prop.source == "agent"
    assert prop.description == "The person's favourite tool"
    acts = (await writer.get("/api/activity?action=person.store")).json()["items"]
    assert acts[0]["meta"]["created"] == ["favourite_tool"]

    # appending to a list keeps what is there and skips folded duplicates
    r = await writer.post(f"/api/people/{ada}/store?wait=true", json={"passage": "sectors: Lending; ai"})
    assert by_change(r.json())[("sectors", "append")]["value"] == ["payments", "lending", "ai"]
    # a near-duplicate new property is redirected to the existing one
    r = await writer.post(f"/api/people/{ada}/store?wait=true", json={"passage": "new favourite tools: hammer",
                                                           "options": {"near_duplicate": 0.3}})
    assert r.json()["redirected"] == {"favourite_tools": "favourite_tool"}
    assert by_change(r.json())[("favourite_tool", "set")] == {
        "key": "favourite_tool", "action": "set", "value": "hammer", "previous": "lathe", "flag": None}
    # beyond max_new the facts go to notes instead of a new property
    r = await writer.post(f"/api/people/{ada}/store?wait=true", json={
        "passage": "new eye colour: green. new shoe size: 42", "options": {"max_new": 1}})
    assert r.json()["created_properties"] == ["eye_colour"] and r.json()["to_notes"] == ["Shoe size"]
    assert by_change(r.json())[("notes", "set")]["value"] == "Shoe size: 42"

    assert (await writer.post("/api/people/000000000000000000000000/store?wait=true",
                              json={"passage": "x: y"})).status_code == 404
    assert (await reader.post(f"/api/people/{ada}/store?wait=true", json={"passage": "x: y"})
            ).status_code == 403
    assert (await writer.post(f"/api/people/{ada}/store/apply", json={})).status_code in (404, 405)


async def test_store_overflow_ignore_and_evidence(people_by_name, writer, reader):
    ada = people_by_name["Ada"]
    # a labelled multi-line passage (what the enrich assistant sends): one fact per line, '#'
    # headers ignored, facts without a home dropped instead of filed under notes
    passage = ("# Facts from data providers; each line names the property it belongs to.\n"
               "# LinkedIn profile (fullenrich people/match)\n"
               "notes: met at the summit. follows up next week\n"
               "mrr: 20k\n"
               "new hat size: 7\n"
               "new shoe size: 42\n")
    r = await writer.post(f"/api/people/{ada}/store?wait=true", json={
        "passage": passage, "options": {"max_new": 0, "overflow": "ignore"}})
    assert r.status_code == 200, r.text
    res = r.json()
    by = by_change(res)
    assert by[("notes", "set")]["value"] == "met at the summit. follows up next week"   # one line, one write
    assert by[("mrr", "set")]["value"] == "20k"
    assert res["dropped"] == ["Hat size", "Shoe size"] and res["to_notes"] == []
    assert res["created_properties"] == [] and await properties.get("hat_size") is None
    assert res["evidence"]["mrr"] == "mrr: 20k"
    assert res["evidence"]["notes"] == "notes: met at the summit. follows up next week"
    assert res["ignored"][0].startswith("# Facts")
    # the default still files the overflow under notes
    r = await writer.post(f"/api/people/{ada}/store?wait=true", json={
        "passage": "new eye colour: green", "options": {"max_new": 0}})
    assert r.json()["to_notes"] == ["Eye colour"] and r.json()["dropped"] == []
    # the audit passage is a property the agent never sees as a current value
    from app.tools.store import render_person
    assert "enrichment_passage" not in render_person({"id": "x", "name": "A", "enrichment_passage": "# …",
                                                      "notes": "n", "norm": {}, "norm_flags": {}})
    cat = {p["key"]: p for p in (await reader.get("/api/properties")).json()}
    assert cat["enrichment_passage"]["type"] == "text" and not cat["enrichment_passage"]["embeddable"]
    assert cat["skills"]["type"] == "list" and cat["skills"]["embeddable"]
    assert cat["about"]["embeddable"] and cat["experience"]["type"] == "text"


def test_storage_check_evidence_locked_and_confirmations():
    from app.properties import Property
    from app.tools.store import NewProperty, Proposal, Write, check
    cat = {k: Property(key=k, type="text", label=k, description="", embeddable=True,
                       normalisable=False, filter_ops=[], source="spec")
           for k in ("about", "company", "title_enriched", "notes")}
    passage = ("about: Currently a medical student, but I’ve always had one too many shoes I "
               "wanted to have the foot in. I found that defining myself a Renaissance woman "
               "would make it all more cohesive… and it does in some sense.\n"
               "title_enriched: Research Assistant")
    # a curly apostrophe copied as a straight one, and a long quote shortened at its end
    ok = Proposal(writes=[Write(key="about", value="x", evidence="Currently a medical student, "
                                "but I've always had one too many shoes I wanted"),
                          Write(key="title_enriched", value="Research Assistant",
                                evidence="title_enriched: Research Assistant")])
    assert check(ok, cat, passage) == []
    made_up = Proposal(writes=[Write(key="notes", value="x", evidence="Studied at Oxford")])
    assert check(made_up, cat, passage)[0] == "notes: evidence must be copied verbatim from the passage"
    # locked keys may be confirmed, never written; confirmations need a current value
    locked = frozenset({"company"})
    bad = Proposal(writes=[Write(key="company", value="ICLUB", evidence="title_enriched: Research")],
                   confirmed=["title_enriched"],
                   new_properties=[NewProperty(key="hat_size", label="Hat size",
                                               description="d", value="7")])
    problems = check(bad, cat, passage, current={"about": "old"}, locked=locked)
    assert problems[0].startswith("company is set by the caller")
    assert "title_enriched has no current value" in problems[1]
    fine = Proposal(confirmed=["company", "about"], ignored=["title_enriched: not a job title"])
    assert check(fine, cat, passage, current={"about": "old"}, locked=locked) == []
    # a labelled line anchors its fact: its evidence is not re-checked word by word
    anchored = Proposal(writes=[Write(key="about", value="x", evidence="about: a paraphrase")],
                        ignored=["title_enriched: not a title"])
    assert check(anchored, cat, passage) == []
    assert check(Proposal(writes=[Write(key="notes", value="x", evidence="a paraphrase")]),
                 cat, passage)[0] == "notes: evidence must be copied verbatim from the passage"
    # a proposed property that names an existing one is that property
    dup = Proposal(new_properties=[NewProperty(key="professional_interests", label="Professional interests",
                                               description="d", value=["x"])])
    cat2 = {**cat, "interests": Property(key="interests", type="list", label="Interests", description="",
                                         embeddable=True, normalisable=False, filter_ops=[], source="spec")}
    assert check(dup, cat2, None) == [("new property 'professional_interests': 'interests' exists "
                                       "for this kind of fact; write to it instead")]
    # every labelled line must be filed, confirmed or set aside
    partial = Proposal(writes=[Write(key="about", value="x", evidence="about")])
    assert check(partial, cat, passage) == [("these labelled lines were neither filed, confirmed nor "
                                             "ignored: title_enriched (write each, confirm it, or list "
                                             "its label under ignored with the reason)")]
    accounted = Proposal(writes=[Write(key="about", value="x", evidence="about")],
                         ignored=["title_enriched: not a job title"])
    assert check(accounted, cat, passage) == []
    # an empty answer to a non-empty passage is not an answer
    assert check(Proposal(), cat, passage)[0].startswith("the proposal is empty")
    assert check(Proposal(ignored=["hi there"]), cat, "hi there") == []
    assert check(Proposal(), cat, "   ") == []


async def test_store_from_file(people_by_name, writer, reader):
    bo = people_by_name["Bo"]
    r = await writer.post(f"/api/people/{bo}/store/file?wait=true",
                          files={"file": ("call.md", b"notes: wants intros to LPs\n", "text/markdown")})
    assert r.status_code == 200, r.text
    assert by_change(r.json())[("notes", "set")]["value"] == "wants intros to LPs"
    r = await writer.post(f"/api/people/{bo}/store/file?wait=true",
                          files={"file": ("e.txt", b"   ", "text/plain")})
    assert r.status_code == 422


# --- normalise one person ----------------------------------------------------------------------------

async def test_normalize_parses_then_asks_the_agent(people_by_name, writer, reader):
    cy = people_by_name["Cy"]
    before = (await reader.get(f"/api/people/{cy}")).json()
    assert before["norm_flags"] == {"stage": "unmapped"}
    await db().people.update_one({"_id": (await db().people.find_one({"name.v": "Cy"}))["_id"]},
                                 {"$set": {"mrr": {"v": "lots", "h": [{"v": "lots"}]}}})
    r = await writer.post(f"/api/people/{cy}/normalize?wait=true")
    assert r.status_code == 200, r.text
    res = r.json()
    by = by_change(res)
    mrr_flags = [c["flag"] for c in res["changes"] if c["key"] == "mrr" and c["action"] == "flagged"]
    assert mrr_flags == ["unmapped", "non_normalizable"]          # the parser, then the agent
    assert by[("stage", "normalized")]["value"] == "Pre-seed"     # the agent mapped the old one
    assert res["agent_called"] and res["unmapped"] == []
    after = (await reader.get(f"/api/people/{cy}")).json()
    assert after["norm"]["stage"] == "Pre-seed" and after["norm_flags"] == {"mrr": "non_normalizable"}
    entry = after["history_norm"]["stage"][-1]
    assert entry["src"] == {"kind": "agent", "by": "normalizer", "ref": "user:wendy"}
    # nothing left for the agent: a second call is free and changes nothing
    res = (await writer.post(f"/api/people/{cy}/normalize?wait=true")).json()
    assert not res["agent_called"] and res["changes"] == []
    # and the parser does not undo the agent on a later unrelated write
    await writer.patch(f"/api/people/{cy}", json={"fields": {"bio": "lending for SMBs"}})
    assert (await reader.get(f"/api/people/{cy}")).json()["norm"]["stage"] == "Pre-seed"
    assert (await reader.post(f"/api/people/{cy}/normalize?wait=true")).status_code == 403


async def test_normalize_merges_allocates_and_drops_enum_values(people_by_name, writer, reader):
    # a stage text the parser cannot read, a region not in the list, an industry list in `stage`
    # for another person, and a second person with the same stage wording
    ada, bo, cy = (people_by_name[n] for n in ("Ada", "Bo", "Cy"))
    await writer.patch(f"/api/people/{ada}", json={"fields": {"region": "Lisbon",
                                                              "stage": "seed round closed"}})
    r = await writer.post(f"/api/people/{ada}/normalize?wait=true")
    assert r.status_code == 200, r.text
    res = r.json()
    by = by_change(res)
    assert by[("stage", "normalized")]["value"] == "Seed" and res["merged"] == {"stage": "Seed"}
    assert by[("region", "enum_value_added")]["value"] == "Lisbon"
    assert by[("region", "normalized")]["value"] == "Lisbon" and res["allocated"] == {"region": ["Lisbon"]}
    assert res["rejected"] == {} and res["unmapped"] == []
    # the registry remembers both: the alias and the new value with its source
    assert "Lisbon" in await properties.enum_values("region")
    lookup = await properties.enum_lookup("stage")
    assert lookup["seed round closed"] == "Seed"
    doc = await db().fields.find_one({"_id": "region"})
    added = next(e for e in doc["enum"]["values"] if e["v"] == "Lisbon")
    assert added["src"].startswith("agent:normalizer") and added["aliases"] == []
    # so the parser alone handles the same wording for the next person: no agent call
    await writer.patch(f"/api/people/{bo}", json={"fields": {"stage": "seed round closed",
                                                             "region": "lisbon"}})
    bo_doc = (await reader.get(f"/api/people/{bo}")).json()
    assert bo_doc["norm"]["stage"] == "Seed" and bo_doc["norm"]["region"] == "Lisbon"
    res = (await writer.post(f"/api/people/{bo}/normalize?wait=true")).json()
    assert not res["agent_called"]
    # an industry list in the stage field is dropped, not allocated
    await writer.patch(f"/api/people/{cy}", json={
        "fields": {"stage": "Human Resources & Workforce, Artificial Intelligence"}})
    res = (await writer.post(f"/api/people/{cy}/normalize?wait=true")).json()
    assert by_change(res)[("stage", "flagged")]["flag"] == "non_normalizable"
    assert res["allocated"] == {} and "Human Resources & Workforce, Artificial Intelligence" not in \
        await properties.enum_values("stage")
    # with allocation off a new category stays unmapped and is reported as rejected
    await writer.patch(f"/api/people/{cy}", json={"fields": {"region": "Porto"}})
    r = await writer.post(f"/api/people/{cy}/normalize?wait=true", json={"options": {"allocate": False}})
    res = r.json()
    assert res["rejected"] == {"region": ["Porto"]} and res["unmapped"] == ["region"]
    assert "Porto" not in await properties.enum_values("region")
    assert (await reader.get(f"/api/people/{cy}")).json()["norm_flags"]["region"] == "unmapped"
    # the cap counts new values per call
    await writer.patch(f"/api/people/{cy}", json={"fields": {"warmth": "Glowing"}})
    r = await writer.post(f"/api/people/{cy}/normalize?wait=true", json={"options": {"max_new_values": 1}})
    res = r.json()
    assert sorted(res["allocated"]) == ["region"] and res["rejected"] == {"warmth": ["Glowing"]}


# --- repair -----------------------------------------------------------------------------------------

async def repair(pid: str, src: Source,
                 opts: repair_tool.RepairOptions | None = None) -> repair_tool.RepairResult:
    """The repair tool on one record, as any caller runs a tool: through an invocation."""
    ctx = await invoke.make_context(pid, src, None)
    assert ctx is not None
    result = await invoke.run(repair_tool.repair, ctx, opts=opts)
    assert isinstance(result, repair_tool.RepairResult)
    return result


async def make(writer, fields_: dict, tier: str = "member") -> str:
    r = await writer.post("/api/people", json={"tier": tier, "fields": fields_})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_signals_and_mechanical_fixes():
    doc = {"name": {"v": "AndrÃ© NiÃ±o", "h": []}, "linkedin": {"v": "https://linkedin.com/in/andre-nino", "h": []}}
    assert repair_tool.signals(doc) == ["mojibake", "symbols in name"]
    assert repair_tool.mechanical_fixes(doc) == {"name": "André Niño"}
    doc = {"name": {"v": "elizabeth hsu", "h": []}, "email": {"v": "e@x.io", "h": []}}
    assert repair_tool.signals(doc) == ["lowercase"]
    assert repair_tool.mechanical_fixes(doc) == {"name": "Elizabeth Hsu"}
    assert repair_tool.mechanical_fixes({"name": {"v": "polly.israni@gmail.com", "h": []}}) == {}
    assert repair_tool.is_full_name("Ada Okafor") and not repair_tool.is_full_name("Polly I.")
    assert not repair_tool.is_full_name("Ada")
    assert repair_tool.title_case("o'neil mcdonald-smith") == "O'Neil Mcdonald-Smith"
    assert repair_tool.title_case("Already Fine") == "Already Fine"
    doc = {"name": {"v": "Vlad", "h": []}, "email": {"v": "v@x.io", "h": []}}
    assert repair_tool.signals(doc) == ["single token"]
    doc = {"name": {"v": "Raj - LootMogul accelerated by a16z", "h": []}}
    assert set(repair_tool.signals(doc)) >= {"digits in name", "five or more tokens",
                                             "nothing identifying besides the name"}
    doc = {"name": {"v": "Alex Engkelidis", "h": []}, "linkedin": {"v": "https://linkedin.com/in/aex", "h": []}}
    assert repair_tool.signals(doc) == ["name absent from linkedin slug"]


async def test_repair_fixes_names_classifies_and_moves_misfiled(writer, reader):
    src = Source("agent", "repair", "test")
    # mojibake and casing are fixed mechanically; a two-token name is complete: Person
    pid = await make(writer, {"name": "andrÃ© niÃ±o", "bio": "Founder of a fintech."})
    res = await repair(pid, src)
    assert res.mechanical == ["name"] and res.kind == "Person" and res.name_source == "unchanged"
    person = (await reader.get(f"/api/people/{pid}")).json()
    assert person["name"] == "André Niño" and person["record_kind"] == "Person"
    assert [e["v"] for e in person["history"]["name"]] == ["andrÃ© niÃ±o", "André Niño"]
    assert person["history"]["name"][-1]["src"] == {"kind": "system", "by": "repair"}
    assert res.credits == 0
    # a company recorded as a person
    pid = await make(writer, {"name": "Light Node Ventures", "website": "https://lightnode.vc"})
    res = await repair(pid, src)
    assert res.kind == "Company" and (await reader.get(f"/api/people/{pid}")).json()["record_kind"] == "Company"
    # an email as the name: the local part gives the full name (evidence), nothing invented
    pid = await make(writer, {"name": "polly.israni@gmail.com", "email": "polly.israni@gmail.com"})
    res = await repair(pid, src)
    assert res.name_source == "evidence" and res.kind == "Person"
    person = (await reader.get(f"/api/people/{pid}")).json()
    assert person["name"] == "Polly Israni" and person["history"]["name"][0]["v"] == "polly.israni@gmail.com"
    assert person["history"]["name"][-1]["src"] == src.doc()
    # a misfiled value moves to notes and is tombstoned where it was
    pid = await make(writer, {"name": "Dana Whitfield", "website": "previously marketing manager at Gap Inc.",
                              "notes": "met in June"})
    res = await repair(pid, src)
    by = {(c.key, c.action): c for c in res.changes}
    assert by[("website", "removed")].previous == "previously marketing manager at Gap Inc."
    assert by[("notes", "set")].value == "met in June\npreviously marketing manager at Gap Inc."
    person = (await reader.get(f"/api/people/{pid}")).json()
    assert "website" not in person and person["history"]["website"][-1]["v"] is None


async def test_repair_judges_kind_by_common_sense_without_asking_anyone(writer, reader):
    """The repair tool never calls a data provider. A human name, even a first name alone, is a
    Person; Unidentifiable is for records with nothing identifying on them."""
    src = Source("agent", "repair", "test")
    pid = await make(writer, {"name": "Ada", "linkedin": "https://www.linkedin.com/in/adaokafor"})
    res = await repair(pid, src)
    assert res.kind == "Person" and res.credits == 0 and res.calls == []
    person = (await reader.get(f"/api/people/{pid}")).json()
    assert person["name"] == "Ada" and "linkedin_headline" not in person
    pid = await make(writer, {"name": "Vlad", "email": "vlad@example.com"})
    assert (await repair(pid, src)).kind == "Person"
    pid = await make(writer, {"name": "Store eo39uo"})
    assert (await repair(pid, src)).kind == "Unidentifiable"
    assert (await reader.get(f"/api/people/{pid}")).json()["record_kind"] == "Unidentifiable"

async def test_repair_validator_refuses_invented_names_and_allocates_kinds(writer, reader):
    src = Source("agent", "repair", "test")
    pid = await make(writer, {"name": "Sam", "bio": "Runs a bakery."})

    def inventor(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "kind": "Person", "name": "Sam Baker", "name_complete": True, "confidence": "high"})])

    with repair_tool.agent.override(model=FunctionModel(inventor)), pytest.raises(AgentRunError):
        await repair(pid, src)
    assert (await reader.get(f"/api/people/{pid}")).json()["name"] == "Sam"

    def stubborn(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "kind": "Unidentifiable", "name": None, "name_complete": False, "confidence": "high"})])

    # a full name behind a LinkedIn profile URL is a person, whatever an earlier mark said
    pid = await make(writer, {"name": "Eleanor Qin", "linkedin": "https://www.linkedin.com/in/eqin",
                              "record_kind": "Unidentifiable"})
    with repair_tool.agent.override(model=FunctionModel(stubborn)):
        res = await repair(pid, src)
    assert res.kind == "Person" and "identify a person" in res.note
    assert (await reader.get(f"/api/people/{pid}")).json()["record_kind"] == "Person"

    def fund(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "kind": "Fund", "name": None, "name_complete": True, "confidence": "high"})])

    pid = await make(writer, {"name": "Sequoia Scout Fund III", "website": "https://sequoia.com"})
    with repair_tool.agent.override(model=FunctionModel(fund)):
        res = await repair(pid, src, repair_tool.RepairOptions(allocate=False))
        assert res.kind == "Person" and res.rejected == ["kind 'Fund' (allocation off)"]
        res = await repair(pid, src)
    assert res.kind == "Fund" and ("record_kind", "enum_value_added") in {(c.key, c.action) for c in res.changes}
    assert "Fund" in await properties.enum_values("record_kind")


async def test_normalize_repairs_once_unless_forced(people_by_name, writer, reader):
    cy = people_by_name["Cy"]
    res = (await writer.post(f"/api/people/{cy}/normalize?wait=true")).json()
    assert res["repair"]["kind"] == "Person"                   # "Cy": a human first name is enough
    assert (await reader.get(f"/api/people/{cy}")).json()["record_kind"] == "Person"
    res = (await writer.post(f"/api/people/{cy}/normalize?wait=true")).json()
    assert res["repair"] is None                               # already classified
    res = (await writer.post(f"/api/people/{cy}/normalize?wait=true", json={"options": {"force": True}})).json()
    assert res["repair"]["kind"] == "Person" and res["repair"]["agent_called"]
    res = (await writer.post(f"/api/people/{cy}/normalize?wait=true", json={"options": {"repair": False, "force": True}})).json()
    assert res["repair"] is None


async def test_normalize_assistant_run(people_by_name, writer, reader):
    r = await writer.post("/api/assistants/normalize/runs",
                          json={"person_ids": [people_by_name["Cy"], people_by_name["Ada"]],
                                "options": {"enrich": False}})
    assert r.status_code == 201, r.text
    await runner.drain()
    run = (await reader.get(f"/api/assistants/runs/{r.json()['id']}")).json()
    assert run["status"] == "done" and run["done"] == 2 and run["credits_used"] == 0
    by_person = {i["person_id"]: i for i in run["items"]}
    cy = by_person[people_by_name["Cy"]]["result"]
    assert cy["text"] == "stage → Pre-seed; record kind: Person (agent consulted)."
    assert by_change(cy)[("stage", "normalized")]["value"] == "Pre-seed"
    assert cy["repair"]["kind"] == "Person"
    ada = by_person[people_by_name["Ada"]]["result"]
    assert ada["agent_called"] is False and ada["text"] == "record kind: Person (agent consulted)."
    person = (await reader.get(f"/api/people/{people_by_name['Cy']}")).json()
    assert person["history_norm"]["stage"][-1]["src"]["by"] == "normalize/normalizer"
    specs = {w["key"]: w for w in (await reader.get("/api/assistants")).json()}
    assert specs["normalize"]["selector_field"] == "name"


# --- MCP parity ---------------------------------------------------------------------------------------

async def test_mcp_tools(people_by_name, writer, reader, client_factory):
    w = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    rk = (await reader.post("/api/api-keys", json={"name": "r"})).json()["key"]
    c = await client_factory()
    await init(c, rk)
    tools = {t["name"] for t in (await c.post("/mcp", headers={
        "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18", "Authorization": f"Bearer {rk}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})).json()["result"]["tools"]}
    assert {"search_people", "plan_search", "ask_people", "list_properties"} <= tools
    assert not {"store_about_person", "normalize_person", "apply_store_proposal"} & tools  # reader

    err, res = await call(c, rk, "plan_search", {"question": "payments founders with mrr over 10000"})
    assert not err, res
    assert res["request"]["filters"] == [{"field": "mrr", "op": "gte", "value": 10000}]
    err, res = await call(c, rk, "ask_people", {"question": "payments founders with mrr over 10000",
                                                "options": {"threshold": 0}})
    assert not err, res
    assert [h["person"]["name"] for h in res["results"]] == ["Ada"] and "Ada" in res["text"]
    err, _ = await call(c, rk, "store_about_person", {"id": people_by_name["Ada"], "passage": "x: y"})
    assert err

    await init(c, w)
    err, res = await call(c, w, "store_about_person", {
        "id": people_by_name["Bo"], "passage": "notes: raised a seed round", "agent": "scout"})
    assert not err, res
    assert by_change(res)[("notes", "set")]["value"] == "raised a seed round"
    bo = (await reader.get(f"/api/people/{people_by_name['Bo']}")).json()
    assert bo["history"]["notes"][-1]["src"] == {"kind": "mcp", "by": "wendy",
                                                 "ref": "key:1 agent:store/scout"}
    err, res = await call(c, w, "normalize_person", {"id": people_by_name["Cy"]})
    assert not err, res
    assert by_change(res)[("stage", "normalized")]["value"] == "Pre-seed"


# --- text values are not capped -----------------------------------------------------------------------

def test_text_values_are_stored_and_read_whole():
    """One lax cap (about 5000 words) on a text value; nothing below it clips or rejects text."""
    from app import fields
    from app.assistants.ask import Citation
    from app.assistants.enrich.assistant import _clip
    from app.routers.people import StoreBody
    from app.tools.store import Proposal, Write

    long_text = " ".join(f"word{i}" for i in range(4000))        # ~36k chars, under the cap
    assert fields.MAX_TEXT_CHARS >= len(long_text) > 30_000
    assert StoreBody(passage=long_text).passage == long_text
    assert _clip("  a   b  " + long_text) == "a b " + long_text   # whitespace folded, nothing cut
    assert Write(key="bio", value=long_text, evidence=long_text).evidence == long_text
    assert Proposal(writes=[Write(key="bio", value=long_text)]).writes[0].value == long_text
    assert Citation(person_id="x", field="bio", value=long_text).value == long_text
    assert repair_tool.signals({"name": "Ann Lee", "bio": long_text}) == []
