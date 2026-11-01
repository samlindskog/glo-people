"""Normalised values: the parser, when documents are (re)normalised, the write paths that record
`norm` in the same write, typed filters and sorts, and compare-and-set under concurrency."""

import asyncio
from datetime import UTC, datetime

import pytest

from app import embedder, history, importer, normalize, people, properties
from app.auth import Principal
from app.history import Source
from app.mongo import db
from app.normalize import NON_NORMALIZABLE, UNMAPPED, Parsed, parse
from tests.conftest import import_people

CSV = """ID,Name,MRR,Stage,Joined,In WhatsApp,Needs Stated,Bio
1,Ada,"$12,000",seed,2026-06-05,Yes,capital; partners,payments infrastructure
2,Bo,200k,Series A,24 Jun 2024,no,growth; crypto,climate investor
3,Cy,N.A.,Pre-seed without revenue,—,maybe,exit,lending
4,Di,,,,,,
"""


@pytest.fixture(autouse=True)
def _fake_embedder():
    embedder.set_embedder(embedder.FakeEmbedder(512))


@pytest.fixture
async def people_by_name(writer):
    await import_people(writer, CSV, "n.csv", "member")
    return {d["name"]["v"]: d async for d in db().people.find({})}


STAGES = {"pre seed": "Pre-seed", "seed": "Seed", "series a": "Series A", "series b": "Series B+"}


def test_parse_numbers_booleans_dates():
    assert parse("$12,000", "number") == Parsed(12000)
    assert parse("200k", "number") == Parsed(200000)
    assert parse("$1.5M+", "number") == Parsed(1500000)
    assert parse("5million", "number") == Parsed(5000000)
    assert parse(">200k", "number") == Parsed(200000)
    assert parse("$4mn", "number") == Parsed(4000000)
    assert parse(12000, "number") == Parsed(12000) and parse(0.5, "number") == Parsed(0.5)
    assert parse("N.A.", "number") == Parsed(None, NON_NORMALIZABLE)
    assert parse("—", "number") == Parsed(None, NON_NORMALIZABLE)
    assert parse("Prefer not to say", "number") == Parsed(None, NON_NORMALIZABLE)
    assert parse("prefer to not disclose/does not apply", "number") == Parsed(None, NON_NORMALIZABLE)
    # ranges normalise to their lower bound; the unit of the upper amount applies to a bare lower one
    assert parse("500k to 1m", "number") == Parsed(500000)
    assert parse("$1k-$5k MRR", "number") == Parsed(1000)
    assert parse("$10k - $50k MRR", "number") == Parsed(10000)
    assert parse("100-500k", "number") == Parsed(100000)
    assert parse("$1-5k mmr", "number") == Parsed(1000)
    assert parse("$250k -1m", "number") == Parsed(250000)
    assert parse("0-25k", "number") == Parsed(0)
    assert parse("$10m+ MRR", "number") == Parsed(10000000)
    assert parse("350,000$", "number") == Parsed(350000)
    # zero phrases
    assert parse("Pre-Revenue", "number") == Parsed(0) and parse("bootstrapped", "number") == Parsed(0)
    assert parse("pre- funding", "number") == Parsed(0) and parse("Self-funded", "number") == Parsed(0)
    assert parse("vc", "number") == Parsed(None, UNMAPPED)
    assert parse("pre-seed", "number") == Parsed(None, UNMAPPED)
    assert parse("$1m, does not apply", "number") == Parsed(None, UNMAPPED)
    assert parse(["1", "2"], "number") == Parsed(None, UNMAPPED)
    assert parse(["7"], "number") == Parsed(7)

    assert parse("Yes", "boolean") == Parsed(True) and parse("nope", "boolean") == Parsed(False)
    assert parse(True, "boolean") == Parsed(True)
    assert parse("maybe", "boolean") == Parsed(None, UNMAPPED)
    assert parse("1", "boolean") == Parsed(True)

    assert parse("2026-06-05", "date") == Parsed(datetime(2026, 6, 5, tzinfo=UTC))
    assert parse("24 Jun 2024", "date") == Parsed(datetime(2024, 6, 24, tzinfo=UTC))
    assert parse("Jan 2024", "date") == Parsed(datetime(2024, 1, 1, tzinfo=UTC))
    assert parse("01/02/2024", "date") == Parsed(datetime(2024, 1, 2, tzinfo=UTC))
    assert parse("2026-06-05T10:00:00Z", "date").value == datetime(2026, 6, 5, 10, tzinfo=UTC)
    assert parse("soon", "date") == Parsed(None, UNMAPPED)
    assert parse(None, "date") == Parsed() and parse("", "number") == Parsed()
    with pytest.raises(ValueError):
        parse("x", "text")


