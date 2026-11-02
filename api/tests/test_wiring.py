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
    from app import embeddings
    from app.assistants import auto_review
    assert people._hooks == [embeddings.sync_person, auto_review.after_write]
