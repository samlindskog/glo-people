"""Run selectors (`from_run`, `where`), the `update` assistant (the same edit on many people) and
the MCP ergonomics an agent relies on after an upload: `find_people`, `start_assistant` with
`from_run` and `wait_seconds`, compact run views, compact person reads, projected search results."""

import pytest

from app import embedder
from app.assistants import runner
from app.config import get_settings
from app.mongo import db
from tests.conftest import import_people
from tests.test_mcp import call, init

CSV = """Name,Email,Sectors,Stage,Bio
Ada Okafor,ada@x.io,fintech,seed,payments
Bo Chen,bo@x.io,climate,Series A,farming
Cy Diaz,cy@x.io,,,health
"""


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    monkeypatch.setattr(get_settings(), "fake_agents", True)


async def _start(client, key, **body):
    r = await client.post(f"/api/assistants/{key}/runs", json=body)
    assert r.status_code == 201, r.text
    run = await runner.wait(r.json()["id"])
    assert run["status"] == "done", run
    return (await client.get(f"/api/assistants/runs/{run['id']}")).json()


async def test_from_run_selects_the_people_a_merge_run_touched(writer):
    res = await import_people(writer, CSV, "p.csv", "member")
    ids = {str(d["_id"]) async for d in db().people.find({}, {"_id": 1})}
    dry = await _start(writer, "update", from_run=res["run_id"],
                       options={"tier": "plus_one", "dry_run": True})
    assert {it["person_id"] for it in dry["items"]} == ids and dry["params"]["from_run"] == res["run_id"]
    assert all(it["result"]["would"] == {"values": {}, "tier": "plus_one"} for it in dry["items"])
    assert await db().people.count_documents({"tier": "member"}) == 3        # a dry run writes nothing

    run = await _start(writer, "update", from_run=res["run_id"],
                       options={"tier": "plus_one", "review": False})
    assert all(it["result"]["text"] == "Wrote tier." for it in run["items"])
    assert await db().people.count_documents({"tier": "plus_one"}) == 3
    doc = await db().people.find_one({"name.v": "Ada Okafor"})
    assert doc["tier"] == "plus_one" and len(doc["name"]["h"]) == 1          # the tier is not a property

    # a run over a run over a run: the update run's people are the same three
    again = await _start(writer, "update", from_run=run["id"], options={"values": {"notes": "x"},
                                                                       "review": False})
    assert len(again["items"]) == 3
    assert (await writer.post("/api/assistants/update/runs", json={"from_run": 999_999})).status_code == 404


async def test_where_selects_matching_people_and_one_selector_is_required(writer):
    await import_people(writer, CSV, "p.csv", "member")
    run = await _start(writer, "update", where=[{"field": "stage", "op": "exists", "value": True}],
                       options={"values": {"notes": "has a stage"}, "review": False})
    assert sorted(it["label"] for it in run["items"]) == ["Ada Okafor", "Bo Chen"]
    assert run["params"]["where"] == [{"field": "stage", "op": "exists", "value": True}]
    run = await _start(writer, "update", where=[{"field": "sectors", "op": "eq", "value": "climate"}],
                       tier="member", options={"values": {"notes": "climate"}, "review": False})
    assert [it["label"] for it in run["items"]] == ["Bo Chen"]
    bo = await db().people.find_one({"name.v": "Bo Chen"})
    assert [e["v"] for e in bo["notes"]["h"]] == ["has a stage", "climate"]
    assert bo["notes"]["h"][-1]["src"] == {"kind": "ui", "by": "wendy"}      # the starter's own write

    ids = [str(bo["_id"])]
    r = await writer.post("/api/assistants/update/runs", json={"person_ids": ids, "from_run": 1})
    assert r.status_code == 422 and "exactly one" in r.text
    r = await writer.post("/api/assistants/update/runs", json={"options": {"tier": "member"}})
    assert r.status_code == 422
    r = await writer.post("/api/assistants/update/runs",
                          json={"where": [{"field": "notes", "op": "eq", "value": "nobody has this"}]})
    assert r.status_code == 422 and "no people" in r.text


