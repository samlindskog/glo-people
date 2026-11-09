"""The write observer: every path that changes a person's values — REST, MCP, a CSV row through
the merge assistant, a tool on a record — reports once to `people.on_write` with who wrote, and the
composition root registers the same observers in the API and the CLI process."""

import pytest

from app import embedder, people, wiring
from app.config import get_settings
from tests.conftest import import_people
from tests.test_mcp import call, init


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    monkeypatch.setattr(get_settings(), "fake_agents", True)


async def test_every_write_path_reports_once_with_its_source(writer, client_factory, monkeypatch):
    seen: list[people.WriteInfo] = []

    async def spy(info: people.WriteInfo) -> None:
        seen.append(info)

    monkeypatch.setattr(people, "_hooks", [*people._hooks, spy])

    r = await writer.post("/api/people", json={"tier": "member", "fields": {"name": "Zed", "bio": "robots"}})
    pid = r.json()["id"]
    assert [(i.src.kind, i.keys, i.is_new) for i in seen] == [("ui", ("bio", "name"), True)]

    seen.clear()
    assert (await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": "warehouse robots"}})).status_code == 200
    assert (await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": "warehouse robots"}})).status_code == 200
    assert [(i.src.kind, i.keys) for i in seen] == [("ui", ("bio",))]          # a no-op write reports nothing

    seen.clear()
    r = await writer.post(f"/api/people/{pid}/store?wait=true", json={"passage": "notes: met at demo day"})
    assert r.status_code == 200, r.text
    assert [(i.src.kind, i.keys) for i in seen] == [("ui", ("notes",))]   # filed for the user who asked

    seen.clear()
    await import_people(writer, "Name,Email,Bio\nAda,ada@x.io,payments\nZed,,robots\n", "p.csv", "member")
    kinds = sorted((i.src.kind, i.is_new) for i in seen)
    assert kinds == [("import", False), ("import", True)]   # Zed matched by name, Ada created

    seen.clear()
    key = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    err, _ = await call(c, key, "update_person", {"id": pid, "fields": {"bio": "robots for warehouses"}})
    assert not err
    assert [(i.src.kind, i.keys) for i in seen] == [("mcp", ("bio",))]

    seen.clear()
    r = await writer.post(f"/api/people/{pid}/normalize?wait=true", json={"options": {"repair": False}})
    assert r.status_code == 200
    assert seen == []                                  # a normalisation-only write changes no text

    seen.clear()
    assert (await writer.delete(f"/api/people/{pid}")).status_code == 204
    assert [(i.person_id, i.deleted, i.src) for i in seen] == [(pid, True, None)]


def test_registration_is_shared_and_idempotent(monkeypatch):
    monkeypatch.setattr(people, "_hooks", [])
    monkeypatch.setattr(wiring, "_registered", False)
    wiring.register_write_hooks()
    wiring.register_write_hooks()
    from app import boards, embeddings
    from app.assistants import auto_review
    assert people._hooks == [embeddings.sync_person, boards.sync_person, auto_review.after_write]


async def test_hooks_run_after_the_write_in_order_in_the_writers_context(monkeypatch, writer):
    """Off the test suite's inline mode a commit queues its hooks: the request returns before
    they run, one worker runs them in commit order in a copy of the writer's context, a failing
    hook costs the others nothing, and `flush_writes` waits for the queue."""
    import asyncio

    from app.assistants import runner

    monkeypatch.setattr(people, "HOOKS_INLINE", False)
    monkeypatch.setattr(people, "_pending", None)
    monkeypatch.setattr(people, "_worker", None)
    seen: list[tuple[str, object]] = []
    gate = asyncio.Event()

    async def slow(info):
        await gate.wait()
        seen.append(("slow", info.keys))

    async def broken(info):
        raise RuntimeError("vector store down")

    async def spy(info):
        seen.append(("spy", runner.CURRENT_ITEM.get()))

    monkeypatch.setattr(people, "_hooks", [slow, broken, spy])
    r = await writer.post("/api/people", json={"tier": "member", "fields": {"name": "Zed", "bio": "a"}})
    pid = r.json()["id"]
    assert (await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": "b"}})).status_code == 200
    assert seen == [] and people.pending_writes() >= 1          # the requests came back first
    with pytest.raises(TimeoutError):
        await people.flush_writes(timeout=0.05)
    token = runner.CURRENT_ITEM.set("item-7")                   # an assistant's item writing
    try:
        await people._after_write(people.WriteInfo(pid, None, ("notes",)))
    finally:
        runner.CURRENT_ITEM.reset(token)
    gate.set()
    await people.flush_writes(timeout=5)
    assert seen == [("slow", ("bio", "name")), ("spy", None), ("slow", ("bio",)), ("spy", None),
                    ("slow", ("notes",)), ("spy", "item-7")]    # commit order; the item's context kept
    assert people.pending_writes() == 0
    await people.flush_writes()                                 # nothing queued: returns at once

    # a worker that died (cancelled at shutdown) does not make flush wait, or pass in silence
    gate.clear()
    await people._after_write(people.WriteInfo(pid, None, ("bio",)))
    await people._after_write(people.WriteInfo(pid, None, ("about",)))
    await asyncio.sleep(0)
    people._worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await people._worker
    assert people.pending_writes() == 1
    await people.flush_writes(timeout=1)                        # returns, warns, does not hang
    seen.clear()
    await people._after_write(people.WriteInfo(pid, None, ("role",)))   # a new worker takes over
    gate.set()
    await people.flush_writes(timeout=5)
    assert [k for _, k in seen if _ == "slow"] == [("role",)]