def test_parse_identity_types_to_lookup_keys():
    assert parse("  Ada   OKAFOR ", "keyword").value == "ada okafor"
    assert parse("Jordan | Cofounder", "keyword").value == "jordan cofounder"
    assert parse("https://www.linkedin.com/in/ada/?utm_source=x", "url").value == \
        parse("linkedin.com/in/ada", "url").value == "linkedin.com/in/ada"
    assert parse(["github.com/adaok", "https://GitHub.com/adaok/"], "url").value == ["github.com/adaok"]
    assert parse("not a url at all", "url").flag == "non_normalizable"
    assert parse(" Ada.Okafor@Example.com ", "contact").value == "ada.okafor@example.com"
    assert parse("mailto:a@b.co", "contact").value == "a@b.co"
    assert parse("+1 (310) 430-2843", "contact").value == "+13104302843"
    assert parse(["a@b.co", "c@d.io"], "contact").value == ["a@b.co", "c@d.io"]
    assert parse("call me maybe", "contact").flag == "non_normalizable"
    assert parse("", "keyword").value is None and parse([], "url").value is None


def test_parse_enums_and_lists():
    assert parse("seed", "enum", STAGES) == Parsed("Seed")
    assert parse("Pre-Seed", "enum", STAGES) == Parsed("Pre-seed")
    assert parse("Series B+", "enum", STAGES) == Parsed("Series B+")
    assert parse("Pre-seed without revenue", "enum", STAGES) == Parsed(None, UNMAPPED)
    assert parse("does not apply", "enum", STAGES) == Parsed(None, NON_NORMALIZABLE)
    yes_no = {"yes": "Yes", "no": "No", "not applicable": "Not applicable"}
    assert parse("Nope", "enum", yes_no) == Parsed("No") and parse("y", "enum", yes_no) == Parsed("Yes")
    assert parse("nope", "enum", STAGES) == Parsed(None, UNMAPPED)
    needs = {"capital": "Capital", "partners": "Partners", "growth": "Growth"}
    assert parse(["capital", "Partners"], "enum_list", needs) == Parsed(["Capital", "Partners"])
    assert parse("growth", "enum_list", needs) == Parsed(["Growth"])
    assert parse(["growth", "crypto"], "enum_list", needs) == Parsed(["Growth"], UNMAPPED)
    assert parse(["crypto"], "enum_list", needs) == Parsed(None, UNMAPPED)
    assert parse(["n/a"], "enum_list", needs) == Parsed(None, NON_NORMALIZABLE)
    assert parse(["capital", "capital", "-"], "enum_list", needs) == Parsed(["Capital"])


async def test_enum_lookup_uses_canonical_values_and_aliases():
    lookup = await properties.enum_lookup("sponsor")
    assert lookup["yes future events"] == "Yes, future events"
    await properties.add_enum_value("stage", "Growth", aliases=["scaleup stage", "Growth stage"])
    lookup = await properties.enum_lookup("stage")
    assert lookup["growth stage"] == "Growth" and lookup["scaleup stage"] == "Growth"
    assert parse("Growth Stage", "enum", lookup) == Parsed("Growth")
    assert await properties.enum_lookup("bio") == {}


async def test_import_records_text_and_normalised_value(people_by_name):
    ada, bo, cy = (people_by_name[n] for n in ("Ada", "Bo", "Cy"))
    assert ada["mrr"]["v"] == "$12,000" and ada["mrr"]["norm"]["v"] == 12000
    entry = ada["mrr"]["norm"]["h"][0]
    assert entry["src"] == {"kind": "system", "by": "normalize"}
    assert entry["of"] == normalize.text_hash("$12,000") and "flag" not in entry
    assert ada["stage"]["norm"]["v"] == "Seed" and ada["in_whatsapp"]["norm"]["v"] is True
    assert ada["joined"]["norm"]["v"] == datetime(2026, 6, 5, tzinfo=UTC)
    assert ada["needs_stated"]["v"] == ["capital", "partners"]
    assert ada["needs_stated"]["norm"]["v"] == ["Capital", "Partners"]
    assert "norm" not in ada["bio"]                       # free text: nothing to normalise
    assert ada["name"]["norm"]["v"] == normalize.parse(ada["name"]["v"], "keyword").value  # lookup key

    assert bo["mrr"]["norm"]["v"] == 200000 and bo["stage"]["norm"]["v"] == "Series A"
    assert bo["in_whatsapp"]["norm"]["v"] is False
    assert bo["needs_stated"]["norm"] == {**bo["needs_stated"]["norm"], "v": ["Growth"],
                                          "flag": "unmapped"}
    cy_norm = {k: (cy[k]["norm"].get("v"), cy[k]["norm"].get("flag"))
               for k in ("mrr", "stage", "joined", "in_whatsapp")}
    assert cy_norm == {"mrr": (None, "non_normalizable"), "stage": (None, "unmapped"),
                       "joined": (None, "non_normalizable"), "in_whatsapp": (None, "unmapped")}
    assert "mrr" not in people_by_name["Di"]


