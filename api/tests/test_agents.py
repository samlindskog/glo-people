"""The agent operations (ask, store, normalise one person) with deterministic fake models, their
REST and MCP surfaces, and the normalise workflow."""

import pytest
from pydantic_ai import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app import embedder, jobs, properties
from app.agents import planner
from app.agents.fakes import _retried
from app.config import get_settings
from app.mongo import db
from app.workflows import runner
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
    r = await writer.post("/api/people/import", data={"tier": "member"},
                          files={"file": ("a.csv", CSV.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    await jobs.drain()
    return {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}


# --- ask --------------------------------------------------------------------------------------------

async def test_ask_plans_runs_and_answers(people_by_name, reader):
    r = await reader.post("/api/search/ask", json={"question": "payments founders with mrr over 10000",
                                                   "options": {"threshold": 0.0}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["semantic"] == [{"field": "bio",
                                         "query": "payments founders with mrr over 10000"}]
    assert body["plan"]["filters"] == [{"field": "mrr", "op": "gte", "value": 10000}]
    names = [h["person"]["name"] for h in body["results"]]
    assert names == ["Ada"] and body["total"] == 1
    assert "Ada" in body["answer"]
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
    assert "No one" in r.json()["answer"]
    # options are validated
    assert (await reader.post("/api/search/ask", json={"question": "x", "options": {"k": 0}})
            ).status_code == 422


async def test_ask_invalid_plan_is_retried_then_fails_clearly(people_by_name, reader):
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

    with planner.agent.override(model=FunctionModel(flaky)):
        r = await reader.post("/api/search/ask", json={"question": "climate", "options": {"threshold": 0}})
    assert r.status_code == 200, r.text
    assert attempts == [False, True]
    assert r.json()["results"][0]["person"]["name"] == "Bo"

    def hopeless(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "semantic": [], "filters": [{"field": "stage", "op": "eq", "value": "Nonsense"}]})])

    with planner.agent.override(model=FunctionModel(hopeless)):
        r = await reader.post("/api/search/ask", json={"question": "anything"})
    assert r.status_code == 502 and "agent failed" in r.text


# --- store -------------------------------------------------------------------------------------------

async def test_store_files_facts_under_properties(people_by_name, writer, reader):
    ada = people_by_name["Ada"]
    r = await writer.post(f"/api/people/{ada}/store", json={
        "passage": "notes: follows up next week. mrr: 20k. sectors: payments; lending. "
                   "stage: seed round closed. bio: payments infrastructure for merchants. "
                   "Hi there. new favourite tool: lathe"})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["applied"] is True
    by = {(i["key"], i["action"]): i for i in rep["items"]}
    assert ("notes", "set") in by and by[("notes", "set")]["conflict"] is True   # replaces a value
    assert by[("mrr", "set")]["value"] == "20k" and by[("mrr", "set")]["conflict"] is True
    assert by[("sectors", "append")]["value"] == ["payments", "lending"]
    assert ("bio", "confirmed") in by                                           # restated, not written
    assert by[(None, "ignored")]["value"] == "Hi there"
    assert by[("favourite_tool", "created")]["reason"].startswith("new property")
    assert rep["created"] == ["favourite_tool"]
    assert rep["written"] == {"notes": "follows up next week", "mrr": "20k",
                              "sectors": ["payments", "lending"], "stage": "seed round closed",
                              "favourite_tool": "lathe"}
    # the clean-up pass mapped the enum text the parser could not read
    assert rep["normalized"]["mapped"] == {"stage": "Seed"} and rep["normalized"]["agent_called"]
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
    r = await writer.post(f"/api/people/{ada}/store", json={"passage": "sectors: Lending; ai"})
    assert r.json()["written"] == {"sectors": ["payments", "lending", "ai"]}
    # a near-duplicate new property is redirected to the existing one
    r = await writer.post(f"/api/people/{ada}/store", json={"passage": "new favourite tools: hammer",
                                                           "options": {"near_duplicate": 0.3}})
    item = r.json()["items"][0]
    assert item["action"] == "redirected" and item["key"] == "favourite_tool"
    assert "too close" in item["reason"]
    # beyond max_new the facts go to notes instead of a new property
    r = await writer.post(f"/api/people/{ada}/store", json={
        "passage": "new eye colour: green. new shoe size: 42", "options": {"max_new": 1}})
    actions = [(i["key"], i["action"]) for i in r.json()["items"]]
    assert actions == [("eye_colour", "created"), ("notes", "to_notes")]
    assert r.json()["written"]["notes"] == "Shoe size: 42"

    assert (await writer.post("/api/people/000000000000000000000000/store",
                              json={"passage": "x: y"})).status_code == 404
    assert (await reader.post(f"/api/people/{ada}/store", json={"passage": "x: y"})
            ).status_code == 403


async def test_store_dry_run_and_apply(people_by_name, writer, reader):
    bo = people_by_name["Bo"]
    before = (await reader.get(f"/api/people/{bo}")).json()
    r = await writer.post(f"/api/people/{bo}/store", json={
        "passage": "notes: wants intros to LPs. new favourite colour: teal",
        "options": {"dry_run": True}})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["applied"] is False and rep["written"] == {} and rep["normalized"] is None
    assert [(i["key"], i["action"]) for i in rep["items"]] == [("notes", "set"),
                                                              ("favourite_colour", "created")]
    assert (await reader.get(f"/api/people/{bo}")).json()["history"] == before["history"]
    assert await properties.get("favourite_colour") is None                   # nothing created

    # a program edits the proposal (drops the new property) and applies it
    proposal = rep["proposal"]
    proposal["new_properties"] = []
    r = await writer.post(f"/api/people/{bo}/store/apply", json={"proposal": proposal})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] and r.json()["written"] == {"notes": "wants intros to LPs"}
    # an invalid proposal is refused like the agent's would be
    bad = {"writes": [{"key": "nonexistent", "value": "x"}]}
    r = await writer.post(f"/api/people/{bo}/store/apply", json={"proposal": bad})
    assert r.status_code == 422 and "not a property" in r.text
    bad = {"writes": [{"key": "bio", "value": "x", "action": "append"}]}
    assert (await writer.post(f"/api/people/{bo}/store/apply", json={"proposal": bad})
            ).status_code == 422


async def test_store_from_file(people_by_name, writer, reader):
    bo = people_by_name["Bo"]
    r = await writer.post(f"/api/people/{bo}/store/file",
                          files={"file": ("call.md", b"notes: wants intros to LPs\n", "text/markdown")})
    assert r.status_code == 200, r.text
    assert r.json()["written"] == {"notes": "wants intros to LPs"}
    r = await writer.post(f"/api/people/{bo}/store/file",
                          files={"file": ("e.txt", b"   ", "text/plain")})
    assert r.status_code == 422


# --- normalise one person ----------------------------------------------------------------------------

async def test_normalize_person_parses_then_asks_the_agent(people_by_name, writer, reader):
    cy = people_by_name["Cy"]
    before = (await reader.get(f"/api/people/{cy}")).json()
    assert before["norm_flags"] == {"stage": "unmapped"}
    await db().people.update_one({"_id": (await db().people.find_one({"name.v": "Cy"}))["_id"]},
                                 {"$set": {"mrr": {"v": "lots", "h": [{"v": "lots"}]}}})
    r = await writer.post(f"/api/people/{cy}/normalize")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["parsed"] == ["mrr"]                      # the parser flagged the new text first
    assert rep["agent_called"] and rep["mapped"] == {"stage": "Pre-seed"}
    assert rep["non_normalizable"] == ["mrr"] and rep["unmapped"] == []
    after = (await reader.get(f"/api/people/{cy}")).json()
    assert after["norm"]["stage"] == "Pre-seed" and after["norm_flags"] == {"mrr": "non_normalizable"}
    entry = after["history_norm"]["stage"][-1]
    assert entry["src"] == {"kind": "agent", "by": "normalizer", "ref": "user:wendy"}
    # nothing left for the agent: a second call is free
    rep = (await writer.post(f"/api/people/{cy}/normalize")).json()
    assert not rep["agent_called"] and rep == {**rep, "parsed": [], "mapped": {}}
    # and the parser does not undo the agent on a later unrelated write
    await writer.patch(f"/api/people/{cy}", json={"fields": {"bio": "lending for SMBs"}})
    assert (await reader.get(f"/api/people/{cy}")).json()["norm"]["stage"] == "Pre-seed"
    assert (await reader.post(f"/api/people/{cy}/normalize")).status_code == 403


async def test_normalize_workflow(people_by_name, writer, reader):
    r = await writer.post("/api/workflows/normalize/runs",
                          json={"person_ids": [people_by_name["Cy"], people_by_name["Ada"]]})
    assert r.status_code == 201, r.text
    await runner.drain()
    run = (await reader.get(f"/api/workflows/runs/{r.json()['id']}")).json()
    assert run["status"] == "done" and run["done"] == 2 and run["credits_used"] == 0
    by_person = {i["person_id"]: i for i in run["items"]}
    assert by_person[people_by_name["Cy"]]["result"]["mapped"] == {"stage": "Pre-seed"}
    assert by_person[people_by_name["Ada"]]["result"]["agent_called"] is False
    cy = (await reader.get(f"/api/people/{people_by_name['Cy']}")).json()
    assert cy["history_norm"]["stage"][-1]["src"]["by"] == "normalize/normalizer"
    specs = {w["key"]: w for w in (await reader.get("/api/workflows")).json()}
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
    assert "ask_people" in tools and "store_about_person" not in tools   # reader: no write tools

    err, res = await call(c, rk, "ask_people", {"question": "payments founders with mrr over 10000",
                                                "options": {"threshold": 0}})
    assert not err, res
    assert [h["person"]["name"] for h in res["results"]] == ["Ada"] and "Ada" in res["answer"]
    err, _ = await call(c, rk, "store_about_person", {"id": people_by_name["Ada"], "passage": "x: y"})
    assert err

    await init(c, w)
    err, res = await call(c, w, "store_about_person", {
        "id": people_by_name["Bo"], "passage": "notes: raised a seed round", "agent": "scout"})
    assert not err, res
    assert res["written"] == {"notes": "raised a seed round"} and res["applied"]
    bo = (await reader.get(f"/api/people/{people_by_name['Bo']}")).json()
    assert bo["history"]["notes"][-1]["src"] == {"kind": "mcp", "by": "wendy",
                                                 "ref": "key:1 agent:store/scout"}
    err, res = await call(c, w, "store_about_person", {
        "id": people_by_name["Bo"], "passage": "notes: second call", "options": {"dry_run": True}})
    assert not err and not res["applied"]
    err, res = await call(c, w, "apply_store_proposal", {"id": people_by_name["Bo"],
                                                         "proposal": res["proposal"]})
    assert not err, res
    assert res["written"] == {"notes": "second call"}
    err, res = await call(c, w, "normalize_person", {"id": people_by_name["Cy"]})
    assert not err, res
    assert res["mapped"] == {"stage": "Pre-seed"}
