import pytest

from app import embedder, embeddings, fields, jobs
from app.mongo import db

MEMBERS = """﻿ID,Name,Company,Sectors,Bio,MRR,Emails,Brand New Column
m1,Ada Okafor,PayRail,"fintech, payments",Builds payments infrastructure for African merchants,"$12,000",ada@x.io;ada2@x.io,alpha
m2,Bo Chen,Leafy,"climate, agtech",Climate founder working on vertical farming,500,bo@x.io,
m3,Cy Diaz,,health,Doctor turned health-tech investor,,,
,  ,,,orphan bio without a name,,,
,,,,,,,
"""

CRM = """Name,Email,Industry,Raised Funds
Dee Evans,dee@y.io,Fintech,1000000
Ed Fox,ed@y.io,Gaming,
"""


async def _import(client, text, name, tier):
    r = await client.post("/api/people/import", data={"tier": tier},
                          files={"file": (name, text.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    await jobs.drain()
    return r.json()


async def test_import_registry_and_coercion(writer):
    res = await _import(writer, MEMBERS, "members.csv", "member")
    assert (res["rows"], res["inserted"], res["skipped"]) == (4, 3, 1)
    assert "brand_new_column" in res["new_fields"]

    fl = {f["key"]: f for f in (await writer.get("/api/fields")).json()}
    assert fl["sectors"]["type"] == "list" and fl["mrr"]["type"] == "number"
    assert fl["bio"]["embed"] and not fl["emails"]["embed"] and not fl["mrr"]["embed"]
    assert fl["name"]["count"] == 3 and fl["brand_new_column"]["count"] == 1
    assert "id" not in fl

    ada = await db().people.find_one({"name.v": "Ada Okafor"})
    assert ada["tier"] == "member" and ada["mrr"]["v"] == 12000
    assert ada["sectors"]["v"] == ["fintech", "payments"]
    assert ada["emails"]["v"] == ["ada@x.io", "ada2@x.io"]
    assert ada["source"]["source_id"] == "m1" and ada["schema_version"] == 2
    assert len(ada["bio"]["h"]) == 1
    assert ada["bio"]["h"][0]["src"] == {"kind": "import", "by": "members.csv", "ref": "row 2"}
    cy = await db().people.find_one({"name.v": "Cy Diaz"})
    assert "company" not in cy  # sparse: empty cells omitted


async def test_reimport_upserts_and_adds_columns(writer):
    await _import(writer, MEMBERS, "members.csv", "member")
    v2 = MEMBERS.replace("Brand New Column", "Brand New Column,Twitter").replace(
        "alpha\n", "alpha,@ada\n")
    res = await _import(writer, v2, "members.csv", "member")
    assert res["inserted"] == 0 and res["updated"] == 3 and res["new_fields"] == ["twitter"]
    assert await db().people.count_documents({}) == 3
    ada = await db().people.find_one({"name.v": "Ada Okafor"})
    assert ada["twitter"]["v"] == "@ada"
    assert len(ada["bio"]["h"]) == 1  # unchanged cells append nothing


async def test_reimport_appends_history_for_changed_cells(writer):
    await _import(writer, MEMBERS, "members.csv", "member")
    before = await db().people.find_one({"name.v": "Ada Okafor"})
    await _import(writer, MEMBERS, "members.csv", "member")
    same = await db().people.find_one({"name.v": "Ada Okafor"})
    assert same["updated_at"] == before["updated_at"] and len(same["bio"]["h"]) == 1
    v2 = MEMBERS.replace("Builds payments infrastructure", "Runs payments infrastructure")
    await _import(writer, v2, "members.csv", "member")
    ada = await db().people.find_one({"name.v": "Ada Okafor"})
    assert ada["bio"]["v"].startswith("Runs") and len(ada["bio"]["h"]) == 2
    assert ada["bio"]["h"][0]["v"].startswith("Builds")
    assert ada["updated_at"] > before["updated_at"]


async def test_crm_without_ids_is_idempotent(writer):
    await _import(writer, CRM, "crm.csv", "plus_one")
    await _import(writer, CRM, "crm.csv", "plus_one")
    assert await db().people.count_documents({"tier": "plus_one"}) == 2


async def test_import_preview(writer):
    await _import(writer, CRM, "crm.csv", "plus_one")
    r = await writer.post("/api/people/import/preview",
                          files={"file": ("m.csv", MEMBERS.encode(), "text/csv")})
    cols = {c["key"]: c for c in r.json()["columns"]}
    assert r.json()["rows"] == 4
    assert cols["id"]["role"] == "source_id"
    assert cols["name"]["new"] is False and cols["bio"]["new"] is True


async def test_embeddings_created_per_field(writer):
    await _import(writer, MEMBERS, "members.csv", "member")
    from sqlalchemy import select

    from app import db as pg
    async with pg.engine().connect() as conn:
        rows = (await conn.execute(select(pg.field_embeddings.c.field))).scalars().all()
    assert rows.count("bio") == 3 and "emails" not in rows and "mrr" not in rows


async def test_person_crud_and_reembed(writer, reader):
    r = await writer.post("/api/people", json={"tier": "plus_one",
                                               "fields": {"name": "Zed", "bio": "builds warehouse robotics",
                                                          "tags": ["a", "b"]}})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    got = (await reader.get(f"/api/people/{pid}")).json()
    assert got["tags"] == ["a", "b"] and got["bio"] == "builds warehouse robotics"
    assert [e["src"] for e in got["history"]["name"]] == [{"kind": "ui", "by": "wendy"}]
    assert await fields.get_field("tags")

    from sqlalchemy import select

    from app import db as pg
    fe = pg.field_embeddings
    async def bio_hashes():
        async with pg.engine().connect() as conn:
            return set((await conn.execute(select(fe.c.content_hash).where(
                fe.c.mongo_id == pid, fe.c.field == "bio"))).scalars().all())
    h1 = await bio_hashes()
    assert len(h1) == 1
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": "builds agricultural drones", "tags": None}})
    assert r.status_code == 200 and "tags" not in r.json()
    body = r.json()
    assert body["bio"] == "builds agricultural drones"
    assert [e["v"] for e in body["history"]["bio"]] == ["builds warehouse robotics",
                                                        "builds agricultural drones"]
    assert body["history"]["tags"][-1]["v"] is None  # tombstone
    h2 = await bio_hashes()
    assert len(h2) == 2 and h1 < h2  # one vector per passage

    # Same value again: no new entry, no updated_at bump, logged as unchanged.
    updated_at = body["updated_at"]
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": "builds agricultural drones",
                                                                  "tags": None}})
    assert len(r.json()["history"]["bio"]) == 2 and len(r.json()["history"]["tags"]) == 2
    assert r.json()["updated_at"] == updated_at
    acts = (await writer.get("/api/activity?action=person.update")).json()["items"]
    assert acts[0]["meta"]["unchanged"] == ["bio", "tags"] and acts[0]["meta"]["src"] == "ui"
    assert acts[1]["meta"] == {"set": ["bio"], "unset": ["tags"], "unchanged": [], "src": "ui"}

    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": None}})
    assert "bio" not in r.json() and await bio_hashes() == set()

    assert (await writer.patch(f"/api/people/{pid}", json={"fields": {"name": ""}})
            ).status_code == 422
    assert (await writer.patch(f"/api/people/{pid}", json={"fields": {"Bad Key": 1}})
            ).status_code == 422
    assert (await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": {"v": "x"}}})
            ).status_code == 422
    assert (await writer.patch(f"/api/people/{pid}", json={"fields": {"history": "x"}})
            ).status_code == 422
    assert (await writer.delete(f"/api/people/{pid}")).status_code == 204
    assert (await reader.get(f"/api/people/{pid}")).status_code == 404


