"""The assistants that run over the database or answer in one item: embed, reparse and ask as runs;
the assistant listing, selectors and the adoption of runs left by a dead worker; the CLI driving a run in its own process; enrich steps that are
not paid twice after an interruption."""

import pytest
from sqlalchemy import select

from app import db as pg
from app import people
from app.assistants import runner
from app.config import get_settings
from app.mongo import db
from tests.conftest import import_people
from tests.test_assistants import PEOPLE
from tests.test_assistants import _fakes as _fakes  # noqa: PLC0414  (fake Firecrawl, site + company agents)
from tests.test_mcp import call, init
from tests.test_tools import CSV


async def _import(client, text: str, name: str, tier: str = "member") -> dict:
    r = await client.post("/api/people/import", data={"tier": tier},
                          files={"file": (name, text.encode(), "text/csv")})
    assert r.status_code == 201, r.text
    body = r.json()
    run = await runner.wait(body["run_id"])
    assert run["status"] == "done", run
    return {**body, "run": run}


@pytest.fixture
async def enrich_people(writer):
    await import_people(writer, PEOPLE, "p.csv", "member")
    return {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}


async def _vectors(field: str = "bio") -> int:
    async with pg.engine().connect() as conn:
        rows = (await conn.execute(select(pg.field_embeddings.c.field))).scalars().all()
    return rows.count(field)




async def test_embed_and_reparse_run_in_batches(writer, reader, monkeypatch):
    monkeypatch.setattr(get_settings(), "fake_agents", True)
    await _import(writer, CSV, "a.csv")
    ts = {"v": "$5k", "h": [{"v": "$5k"}]}
    await db().people.insert_one({"name": {"v": "Old", "h": [{"v": "Old"}]}, "mrr": ts,
                                  "tier": "member", "schema_version": 2})
    # reparse: the parser over everyone, in batches, writes only what changes
    r = await writer.post("/api/assistants/reparse/runs", json={"options": {"batch": 2}})
    assert r.status_code == 201, r.text
    run = await runner.wait(r.json()["id"])
    assert run["status"] == "done" and run["total"] == 2 and run["done"] == 2
    items = (await reader.get(f"/api/assistants/runs/{run['id']}")).json()["items"]
    assert sum(it["result"]["written"] for it in items) == 1
    assert (await db().people.find_one({"name.v": "Old"}))["mrr"]["norm"]["v"] == 5000
    # embed: every person's passages, in batches; nothing to do the second time
    async with pg.engine().begin() as conn:
        await conn.execute(pg.field_embeddings.delete())
    r = await writer.post("/api/assistants/embed/runs", json={"options": {"batch": 3}})
    assert r.status_code == 201, r.text
    run = await runner.wait(r.json()["id"])
    assert run["status"] == "done" and run["total"] == 2
    items = (await reader.get(f"/api/assistants/runs/{run['id']}")).json()["items"]
    assert sum(it["result"]["vectors"] for it in items) == 4 and await _vectors("bio") == 3
    assert items[0]["label"].startswith("people 1")
    r = await writer.post("/api/assistants/embed/runs", json={"options": {"fields": ["bio"]}})
    run = await runner.wait(r.json()["id"])
    assert sum(it["result"]["vectors"] for it in
               (await reader.get(f"/api/assistants/runs/{run['id']}")).json()["items"]) == 0
    specs = {s["key"]: s for s in (await reader.get("/api/assistants")).json()}
    assert specs["embed"]["scope"] == "batches" and specs["reparse"]["scope"] == "batches"
    assert specs["merge"]["scope"] == "batches" and specs["ask"]["scope"] == "single"


