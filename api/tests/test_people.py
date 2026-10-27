import pytest

from app import embeddings, fields, jobs
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

    ada = await db().people.find_one({"name": "Ada Okafor"})
    assert ada["tier"] == "member" and ada["mrr"] == 12000
    assert ada["sectors"] == ["fintech", "payments"]
    assert ada["emails"] == ["ada@x.io", "ada2@x.io"]
    assert ada["source"]["source_id"] == "m1"
    cy = await db().people.find_one({"name": "Cy Diaz"})
    assert "company" not in cy  # sparse: empty cells omitted


async def test_reimport_upserts_and_adds_columns(writer):
    await _import(writer, MEMBERS, "members.csv", "member")
    v2 = MEMBERS.replace("Brand New Column", "Brand New Column,Twitter").replace(
        "alpha\n", "alpha,@ada\n")
    res = await _import(writer, v2, "members.csv", "member")
    assert res["inserted"] == 0 and res["updated"] == 3 and res["new_fields"] == ["twitter"]
    assert await db().people.count_documents({}) == 3
    assert (await db().people.find_one({"name": "Ada Okafor"}))["twitter"] == "@ada"


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
    assert (await reader.get(f"/api/people/{pid}")).json()["tags"] == ["a", "b"]
    assert await fields.get_field("tags")

    from sqlalchemy import select

    from app import db as pg
    fe = pg.field_embeddings
    async def bio_hash():
        async with pg.engine().connect() as conn:
            return (await conn.execute(select(fe.c.content_hash).where(
                fe.c.mongo_id == pid, fe.c.field == "bio"))).scalar_one_or_none()
    h1 = await bio_hash()
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": "builds agricultural drones", "tags": None}})
    assert r.status_code == 200 and "tags" not in r.json()
    assert (await bio_hash()) not in (None, h1)

    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": None}})
    assert await bio_hash() is None

    assert (await writer.patch(f"/api/people/{pid}", json={"fields": {"name": ""}})
            ).status_code == 422
    assert (await writer.patch(f"/api/people/{pid}", json={"fields": {"Bad Key": 1}})
            ).status_code == 422
    assert (await writer.delete(f"/api/people/{pid}")).status_code == 204
    assert (await reader.get(f"/api/people/{pid}")).status_code == 404


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
    embeddings.set_embedder(embeddings.FakeEmbedder(512))


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
    a = await db().people.find_one({"name": "A"})
    assert a["location"] == "Lagos, Nigeria" and a["tags"] == ["a", "b", "c"]


async def test_switching_embedder_reembeds(writer):
    await _import(writer, MEMBERS, "members.csv", "member")
    ids = [str(d["_id"]) async for d in db().people.find({}, {"_id": 1})]
    assert await embeddings.sync_people(ids) == 0  # up to date

    class Other(embeddings.FakeEmbedder):
        pass

    embeddings.set_embedder(Other(512))
    assert await embeddings.sync_people(ids) > 0