async def test_update_sets_appends_removes_and_reviews(writer):
    await import_people(writer, CSV, "p.csv", "member")
    ada = str((await db().people.find_one({"name.v": "Ada Okafor"}))["_id"])
    run = await _start(writer, "update", person_ids=[ada], options={
        "values": {"sectors": ["lending"], "stage": "pre seed", "bio": None, "notes": "met at demo day"},
        "append": ["sectors"]})
    res = run["items"][0]["result"]
    assert res["written"] == ["bio", "notes", "sectors", "stage"] and res["kept"] == []
    assert res["review"]["kind"] == "Person"                 # the closing review ran (fakes)
    doc = await db().people.find_one({"_id": {"$exists": True}, "name.v": "Ada Okafor"})
    assert doc["sectors"]["v"] == ["fintech", "lending"] and doc["bio"]["v"] is None
    assert doc["stage"]["v"] == "pre seed" and doc["record_kind"]["v"] == "Person"
    # nothing to write
    r = await writer.post("/api/assistants/update/runs", json={"person_ids": [ada], "options": {}})
    run = await runner.wait(r.json()["id"])
    items = (await writer.get(f"/api/assistants/runs/{run['id']}")).json()["items"]
    assert items[0]["status"] == "skipped"


async def test_rest_find_people(writer):
    await import_people(writer, CSV, "p.csv", "member")
    r = await writer.get("/api/people/find", params={"email": "ADA@x.io", "name": "Someone Else"})
    body = r.json()
    assert body["decision"] == "matched" and body["tier"] == "identifier" and body["keys"] == ["email"]
    assert body["candidates"][0]["name"] == "Ada Okafor" and body["candidates"][0]["tier"] == "member"
    r = await writer.get("/api/people/find", params={"name": "Nobody"})
    assert r.json()["decision"] == "new"
    assert (await writer.get("/api/people/find")).status_code == 422


async def test_mcp_follow_up_after_an_upload(writer, client_factory):
    key = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    err, run = await call(c, key, "start_assistant", {
        "key": "merge", "records": [{"name": "Dee Evans", "email": "dee@y.io", "stage": "seed"},
                                    {"name": "Ed Fox", "email": "ed@y.io"}],
        "options": {"review": False}, "wait_seconds": 50})
    assert not err, run
    assert run["status"] == "done" and len(run["items"]) == 2
    first = run["items"][0]
    assert first["person_id"] and first["result"]["created"] is True
    assert first["result"]["match"] == {"decision": "new", "tier": None, "keys": []}
    assert "text" in first["result"] and "changes" not in first["result"]     # compact: no full result

    err, found = await call(c, key, "find_people", {"email": "dee@y.io"})
    assert not err and found["decision"] == "matched" and found["person_id"] == first["person_id"]
    err, _ = await call(c, key, "find_people", {})
    assert err

    err, upd = await call(c, key, "start_assistant", {
        "key": "update", "from_run": run["id"], "options": {"tier": "plus_one", "review": False},
        "wait_seconds": 50})
    assert not err, upd
    assert upd["status"] == "done" and {it["person_id"] for it in upd["items"]} == \
        {it["person_id"] for it in run["items"]}
    assert await db().people.count_documents({"tier": "plus_one"}) == 2

    err, got = await call(c, key, "get_run", {"run_id": upd["id"]})
    assert not err and got["items"][0]["result"]["text"] == "Wrote tier."

    err, person = await call(c, key, "get_person", {"id": first["person_id"], "include_history": False})
    assert not err and "history" not in person and person["name"] == "Dee Evans"
    err, person = await call(c, key, "get_person", {"id": first["person_id"]})
    assert not err and "history" in person

    err, res = await call(c, key, "search_people", {
        "filters": [{"field": "stage", "op": "eq", "value": "Seed"}], "fields": ["stage"]})
    assert not err and len(res["results"]) == 1
    assert set(res["results"][0]) <= {"id", "tier", "name", "score", "field_scores", "stage"}