async def test_tombstone_drops_coverage_count(writer):
    r = await writer.post("/api/people", json={"tier": "member",
                                               "fields": {"name": "Yan", "bio": "long enough bio text"}})
    pid = r.json()["id"]
    assert (await fields.get_field("bio"))["count"] == 1
    await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": None}})
    assert (await fields.get_field("bio"))["count"] == 0


async def test_legacy_documents_are_migrated(writer, reader):
    from datetime import UTC, datetime

    from app import people as people_svc
    ts = datetime(2026, 1, 2, tzinfo=UTC)
    await db().people.insert_many([
        {"name": "Old One", "tier": "member", "bio": "legacy bio", "tags": ["x"],
         "created_at": ts, "updated_at": ts,
         "source": {"file": "old.csv", "source_id": "1", "row": 7}},
        {"name": "Old Two", "tier": "member", "created_at": ts},
    ])
    one = await db().people.find_one({"name": "Old One"})
    pid = str(one["_id"])
    got = (await reader.get(f"/api/people/{pid}")).json()   # readable before migration
    assert got["bio"] == "legacy bio" and got["history"]["bio"] == [{"v": "legacy bio"}]

    assert await people_svc.migrate_legacy() == 2
    assert await people_svc.migrate_legacy() == 0
    one = await db().people.find_one({"_id": one["_id"]})
    assert one["schema_version"] == 2 and one["updated_at"] == ts
    assert one["bio"] == {"v": "legacy bio", "h": [{"v": "legacy bio", "ts": ts,
                                                   "src": {"kind": "import", "by": "old.csv",
                                                           "ref": "row 7"}}]}
    two = await db().people.find_one({"name.v": "Old Two"})
    assert two["name"]["h"][0]["src"] == {"kind": "legacy"} and two["name"]["h"][0]["ts"] == ts
    assert "name_1" not in await db().people.index_information()
    assert "name.v_1" in await db().people.index_information()

    # A write on a legacy document migrates it as a side effect.
    await db().people.insert_one({"name": "Old Three", "tier": "member", "bio": "b"})
    three = await db().people.find_one({"name": "Old Three"})
    r = await writer.patch(f"/api/people/{three['_id']}", json={"fields": {"bio": "c"}})
    assert [e["v"] for e in r.json()["history"]["bio"]] == ["b", "c"]
    three = await db().people.find_one({"_id": three["_id"]})
    assert three["schema_version"] == 2 and three["name"]["v"] == "Old Three"


