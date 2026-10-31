"""The tools (plan, store, normalize) and the ask assistant with deterministic fake models, their
REST and MCP surfaces."""

import pytest
from pydantic_ai import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app import embedder, jobs, properties
from app.agents.fakes import _retried
from app.assistants import runner
from app.config import get_settings
from app.mongo import db
from app.tools import plan as plan_tool
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
    r = await writer.post(f"/api/people/{ada}/store", json={
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
    r = await writer.post(f"/api/people/{ada}/store", json={"passage": "sectors: Lending; ai"})
    assert by_change(r.json())[("sectors", "append")]["value"] == ["payments", "lending", "ai"]
    # a near-duplicate new property is redirected to the existing one
    r = await writer.post(f"/api/people/{ada}/store", json={"passage": "new favourite tools: hammer",
                                                           "options": {"near_duplicate": 0.3}})
    assert r.json()["redirected"] == {"favourite_tools": "favourite_tool"}
    assert by_change(r.json())[("favourite_tool", "set")] == {
        "key": "favourite_tool", "action": "set", "value": "hammer", "previous": "lathe", "flag": None}
    # beyond max_new the facts go to notes instead of a new property
    r = await writer.post(f"/api/people/{ada}/store", json={
        "passage": "new eye colour: green. new shoe size: 42", "options": {"max_new": 1}})
    assert r.json()["created_properties"] == ["eye_colour"] and r.json()["to_notes"] == ["Shoe size"]
    assert by_change(r.json())[("notes", "set")]["value"] == "Shoe size: 42"

    assert (await writer.post("/api/people/000000000000000000000000/store",
                              json={"passage": "x: y"})).status_code == 404
    assert (await reader.post(f"/api/people/{ada}/store", json={"passage": "x: y"})
            ).status_code == 403
    assert (await writer.post(f"/api/people/{ada}/store/apply", json={})).status_code in (404, 405)


async def test_store_from_file(people_by_name, writer, reader):
    bo = people_by_name["Bo"]
    r = await writer.post(f"/api/people/{bo}/store/file",
                          files={"file": ("call.md", b"notes: wants intros to LPs\n", "text/markdown")})
    assert r.status_code == 200, r.text
    assert by_change(r.json())[("notes", "set")]["value"] == "wants intros to LPs"
    r = await writer.post(f"/api/people/{bo}/store/file",
                          files={"file": ("e.txt", b"   ", "text/plain")})
    assert r.status_code == 422


# --- normalise one person ----------------------------------------------------------------------------

async def test_normalize_parses_then_asks_the_agent(people_by_name, writer, reader):
    cy = people_by_name["Cy"]
    before = (await reader.get(f"/api/people/{cy}")).json()
    assert before["norm_flags"] == {"stage": "unmapped"}
    await db().people.update_one({"_id": (await db().people.find_one({"name.v": "Cy"}))["_id"]},
                                 {"$set": {"mrr": {"v": "lots", "h": [{"v": "lots"}]}}})
    r = await writer.post(f"/api/people/{cy}/normalize")
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
    res = (await writer.post(f"/api/people/{cy}/normalize")).json()
    assert not res["agent_called"] and res["changes"] == []
    # and the parser does not undo the agent on a later unrelated write
    await writer.patch(f"/api/people/{cy}", json={"fields": {"bio": "lending for SMBs"}})
    assert (await reader.get(f"/api/people/{cy}")).json()["norm"]["stage"] == "Pre-seed"
    assert (await reader.post(f"/api/people/{cy}/normalize")).status_code == 403


async def test_normalize_assistant_run(people_by_name, writer, reader):
    r = await writer.post("/api/assistants/normalize/runs",
                          json={"person_ids": [people_by_name["Cy"], people_by_name["Ada"]]})
    assert r.status_code == 201, r.text
    await runner.drain()
    run = (await reader.get(f"/api/assistants/runs/{r.json()['id']}")).json()
    assert run["status"] == "done" and run["done"] == 2 and run["credits_used"] == 0
    by_person = {i["person_id"]: i for i in run["items"]}
    cy = by_person[people_by_name["Cy"]]["result"]
    assert cy["text"] == "stage → Pre-seed (agent consulted)."
    assert by_change(cy)[("stage", "normalized")]["value"] == "Pre-seed"
    ada = by_person[people_by_name["Ada"]]["result"]
    assert ada["agent_called"] is False and ada["text"] == "Everything was already normalised."
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
