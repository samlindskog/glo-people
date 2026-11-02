"""Stage B: a human or API write is reviewed shortly after, wherever it came from; assistant and
import writes are not (their items review themselves)."""

import pytest

from app import embedder, people, wiring
from app.assistants import auto_review, runner
from app.config import get_settings
from app.mongo import db
from tests.conftest import import_people
from tests.test_mcp import call, init


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    monkeypatch.setattr(get_settings(), "fake_agents", True)
    monkeypatch.setattr(get_settings(), "auto_review", True)
    monkeypatch.setattr(auto_review, "DELAY_S", 3600.0)        # tests flush by hand
    monkeypatch.setattr(people, "_hooks", [])
    monkeypatch.setattr(wiring, "_registered", False)
    wiring.register_write_hooks()
    auto_review._pending.clear()
    yield
    if auto_review._task is not None:
        auto_review._task.cancel()
    auto_review._pending.clear()


async def _runs(client):
    return [r for r in (await client.get("/api/assistants/runs")).json()["items"]
            if r["label"] == auto_review.LABEL]


async def test_human_and_api_writes_are_reviewed_once(writer, client_factory):
    r = await writer.post("/api/people", json={"tier": "member", "fields": {
        "name": "Ada Okafor", "stage": "pre seed"}})
    pid = r.json()["id"]
    await writer.patch(f"/api/people/{pid}", json={"fields": {"notes": "met at demo day"}})
    key = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    err, other = await call(c, key, "create_person", {"tier": "plus_one", "fields": {"name": "Bo Chen"}})
    assert not err
    assert set(auto_review._pending) == {pid, other["id"]}       # collected once each, not per write

    run = await auto_review.flush()
    assert run is not None and run["workflow"] == "normalize" and run["label"] == auto_review.LABEL
    done = await runner.wait(run["id"])
    assert done["status"] == "done" and done["total"] == 2
    items = (await writer.get(f"/api/assistants/runs/{run['id']}")).json()["items"]
    assert {it["person_id"] for it in items} == {pid, other["id"]}
    ada = await db().people.find_one({"_id": people._oid(pid)})
    assert ada["record_kind"]["v"] == "Person" and ada["stage"]["norm"]["v"] is not None
    assert auto_review._pending == {} and await auto_review.flush() is None

    # the review's own writes (agent, system) did not queue anyone again
    assert auto_review._pending == {}
    # a tier-only change is not a reason to review
    await writer.patch(f"/api/people/{pid}", json={"fields": {}, "tier": "plus_one"})
    assert auto_review._pending == {}


async def test_assistant_and_import_writes_are_not_collected(writer):
    await import_people(writer, "Name,Email,Stage\nDee Evans,dee@y.io,seed\n", "crm.csv", "member")
    assert auto_review._pending == {}                            # merge reviews its own items
    pid = str((await db().people.find_one({"name.v": "Dee Evans"}))["_id"])
    r = await writer.post("/api/assistants/update/runs", json={
        "person_ids": [pid], "options": {"values": {"notes": "bulk"}, "review": False}})
    await runner.wait(r.json()["id"])
    assert auto_review._pending == {}                            # an assistant item, even as its starter
    assert (await writer.delete(f"/api/people/{pid}")).status_code == 204
    assert auto_review._pending == {}                            # a delete is nothing to review


async def test_setting_off_collects_nothing(writer, monkeypatch):
    monkeypatch.setattr(get_settings(), "auto_review", False)
    r = await writer.post("/api/people", json={"tier": "member", "fields": {"name": "Zed"}})
    assert r.status_code == 201 and auto_review._pending == {}
    assert await _runs(writer) == []