async def test_public_view_and_coverage_flags(people_by_name, reader):
    ada = (await reader.get(f"/api/people/{people_by_name['Ada']['_id']}")).json()
    assert ada["mrr"] == "$12,000" and ada["norm"]["mrr"] == 12000
    assert ada["norm"]["joined"] == "2026-06-05T00:00:00+00:00" and ada["norm_flags"] == {}
    assert ada["history_norm"]["mrr"][0]["v"] == 12000 and "history_norm" in ada
    assert "bio" not in ada["history_norm"]
    cy = (await reader.get(f"/api/people/{people_by_name['Cy']['_id']}")).json()
    assert cy["norm"] == {"needs_stated": ["Exit"]} and cy["norm_flags"] == {
        "mrr": "non_normalizable", "stage": "unmapped", "joined": "non_normalizable",
        "in_whatsapp": "unmapped"}
    cat = {p["key"]: p for p in (await reader.get("/api/properties")).json()}
    assert (cat["mrr"]["count"], cat["mrr"]["unmapped"], cat["mrr"]["non_normalizable"]) == (3, 0, 1)
    assert (cat["stage"]["unmapped"], cat["needs_stated"]["unmapped"]) == (1, 1)
    assert cat["bio"]["unmapped"] == 0 and cat["mrr"]["filter_ops"][-1] == "lte"
    assert "gt" not in cat["bio"]["filter_ops"]


async def test_updates_normalise_in_the_same_write_and_dedupe(people_by_name, writer):
    pid = str(people_by_name["Ada"]["_id"])
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"stage": "series a"}})
    body = r.json()
    assert body["stage"] == "series a" and body["norm"]["stage"] == "Series A"
    assert [e["v"] for e in body["history_norm"]["stage"]] == ["Seed", "Series A"]
    # the same text again: nothing appended anywhere
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"stage": "series a"}})
    assert len(r.json()["history"]["stage"]) == 2 and len(r.json()["history_norm"]["stage"]) == 2
    # a different text for the same value: a norm entry records which text it derives from
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"stage": "Series A"}})
    hn = r.json()["history_norm"]["stage"]
    assert len(hn) == 3 and hn[-1]["of"] == normalize.text_hash("Series A")
    # unmapped and non-normalizable texts are flagged; removing the value clears the norm
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"stage": "Pre-seed w/o revenue"}})
    assert r.json()["norm_flags"] == {"stage": "unmapped"} and "stage" not in r.json()["norm"]
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"stage": "—"}})
    assert r.json()["norm_flags"] == {"stage": "non_normalizable"}
    r = await writer.patch(f"/api/people/{pid}", json={"fields": {"stage": None}})
    assert "stage" not in r.json()["norm"] and r.json()["norm_flags"] == {}
    doc = await db().people.find_one({"_id": people_by_name["Ada"]["_id"]})
    assert doc["stage"]["norm"]["v"] is None and doc["stage"]["norm"]["h"][-1]["v"] is None
    assert doc["rev"] == 1 + 5   # one import write, then every effective update bumped rev