async def test_duplicate_rows_in_one_import_keep_every_entry(writer):
    csv = "ID,Name,Bio\nd1,Dup Person,first bio\nd1,Dup Person,second bio\n"
    res = await _import(writer, csv, "dups.csv", "member")
    assert res["inserted"] == 1 and await db().people.count_documents({}) == 1
    doc = await db().people.find_one({"source.source_id": "d1"})
    assert doc["bio"]["v"] == "second bio"
    assert [e["v"] for e in doc["bio"]["h"]] == ["first bio", "second bio"]
    assert [e["src"]["ref"] for e in doc["bio"]["h"]] == ["row 2", "row 3"]
    assert doc["source"]["row"] == 3


async def test_reserved_history_header_is_renamed(writer):
    res = await _import(writer, "Name,History\nA,was here\n", "h.csv", "member")
    assert "col_history" in res["new_fields"]


async def test_toggle_field_embed(writer):
    await _import(writer, MEMBERS, "members.csv", "member")
    r = await writer.patch("/api/fields/bio", json={"embed": False})
    assert r.json()["embed"] is False
    r = await writer.patch("/api/fields/company", json={"embed": True})
    await jobs.drain()
    from sqlalchemy import func, select

    from app import db as pg
    fe = pg.field_embeddings
    async with pg.engine().connect() as conn:
        n_bio = (await conn.execute(select(func.count()).where(fe.c.field == "bio"))).scalar()
    assert n_bio == 0


@pytest.fixture(autouse=True)
def _fake_embedder():
    embedder.set_embedder(embedder.FakeEmbedder(512))


PROSE = """Name,Location,Bio,Status,Notes,Tags
A,"Lagos, Nigeria","Founder; previously at Stripe, Paystack.\nLoves chess",active,"Met at demo day;\nfollow up",a; b; c
B,"Berlin, Germany","Investor, climate; ex-founder",active,,x; y
C,"Austin, USA",Engineer,paused,"call back, maybe",z
"""


async def test_prose_columns_stay_text_and_short_values_not_embedded(writer):
    await _import(writer, PROSE, "prose.csv", "member")
    fl = {f["key"]: f for f in (await writer.get("/api/fields")).json()}
    assert fl["location"]["type"] == "text" and fl["bio"]["type"] == "text"
    assert fl["notes"]["type"] == "text"
    assert fl["tags"]["type"] == "list"
    assert fl["bio"]["embed"] and not fl["status"]["embed"]
    a = await db().people.find_one({"name.v": "A"})
    assert a["location"]["v"] == "Lagos, Nigeria" and a["tags"]["v"] == ["a", "b", "c"]


async def test_switching_embedder_reembeds(writer):
    await _import(writer, MEMBERS, "members.csv", "member")
    ids = [str(d["_id"]) async for d in db().people.find({}, {"_id": 1})]
    assert await embeddings.sync_people(ids) == 0  # up to date

    class Other(embedder.FakeEmbedder):
        pass

    embedder.set_embedder(Other(512))
    assert await embeddings.sync_people(ids) > 0
