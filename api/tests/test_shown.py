"""Stage D end to end: which entry is shown. An upload does not displace an assistant's finding
(the owner's example: a CSV says Google, LinkedIn said Anthropic), a person's edit does; a kept
write is reported everywhere; restoring is writing the earlier value again; corrections are
derived; the backfill reports and applies flips."""

import pytest

from app import embedder, people
from app.config import get_settings
from app.history import Source
from app.mongo import db
from tests.conftest import import_people
from tests.test_mcp import call, init

AGENT = Source("agent", "enrich/linkedin_reader", "run:9/item:1")


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    monkeypatch.setattr(get_settings(), "fake_agents", True)


async def _agent_write(pid: str, values: dict) -> None:
    tx = await people.Transaction.open(pid)
    assert tx is not None
    await tx.set_values(values, AGENT)
    await tx.commit(None)


async def test_an_upload_does_not_displace_an_enriched_value(writer, reader):
    await import_people(writer, "Name,Email,Company\nAlex Ray,alex@x.io,Google\n", "crm.csv", "member")
    pid = str((await db().people.find_one({"name.v": "Alex Ray"}))["_id"])
    await _agent_write(pid, {"company": "Anthropic"})            # LinkedIn says he moved
    res = await import_people(writer, "Name,Email,Company\nAlex Ray,alex@x.io,Google\n", "crm.csv", "member")
    item = (await writer.get(f"/api/assistants/runs/{res['run_id']}")).json()["items"][0]
    assert item["result"]["kept"] == ["company"] and item["result"]["written"] == []
    assert "recorded but not shown: company" in item["result"]["text"]
    got = (await reader.get(f"/api/people/{pid}")).json()
    assert got["company"] == "Anthropic"
    hist = got["history"]["company"]
    assert [e["v"] for e in hist] == ["Google", "Anthropic", "Google"]   # recorded, not shown
    assert hist[1]["shown"] is True and "finding outranks" in hist[1]["why"]
    assert "shown" not in hist[2]
    acts = (await writer.get("/api/activity?action=person.update")).json()["items"]
    assert acts[0]["meta"].get("kept") == ["company"]

    # the same upload again repeats the last entry from a source that still loses: nothing added
    await import_people(writer, "Name,Email,Company\nAlex Ray,alex@x.io,Google\n", "crm.csv", "member")
    got = (await reader.get(f"/api/people/{pid}")).json()
    assert got["company"] == "Anthropic" and len(got["history"]["company"]) == 3


async def test_a_persons_edit_wins_and_restoring_is_writing_again(writer, reader):
    r = await writer.post("/api/people", json={"tier": "member", "fields": {
        "name": "James Brown", "stage": "Seed"}})
    pid = r.json()["id"]
    await _agent_write(pid, {"name": "James B", "stage": "Series A"})   # an assistant's claim
    got = (await reader.get(f"/api/people/{pid}")).json()
    assert got["name"] == "James Brown" and got["stage"] == "Seed"       # the person's edit holds
    assert got["norm"]["stage"] == "Seed"
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"stage": "Series A"}})
    assert r.json()["stage"] == "Series A" and r.json()["norm"]["stage"] == "Series A"
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"stage": "Seed"}})
    got = r.json()
    assert got["stage"] == "Seed" and got["norm"]["stage"] == "Seed"    # restored, norm follows
    assert [e["v"] for e in got["history"]["stage"]] == ["Seed", "Series A", "Series A", "Seed"]
    assert got["history"]["stage"][3]["shown"] and got["history"]["stage"][3]["why"] == "newest entry"