async def test_create_normalises(writer):
    r = await writer.post("/api/people", json={"tier": "member", "fields": {
        "name": "Zed", "mrr": "1.5M", "warmth": "hot", "communities": ["Investors", "Robots"]}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["norm"] == {"mrr": 1500000, "warmth": "Hot", "communities": ["Investors"]}
    assert body["norm_flags"] == {"communities": "unmapped"}
    doc = await db().people.find_one({"name.v": "Zed"})
    assert doc["rev"] == 1


async def test_document_skips_up_to_date_properties(people_by_name):
    ada = people_by_name["Ada"]
    assert await people.pending_normalization(ada) == {}
    forced = await people.pending_normalization(ada, force=True)      # a parser upgrade redoes its own work
    assert set(forced) == {"mrr", "stage", "joined", "in_whatsapp", "needs_stated", "name"}
    assert forced["mrr"] == Parsed(12000)                   # ...and append_norm then dedupes it
    assert history.append_norm(ada["mrr"], 12000, people.NORMALIZE_SRC, datetime.now(UTC),
                               normalize.text_hash("$12,000")) is None
    ada["stage"]["norm"]["h"][-1]["src"] = {"kind": "agent", "by": "normalizer"}
    assert "stage" not in await people.pending_normalization(ada, force=True)   # an agent's call is kept
    ada["mrr"]["v"] = "$13,000"                           # text changed, norm not yet
    assert await people.pending_normalization(ada) == {"mrr": Parsed(13000)}
    ada["mrr"]["v"] = None                                # removed: norm to be cleared
    assert await people.pending_normalization(ada) == {"mrr": Parsed()}
    del ada["mrr"]["norm"]
    assert await people.pending_normalization(ada) == {}           # removed and never normalised: nothing
    assert await people.pending_normalization({"_id": 1, "bio": {"v": "x", "h": []}}) == {}


async def test_reparse_fills_legacy_documents_and_update_norm(people_by_name, writer):
    ts = datetime(2026, 1, 2, tzinfo=UTC)
    await db().people.insert_one({"name": {"v": "Old", "h": [{"v": "Old", "ts": ts}]},
                                  "mrr": {"v": "$5k", "h": [{"v": "$5k", "ts": ts}]},
                                  "tier": "member", "schema_version": 2})
    everyone = await people.find_ids({})
    assert await people.reparse(everyone) == 1
    old = await db().people.find_one({"name.v": "Old"})
    assert old["mrr"]["norm"]["v"] == 5000 and old["rev"] == 1
    assert await people.reparse(everyone) == 0              # same parser, same texts: no writes

    # the normaliser agent maps an unmapped value; a later text-unrelated write keeps it
    cy = str(people_by_name["Cy"]["_id"])
    agent = Source("agent", "normalizer", "run:1/item:2")
    got = (await people.update_norm(None, cy, {"stage": Parsed("Pre-seed")}, agent)).person
    assert got["norm"]["stage"] == "Pre-seed" and got["norm_flags"] == {
        "mrr": "non_normalizable", "joined": "non_normalizable", "in_whatsapp": "unmapped"}
    assert got["history_norm"]["stage"][-1]["src"] == agent.doc()
    assert await people.update_norm(None, cy, {"stage": Parsed("Pre-seed")}, agent)  # no-op ok
    r = await writer.patch(f"/api/people/{cy}", json={"fields": {"bio": "lending for SMBs"}})
    assert r.json()["norm"]["stage"] == "Pre-seed"
    with pytest.raises(people.PersonError):
        await people.update_norm(None, cy, {"bio": Parsed("x")}, agent)


async def test_typed_filters_and_sorts(people_by_name, reader):
    async def names(body):
        r = await reader.post("/api/search", json=body)
        assert r.status_code == 200, r.text
        return [h["person"]["name"] for h in r.json()["results"]]

    f = lambda field, op, value: {"filters": [{"field": field, "op": op, "value": value}]}
    assert await names(f("mrr", "gte", 12000)) == ["Ada", "Bo"]
    assert await names(f("mrr", "gt", "12k")) == ["Bo"]
    assert await names(f("mrr", "eq", "$12,000")) == ["Ada"]
    assert await names(f("mrr", "contains", "N.A")) == ["Cy"]              # text op on the text
    assert await names(f("stage", "eq", "SERIES A")) == ["Bo"]
    assert await names(f("stage", "in", ["seed", "series a"])) == ["Ada", "Bo"]
    assert await names(f("stage", "ne", "seed")) == ["Bo", "Cy", "Di"]
    assert await names(f("stage", "contains", "pre-seed")) == ["Cy"]
    assert await names(f("stage", "exists", True)) == ["Ada", "Bo", "Cy"]
    assert await names(f("joined", "lt", "2025-01-01")) == ["Bo"]
    assert await names(f("in_whatsapp", "eq", True)) == ["Ada"]
    assert await names(f("in_whatsapp", "eq", "no")) == ["Bo"]
    assert await names(f("needs_stated", "eq", "partners")) == ["Ada"]
    assert await names(f("needs_stated", "in", ["Growth", "capital"])) == ["Ada", "Bo"]
    assert await names({"sort": {"field": "mrr", "dir": "desc"}}) == ["Bo", "Ada", "Cy", "Di"]
    assert await names({"sort": {"field": "name", "dir": "desc"}}) == ["Di", "Cy", "Bo", "Ada"]

    s = lambda body: reader.post("/api/search", json=body)
    r = await s(f("stage", "eq", "Pre-seed without revenue"))
    assert r.status_code == 422 and "one of" in r.text
    assert (await s(f("mrr", "gte", "lots"))).status_code == 422
    r = await s(f("bio", "gt", 3))
    assert r.status_code == 422 and "not available" in r.text
    assert (await s(f("joined", "lte", "yesterday"))).status_code == 422


async def test_concurrent_updates_all_land(writer):
    r = await writer.post("/api/people", json={"tier": "member", "fields": {"name": "Race"}})
    pid = r.json()["id"]
    principal = Principal(user_id=1, username="wendy", role="writer",
                          permissions=frozenset({"people:write"}))
    # every failed compare-and-set means another writer succeeded, so N writers need at most N
    # attempts each; CAS_RETRIES bounds the writers that can all collide at once
    keys = ["bio", "notes", "one_liner", "company", "role", "location", "mrr", "stage"]
    assert len(keys) <= people.CAS_RETRIES
    await asyncio.gather(*(people.update(principal, pid, {k: f"value {i}"})
                           for i, k in enumerate(keys)))
    doc = await db().people.find_one({"name.v": "Race"})
    assert all(doc[k]["v"] == f"value {i}" for i, k in enumerate(keys))
    assert doc["rev"] == 1 + len(keys)
    assert doc["mrr"]["norm"]["flag"] == "unmapped"


async def test_import_concurrent_with_an_edit_loses_nothing(writer):
    await import_people(writer, "ID,Name,Bio\n1,Ada,first\n", "c.csv", "member")
    pid = str((await db().people.find_one({"name.v": "Ada"}))["_id"])
    principal = Principal(user_id=1, username="wendy", role="writer",
                          permissions=frozenset({"people:write"}))
    for i in range(5):
        parsed = importer.parse_csv(f"ID,Name,Bio\n1,Ada,import {i}\n", "c.csv")
        await asyncio.gather(
            people.upsert_imported(parsed.rows, "c.csv", "member", datetime.now(UTC)),
            people.update(principal, pid, {"notes": f"note {i}"}))
    doc = await db().people.find_one({"_id": people._oid(pid)})
    assert [e["v"] for e in doc["bio"]["h"]] == ["first", *(f"import {i}" for i in range(5))]
    assert [e["v"] for e in doc["notes"]["h"]] == [f"note {i}" for i in range(5)]
    assert doc["rev"] == 11 and await db().people.count_documents({}) == 1


def test_norm_history_helpers():
    ts = datetime(2026, 10, 30, tzinfo=UTC)
    src = Source("system", "normalize")
    prop = {"v": "$5k", "h": [{"v": "$5k", "ts": ts, "src": {"kind": "ui"}}]}
    p1 = history.append_norm(prop, 5000, src, ts, "abc")
    assert p1["norm"] == {"v": 5000, "h": [{"v": 5000, "ts": ts, "src": src.doc(), "of": "abc"}]}
    assert history.append_norm(p1, 5000, src, ts, "abc") is None
    p2 = history.append_norm(p1, None, src, ts, "def", flag="unmapped")
    assert p2["norm"]["flag"] == "unmapped" and p2["norm"]["h"][-1]["flag"] == "unmapped"
    assert history.norm_value(p2) is None and history.norm_flag(p2) == "unmapped"
    p3 = history.append_norm(p2, 6000, src, ts, "ghi")
    assert "flag" not in p3["norm"] and history.norm_value(p3) == 6000
    doc = {"_id": 1, "mrr": p3, "gone": history.append_norm(
        {"v": None, "h": [{"v": None}]}, 7, src, ts, "x"), "bio": {"v": "b", "h": []}}
    assert history.flatten_norm(doc, frozenset({"_id"})) == {"mrr": 6000}
    assert history.norm_flags({"x": p2, "_id": 1}, frozenset({"_id"})) == {"x": "unmapped"}
    assert history.jsonable([ts, 1]) == [ts.isoformat(), 1]
