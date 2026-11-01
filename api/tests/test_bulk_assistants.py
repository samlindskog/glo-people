"""The assistants that run over the database or answer in one item: merge (a CSV's rows, one item
per person), embed, reparse and ask as runs; the CLI driving a run in its own process; enrich steps that are
not paid twice after an interruption."""

import pytest
from sqlalchemy import select

from app import db as pg
from app import people
from app.assistants import runner
from app.config import get_settings
from app.history import Source
from app.mongo import db
from tests.test_assistants import PEOPLE, FakeFirecrawl, written
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


async def _vectors(field: str = "bio") -> int:
    async with pg.engine().connect() as conn:
        rows = (await conn.execute(select(pg.field_embeddings.c.field))).scalars().all()
    return rows.count(field)


async def test_import_is_a_merge_run_with_one_item_per_person(writer, reader, monkeypatch):
    monkeypatch.setattr(get_settings(), "fake_agents", True)
    res = await _import(writer, CSV, "a.csv")
    assert res["rows"] == 3 and res["skipped"] == 0 and res["new_properties"] == []
    run = res["run"]
    assert run["workflow"] == "merge" and run["total"] == 3 and run["done"] == 3
    assert run["label"] == "a.csv"
    items = (await reader.get(f"/api/assistants/runs/{run['id']}")).json()["items"]
    ada = next(it for it in items if it["label"] == "Ada")
    assert ada["person_id"] and ada["result"]["created"] and ada["result"]["match"]["decision"] == "new"
    assert ada["result"]["written"] == ["bio", "mrr", "name", "notes", "stage"]
    assert ada["result"]["review"]["kind"] == "Person"          # the closing review ran (fakes)
    assert ada["result"]["text"].startswith("Created Ada (member); wrote bio, mrr, name, notes, stage")
    assert await db().people.count_documents({}) == 3 and await _vectors("bio") == 3   # write hooks
    # a second import of the same file matches by the file's ids, nothing duplicated
    res = await _import(writer, CSV.replace("met at demo day", "met at the summit"), "a.csv")
    assert res["run"]["done"] == 3
    items = (await reader.get(f"/api/assistants/runs/{res['run']['id']}")).json()["items"]
    ada = next(it for it in items if it["label"] == "Ada")
    assert not ada["result"]["created"] and ada["result"]["match"]["tier"] == "identifier"
    assert ada["result"]["written"] == ["notes"]
    assert await db().people.count_documents({}) == 3
    doc = await db().people.find_one({"name.v": "Ada"})
    assert [e["v"] for e in doc["notes"]["h"]] == ["met at demo day", "met at the summit"]
    acts = (await writer.get("/api/activity?action=people.import")).json()["items"]
    assert acts[0]["meta"]["rows"] == 3 and acts[0]["meta"]["run_id"] == res["run"]["id"]
    assert (await writer.get("/api/jobs")).status_code == 404        # the jobs list is gone
    assert (await reader.post("/api/people/import", data={"tier": "member"},
                              files={"file": ("a.csv", CSV.encode(), "text/csv")})).status_code == 403


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
    from app import cli
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


async def test_enrich_does_not_pay_twice_for_a_step_already_on_the_record(writer, reader):
    # the LinkedIn step ran and its facts were filed (entries by enrich/linkedin_reader and
    # enrich/storage are on the record) but the item never finished: a redo skips the paid step
    # instead of spending 5 credits again
    r = await writer.post("/api/people/import", data={"tier": "member"},
                          files={"file": ("p.csv", PEOPLE.encode(), "text/csv")})
    assert r.status_code == 201, r.text
    await runner.wait(r.json()["run_id"])
    ada = str((await db().people.find_one({"name.v": "Ada Okafor"}))["_id"])
    await people.update(None, ada, {"linkedin_headline": "CEO at PayRail"},
                        source=Source("agent", "enrich/linkedin_reader", "run:0/item:0"))
    await people.update(None, ada, {"enrichment_passage": "# filed earlier"},
                        source=Source("agent", "enrich/storage", "run:0/item:0"))
    FakeFirecrawl.calls.clear()
    r = await writer.post("/api/assistants/enrich/runs", json={"person_ids": [ada]})
    run = await runner.wait(r.json()["id"])
    item = (await reader.get(f"/api/assistants/runs/{run['id']}")).json()["items"][0]
    assert item["status"] == "done"
    steps = {s["step"]: s["reason"] for s in item["result"]["skipped"]}
    assert "already" in steps["linkedin"]
    assert not any(c[0] == "fullenrich/people/match" for c in FakeFirecrawl.calls)
    assert "linkedin_headline" not in written(item["result"])


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
