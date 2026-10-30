"""The property catalog: specs + registry, creation through `ensure` only, name vectors."""

import pytest
from sqlalchemy import select

from app import db, embedder, jobs, people, properties
from app.mongo import db as mongo_db
from app.property_specs import SPECS, STAGES

CSV = """ID,Name,Bio,MRR,Stage,Brand New Column
1,Ada,payments infrastructure for merchants,"$12,000",seed,alpha
2,Bo,climate investor,,Series A,
"""


@pytest.fixture(autouse=True)
def _fake_embedder():
    embedder.set_embedder(embedder.FakeEmbedder(512))


async def _import(client, text=CSV, name="p.csv"):
    r = await client.post("/api/people/import", data={"tier": "member"},
                          files={"file": (name, text.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    await jobs.drain()
    return r.json()


async def _name_rows() -> dict[str, str]:
    pe = db.property_embeddings
    async with db.engine().connect() as conn:
        return {r.key: r.text for r in await conn.execute(select(pe.c.key, pe.c.text))}


async def test_catalog_merges_specs_registry_and_coverage(writer, reader):
    res = await _import(writer)
    assert res["new_properties"] == ["brand_new_column"]    # typed keys already exist
    cat = {p["key"]: p for p in (await reader.get("/api/properties")).json()}
    new = cat["brand_new_column"]
    assert (new["type"], new["embeddable"], new["normalisable"], new["source"], new["count"]) == (
        "text", True, False, "import", 1)
    assert cat["mrr"]["type"] == "number" and not cat["mrr"]["embeddable"]
    assert cat["mrr"]["normalisable"] and cat["mrr"]["source"] == "spec"
    assert cat["stage"]["values"] == list(STAGES) and cat["stage"]["count"] == 2
    assert cat["name"]["type"] == "keyword" and cat["name"]["count"] == 2
    assert cat["bio"]["label"] == "Bio" and cat["col_source"]["label"] == "Source"
    assert "id" not in cat and all(k in cat for k in SPECS)
    doc = await mongo_db().fields.find_one({"_id": "brand_new_column"})
    assert doc["source"] == "import" and "type" not in doc and "embed" not in doc


async def test_ensure_is_the_only_creator(writer):
    assert await properties.ensure(["favourite_tool"], "agent") == ["favourite_tool"]
    assert await properties.ensure(["favourite_tool"], "agent") == []       # idempotent
    prop = await properties.get("favourite_tool")
    assert prop.type == "text" and prop.embeddable and prop.source == "agent"
    assert (await _name_rows())["favourite_tool"] == "favourite tool"
    with pytest.raises(properties.PropertyError):
        await properties.ensure(["Bad Key"], "agent")
    with pytest.raises(properties.PropertyError):
        await properties.ensure(["history"], "agent")
    assert not await properties.is_known("history")


async def test_people_writes_accept_known_properties_only(writer):
    r = await writer.post("/api/people", json={"tier": "member",
                                               "fields": {"name": "Zed", "gadget": "lathe"}})
    assert r.status_code == 422 and "unknown property 'gadget'" in r.text
    r = await writer.post("/api/people", json={"tier": "member", "fields": {"name": "Zed"}})
    assert r.status_code == 201
    pid = r.json()["id"]
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"gadget": "lathe"}})
    assert r.status_code == 422
    await properties.ensure(["gadget"], "agent")
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"gadget": "lathe"}})
    assert r.status_code == 200 and r.json()["gadget"] == "lathe"


async def test_enum_values_are_stored_and_extensible():
    assert await properties.enum_values("model") == ["B2B", "B2C", "Marketplace"]
    assert await properties.add_enum_value("model", "B2B2C", aliases=["b2b2c"], src="agent:x")
    assert not await properties.add_enum_value("model", "B2B2C")               # already there
    assert await properties.enum_values("model") == ["B2B", "B2C", "Marketplace", "B2B2C"]
    doc = await mongo_db().fields.find_one({"_id": "model"})
    extra = doc["enum"]["values"][-1]
    assert extra["v"] == "B2B2C" and extra["aliases"] == ["b2b2c"] and extra["src"] == "agent:x"
    assert len(doc["enum"]["values"]) == 4
    await properties.sync_specs()                                               # idempotent
    doc = await mongo_db().fields.find_one({"_id": "model"})
    assert [e["v"] for e in doc["enum"]["values"]] == ["B2B", "B2C", "Marketplace", "B2B2C"]
    assert await mongo_db().fields.count_documents({}) == len(SPECS)
    with pytest.raises(properties.PropertyError):
        await properties.add_enum_value("bio", "x")
    with pytest.raises(properties.PropertyError):
        await properties.add_enum_value("model", "  ")


async def test_sync_names_follows_text_changes_and_removals():
    rows = await _name_rows()
    assert rows["model"] == "model. Values: B2B; B2C; Marketplace"
    assert rows["linkedin"] == "linkedin" and rows["col_source"] == "source"
    assert await properties.sync_names() == 0                                   # up to date
    await properties.add_enum_value("model", "B2B2C")
    assert await properties.sync_names() == 1
    assert (await _name_rows())["model"].endswith("B2B2C")
    await properties.ensure(["gadget"], "agent")
    assert "gadget" in await _name_rows()
    await mongo_db().fields.delete_one({"_id": "gadget"})                      # gone from the data
    properties.reset()
    await properties.sync_names()
    assert "gadget" not in await _name_rows()


async def test_nearest_property_names():
    await properties.ensure(["favourite_tool"], "agent")
    vectors = await embedder.embed_texts(["company description", "favourite tool"])
    hits = await properties.nearest(vectors, k=2)
    assert hits[0][0][0] == "company_description" and hits[0][0][1] == pytest.approx(1.0)
    assert hits[1][0][0] == "favourite_tool"
    assert len(hits[0]) == 2 and hits[0][0][1] >= hits[0][1][1]


async def test_coverage_counts_current_values_only(writer, reader):
    r = await writer.post("/api/people", json={"tier": "member",
                                               "fields": {"name": "Yan", "bio": "long enough bio"}})
    pid = r.json()["id"]
    count = lambda cat, k: next(p["count"] for p in cat if p["key"] == k)
    assert count((await reader.get("/api/properties")).json(), "bio") == 1
    await writer.patch(f"/api/people/{pid}", json={"fields": {"bio": None}})
    assert count((await reader.get("/api/properties")).json(), "bio") == 0
    assert await people.value_counts("name") == [("Yan", 1)]