async def test_store_and_update_report_kept(writer):
    from app.tools import invoke
    from app.tools import store as store_tool

    r = await writer.post("/api/people", json={"tier": "member", "fields": {
        "name": "Ada Okafor", "company": "PayRail"}})
    pid = r.json()["id"]
    # an assistant's storage agent files a company the person did not state: recorded, not shown
    ctx = await invoke.make_context(pid, Source("agent", "enrich/storage", "run:1/item:1"), None)
    res = await invoke.run(store_tool.store, ctx, passage="company: Leafy")
    assert res.kept == ["company"]
    assert (await db().people.find_one({"_id": people._oid(pid)}))["company"]["v"] == "PayRail"
    # the person filing the same passage from the UI is their own edit: it shows
    r = await writer.post(f"/api/people/{pid}/store?wait=true", json={"passage": "company: Leafy"})
    assert r.status_code == 200 and r.json()["kept"] == []
    assert (await db().people.find_one({"_id": people._oid(pid)}))["company"]["v"] == "Leafy"
    # the update assistant writes as the person who started it: it shows
    r = await writer.post("/api/assistants/update/runs", json={
        "person_ids": [pid], "options": {"values": {"company": "Numix"}, "review": False}})
    from app.assistants import runner
    run = await runner.wait(r.json()["id"])
    items = (await writer.get(f"/api/assistants/runs/{run['id']}")).json()["items"]
    assert items[0]["result"]["written"] == ["company"] and items[0]["result"]["kept"] == []
    assert (await db().people.find_one({"_id": people._oid(pid)}))["company"]["v"] == "Numix"


async def test_corrections_are_derived_and_supersede(writer, reader):
    r = await writer.post("/api/people", json={"tier": "member", "fields": {
        "name": "andrÃ© niÃ±o", "bio": "Founder of a fintech."}})
    pid = r.json()["id"]
    r = await writer.post(f"/api/people/{pid}/normalize?wait=true", json={})
    assert r.status_code == 200, r.text
    got = (await reader.get(f"/api/people/{pid}")).json()
    assert got["name"] == "André Niño"                                  # a system fix of a UI value
    assert got["history"]["name"][-1]["derived"] is True and got["history"]["name"][-1]["shown"]
    assert got["history"]["name"][-1]["why"] == "newest entry"


async def test_recompute_shown_reports_and_applies(writer):
    r = await writer.post("/api/people", json={"tier": "member", "fields": {
        "name": "Zed Zane", "company": "Old Co"}})
    pid = r.json()["id"]
    # a document written when the last entry always showed: an upload over a UI value
    await db().people.update_one({"_id": people._oid(pid)}, {"$push": {"company.h": {
        "v": "Uploaded Co", "src": {"kind": "import", "by": "x.csv", "ref": "row 2"}}},
        "$set": {"company.v": "Uploaded Co"}})
    rep = await people.recompute_shown(dry_run=True)
    assert rep["flips"] == 1 and rep["by_key"] == {"company": 1} and rep["by_kinds"] == {"import -> ui": 1}
    assert rep["samples"][0]["from"] == "Uploaded Co" and rep["samples"][0]["to"] == "Old Co"
    assert (await db().people.find_one({"_id": people._oid(pid)}))["company"]["v"] == "Uploaded Co"
    rep = await people.recompute_shown(dry_run=False)
    assert rep["flips"] == 1
    doc = await db().people.find_one({"_id": people._oid(pid)})
    assert doc["company"]["v"] == "Old Co" and len(doc["company"]["h"]) == 2
    assert (await people.recompute_shown(dry_run=True))["flips"] == 0


async def test_mcp_restore_by_writing_again(writer, client_factory):
    r = await writer.post("/api/people", json={"tier": "member", "fields": {"name": "James Brown"}})
    pid = r.json()["id"]
    key = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    err, _ = await call(c, key, "update_person", {"id": pid, "fields": {"name": "James B"}})
    assert not err
    err, got = await call(c, key, "get_person", {"id": pid})
    assert not err and got["name"] == "James B"
    err, got = await call(c, key, "update_person", {"id": pid, "fields": {"name": "James Brown"}})
    assert not err and got["name"] == "James Brown"
    err, got = await call(c, key, "get_person", {"id": pid})
    assert [e["v"] for e in got["history"]["name"]] == ["James Brown", "James B", "James Brown"]
    assert got["history"]["name"][2]["shown"] is True