async def test_ask_is_a_run_with_the_answer_as_its_item(writer, reader, monkeypatch):
    monkeypatch.setattr(get_settings(), "fake_agents", True)
    await _import(writer, CSV, "a.csv")
    r = await writer.post("/api/assistants/ask/runs", json={
        "options": {"question": "payments founders with mrr over 10000", "threshold": 0}})
    assert r.status_code == 201, r.text
    run = await runner.wait(r.json()["id"])
    assert run["status"] == "done" and run["total"] == 1 and run["label"].startswith("payments")
    item = (await reader.get(f"/api/assistants/runs/{run['id']}")).json()["items"][0]
    res = item["result"]
    assert "Ada" in res["text"] and res["total"] == 1 and res["changes"] == []
    assert res["citations"][0]["field"] and res["request"]["filters"] == [
        {"field": "mrr", "op": "gte", "value": 10000}]
    assert res["search_id"] and (await writer.get(f"/api/searches/{res['search_id']}")).status_code == 200
    kinds = [e["kind"] for e in await runner.events(run["id"])]
    assert kinds.count("step") >= 3 and "agent" in kinds
    # a reader may ask too (it writes nothing)
    r = await reader.post("/api/assistants/ask/runs", json={"options": {"question": "climate investors"}})
    assert r.status_code == 201, r.text
    assert (await runner.wait(r.json()["id"]))["status"] == "done"
    # the question is required
    assert (await writer.post("/api/assistants/ask/runs", json={"options": {}})).status_code == 422


async def test_cli_drives_a_run_in_its_own_process(writer, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "fake_agents", True)
    from app import cli, wiring
    # the CLI is its own process: it must register the write observers itself, or its runs would
    # create people without vectors (the test process inherited the API's registration; drop it)
    monkeypatch.setattr(people, "_hooks", [])
    monkeypatch.setattr(wiring, "_registered", False)
    path = tmp_path / "people.csv"
    path.write_text(CSV)
    await cli.import_file(str(path), "member", dry_run=False, agent=True, review=False)
    out = capsys.readouterr().out
    assert "3 created, 0 matched" in out and "run " in out and "done" in out
    assert await db().people.count_documents({}) == 3 and await _vectors("bio") == 3
    await cli.normalize_people()
    assert "reparse" in capsys.readouterr().out
    await cli.embed(None)
    assert "embed" in capsys.readouterr().out
    runs = (await writer.get("/api/assistants/runs")).json()["items"]
    assert [r["workflow"] for r in runs] == ["embed", "reparse", "merge"]
    assert all(r["status"] == "done" for r in runs)




async def test_mcp_starts_a_batch_assistant_and_ask(writer, reader, client_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "fake_agents", True)
    await _import(writer, CSV, "a.csv")
    w = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    c = await client_factory()
    await init(c, w)
    err, run = await call(c, w, "start_assistant", {"key": "reparse", "options": {"batch": 1}})
    assert not err, run
    assert run["total"] == 3 and (await runner.wait(run["id"]))["status"] == "done"
    err, run = await call(c, w, "start_assistant", {"key": "ask", "options": {
        "question": "payments founders with mrr over 10000", "threshold": 0}})
    assert not err, run
    assert (await runner.wait(run["id"]))["status"] == "done"
    err, got = await call(c, w, "get_run", {"run_id": run["id"]})
    assert not err and got["items"][0]["status"] == "done"
    err, _ = await call(c, w, "start_assistant", {"key": "merge"})
    assert err                                                    # records (or the CSV upload) needed
    err, run = await call(c, w, "start_assistant", {"key": "merge", "options": {"review": False},
                                                    "records": [{"name": "Ada", "stage": "seed"}]})
    assert not err, run
    assert (await runner.wait(run["id"]))["status"] == "done"
    err, got = await call(c, w, "get_run", {"run_id": run["id"]})
    assert not err and got["items"][0]["status"] == "done" and got["items"][0]["person_id"]


@pytest.mark.parametrize("key", ["embed", "reparse"])
async def test_batch_assistants_refuse_an_empty_database(writer, key):
    r = await writer.post(f"/api/assistants/{key}/runs", json={})
    assert r.status_code == 422 and "no people" in r.text


async def test_workflow_listing_and_permissions(reader, writer):
    specs = {s["key"]: s for s in (await reader.get("/api/assistants")).json()}
    assert specs["enrich"]["selector_field"] == "linkedin" and specs["enrich"]["scope"] == "people"
    assert "refresh" in specs["enrich"]["options_schema"]["properties"]
    r = await reader.post("/api/assistants/enrich/runs", json={"person_ids": ["x"]})
    assert r.status_code == 403
    assert (await reader.get("/api/assistants/runs")).json() == {"items": [], "has_more": False}
    r = await writer.post("/api/assistants/enrich/runs", json={"person_ids": []})
    assert r.status_code == 422
    r = await writer.post("/api/assistants/enrich/runs", json={"person_ids": ["x"],
                                                              "options": {"max_credits": 0}})
    assert r.status_code == 422
    r = await writer.post("/api/assistants/nope/runs", json={"person_ids": ["x"]})
    assert r.status_code == 404


async def test_bulk_selector_and_listing(writer, enrich_people):
    r = await writer.post("/api/assistants/enrich/runs", json={"all_with_field": "linkedin"})
    assert r.status_code == 201 and r.json()["total"] == 3
    await runner.drain()
    runs = (await writer.get("/api/assistants/runs")).json()["items"]
    assert runs[0]["id"] == r.json()["id"] and runs[0]["username"] == "wendy"
    assert runs[0]["status"] == "done" and runs[0]["done"] == 3
    acts = (await writer.get("/api/activity?action=assistant.run")).json()["items"]
    assert acts[0]["meta"]["people"] == 3 and acts[0]["meta"]["assistant"] == "enrich"
    item = (await writer.get(f"/api/assistants/runs/{r.json()['id']}")).json()["items"][0]
    assert item["result"]["text"].startswith("Linkedin profile matched") or "LinkedIn" in item["result"]["text"]
    assert all({"key", "action", "value", "previous", "flag"} <= set(c) for c in item["result"]["changes"])


async def test_a_run_left_by_a_dead_worker_is_adopted_and_continues(writer, enrich_people):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import insert

    from app import db as pg
    stale = datetime.now(UTC) - timedelta(hours=1)
    async with pg.engine().begin() as conn:
        rid = (await conn.execute(insert(pg.workflow_runs).values(
            workflow="enrich", status="running", total=2, worker="api:999", heartbeat_at=stale,
            params={"options": {}}).returning(pg.workflow_runs.c.id))).scalar_one()
        await conn.execute(insert(pg.workflow_run_items).values([
            {"run_id": rid, "workflow": "enrich", "person_id": enrich_people["Cy Diaz"],
             "status": "running", "attempts": 1, "label": "Cy Diaz"},
            {"run_id": rid, "workflow": "enrich", "person_id": enrich_people["Dee Evans"],
             "status": "queued", "attempts": 0, "label": "Dee Evans"}]))
        # a CLI-owned run with a fresh heartbeat is left alone
        other = (await conn.execute(insert(pg.workflow_runs).values(
            workflow="enrich", status="running", total=0, worker="cli:4", heartbeat_at=datetime.now(UTC),
            params={"options": {}}).returning(pg.workflow_runs.c.id))).scalar_one()
    adopted = await runner.adopt(all_api_runs=True)
    assert adopted == [rid]
    run = await runner.wait(rid)
    assert run["status"] == "done" and run["done"] == 2 and run["worker"].startswith("api:")
    items = {it["person_id"]: it for it in (await writer.get(f"/api/assistants/runs/{rid}")).json()["items"]}
    assert items[enrich_people["Cy Diaz"]]["status"] == "done" and items[enrich_people["Cy Diaz"]]["attempts"] == 2
    messages = [e["message"] for e in await runner.events(rid)]
    assert any(m.startswith("adopted by api:") and "1 item(s) requeued" in m for m in messages)
    assert (await writer.get(f"/api/assistants/runs/{other}")).json()["worker"] == "cli:4"
    assert (await writer.post(f"/api/assistants/runs/{rid}/cancel")).status_code == 409
    # an item interrupted too often fails instead of running forever
    async with pg.engine().begin() as conn:
        rid2 = (await conn.execute(insert(pg.workflow_runs).values(
            workflow="enrich", status="running", total=1, worker=None,
            params={"options": {}}).returning(pg.workflow_runs.c.id))).scalar_one()
        await conn.execute(insert(pg.workflow_run_items).values(
            run_id=rid2, workflow="enrich", person_id=enrich_people["Cy Diaz"], status="running",
            attempts=3, label="Cy Diaz"))
    assert await runner.adopt(all_api_runs=False) == [rid2]
    run = await runner.wait(rid2)
    assert run["status"] == "done" and run["failed"] == 1
    item = (await writer.get(f"/api/assistants/runs/{rid2}")).json()["items"][0]
    assert item["status"] == "failed" and item["error"] == "interrupted repeatedly"
