"""The merge assistant: the plan for one record (verbatim vs the storage agent), matching a record
to a person by identifier, name or context, what a run writes and files, ambiguity, dry runs, the
tier of created people, and the synchronous REST and MCP surfaces."""

import pytest

from app import embedder, importer, properties
from app.assistants import runner
from app.config import get_settings
from app.mongo import db
from tests.conftest import import_people
from tests.test_mcp import call, init

LONG = ("Ada builds payments infrastructure for African merchants and spent a decade at banks "
        "before founding PayRail, where she leads product and engineering across three markets.")
assert len(LONG) > importer.SIMPLE_TEXT_CHARS


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    monkeypatch.setattr(get_settings(), "fake_agents", True)


async def _create(client, **fields):
    r = await client.post("/api/people", json={"tier": fields.pop("tier", "member"),
                                               "fields": fields})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _merge_run(client, records, **options):
    r = await client.post("/api/assistants/merge/runs",
                          json={"records": records, "options": options})
    assert r.status_code == 201, r.text
    run = await runner.wait(r.json()["id"])
    assert run["status"] == "done", run
    return (await client.get(f"/api/assistants/runs/{run['id']}")).json()["items"]


# --- level 1: the plan and the match ---------------------------------------------------------------------

async def test_plan_row_splits_verbatim_from_agent_lines(writer):
    catalog = {p.key: p for p in await properties.catalog()}
    values = {"name": "Ada Okafor", "sectors": ["fintech"], "bio": LONG, "mystery": "zz", "mrr": ""}
    plan = importer.plan_row(values, catalog)
    assert plan.direct == {"name": "Ada Okafor", "sectors": ["fintech"]}
    assert plan.appends == ["sectors"]                     # a list property appends its items
    assert [k for k, _ in plan.lines] == ["bio", "mystery"] and plan.unknown == ["mystery"]
    assert plan.public() == {"direct": ["name", "sectors"], "agent": ["bio", "mystery"],
                             "unknown": ["mystery"]}
    passage = importer.render_lines(plan.lines)
    assert passage.splitlines()[1].startswith("bio: Ada builds") and "mystery: zz" in passage

    without = importer.plan_row(values, catalog, agent=False)
    assert without.direct == {"name": "Ada Okafor", "sectors": ["fintech"], "bio": LONG}
    assert without.lines == [] and without.unknown == ["mystery"]


def test_rows_of_one_person_are_grouped_in_file_order():
    parsed = importer.parse_csv("ID,Name,Email,Bio\n"
                                "1,Ada,ada@x.io,first\n"
                                ",Bo,bo@x.io,b1\n"
                                "1,Ada,,second\n"
                                ",Bo,bo@x.io,b2\n"
                                ",Bo,other@x.io,b3\n", "g.csv")
    groups = importer.group_rows(parsed.rows)
    assert [[r.row_no for r in g] for g in groups] == [[2, 4], [3, 5], [6]]
    assert groups[0][0].source_id == "1" and groups[1][0].source_id is None


async def test_match_record_by_identifier_name_and_context(writer):
    ada = await _create(writer, name="Ada Okafor", email="Ada@X.io", company="PayRail")
    ada2 = await _create(writer, name="ada okafor", company="Leafy", location="Berlin")
    bo = await _create(writer, name="Bo Chen", linkedin="https://www.linkedin.com/in/bochen/")

    m = await importer.match_record({"name": "A. Okafor", "email": "ada@x.io"})
    assert (m.decision, m.person_id, m.tier, m.keys) == ("matched", ada, "identifier", ["email"])
    m = await importer.match_record({"name": "Robert Chen", "linkedin": "linkedin.com/in/bochen?x=1"})
    assert (m.decision, m.person_id, m.tier) == ("matched", bo, "identifier")
    m = await importer.match_record({"name": "Bo Chen"})
    assert (m.decision, m.person_id, m.tier) == ("matched", bo, "name")
    m = await importer.match_record({"name": "Ada Okafor", "company": "LEAFY"})
    assert (m.decision, m.person_id, m.tier) == ("matched", ada2, "name+context")
    m = await importer.match_record({"name": "Ada Okafor", "email": "someone@x.io"})
    assert (m.decision, m.person_id, m.tier) == ("matched", ada, "name+context")   # email domain
    m = await importer.match_record({"name": "Ada Okafor"})
    assert m.decision == "ambiguous" and {c["person_id"] for c in m.candidates} == {ada, ada2}
    assert m.person_id is None
    m = await importer.match_record({"name": "Nobody Here", "email": "n@n.io"})
    assert m.decision == "new" and m.candidates == []

    await import_people(writer, "ID,Name\nm7,Someone\n", "m.csv", "member")
    m = await importer.match_record({"name": "Renamed"}, {"file": "m.csv", "source_id": "m7"})
    assert (m.decision, m.tier, m.keys) == ("matched", "identifier", ["source_id"])


# --- the assistant ------------------------------------------------------------------------------------------

async def test_run_matches_writes_verbatim_and_files_prose_through_the_agent(writer):
    await import_people(writer, "Name,Email,Sectors,Bio\nAda Okafor,ada@x.io,fintech;payments,Builds\n",
                        "m.csv", "member")
    before = await db().people.find_one({"name.v": "Ada Okafor"})
    items = await _merge_run(writer, [{"name": "Ada Okafor", "email": "ada@x.io",
                                       "sectors": ["lending"], "bio": LONG,
                                       "favourite_tool": "lathe"}])
    res = items[0]["result"]
    assert items[0]["status"] == "done" and items[0]["person_id"] == str(before["_id"])
    assert res["match"]["decision"] == "matched" and res["match"]["tier"] == "identifier"
    assert res["created"] is False and res["written"] == ["sectors"]      # the email is the same
    assert set(res["filed"]) >= {"bio", "notes"}      # long prose filed; an unknown key to notes
    assert res["plan"] == {"direct": ["email", "name", "sectors"],
                           "agent": ["bio", "favourite_tool"], "unknown": ["favourite_tool"]}
    assert res["review"]["kind"] == "Person"
    doc = await db().people.find_one({"_id": before["_id"]})
    assert doc["sectors"]["v"] == ["fintech", "payments", "lending"]
    assert doc["bio"]["v"] == LONG and [e["v"] for e in doc["bio"]["h"]] == ["Builds", LONG]
    assert "favourite_tool: lathe" in doc["notes"]["v"] and doc["record_kind"]["v"] == "Person"
    src = doc["sectors"]["h"][-1]["src"]
    assert src["kind"] == "ui" and src["by"] == "wendy"      # a record handed in: the caller's write
    assert doc["bio"]["h"][-1]["src"]["kind"] == "agent"       # the storage agent's


async def test_ambiguous_record_is_skipped_with_its_candidates(writer):
    ada = await _create(writer, name="Ada Okafor", company="PayRail")
    ada2 = await _create(writer, name="Ada Okafor", company="Leafy")
    items = await _merge_run(writer, [{"name": "Ada Okafor", "bio": "new facts"}])
    assert items[0]["status"] == "skipped" and items[0]["person_id"] is None
    res = items[0]["result"]
    assert res["match"]["decision"] == "ambiguous"
    assert {c["person_id"] for c in res["match"]["candidates"]} == {ada, ada2}
    assert res["text"].startswith("Ambiguous: 2 people could be Ada Okafor")
    assert await db().people.count_documents({"bio.v": "new facts"}) == 0
    assert (await db().people.find_one({"_id": {"$exists": True}}))["rev"] == 1


async def test_created_people_take_the_run_tier_and_matched_keep_theirs(writer):
    await import_people(writer, "Name,Email\nDee Evans,dee@y.io\n", "crm.csv", "plus_one")
    items = await _merge_run(writer, [{"name": "Dee Evans", "email": "dee@y.io", "company": "Y"},
                                      {"name": "New Person", "email": "new@y.io"}],
                             tier="member", review=False)
    by_label = {it["label"]: it for it in items}
    assert by_label["Dee Evans"]["result"]["created"] is False
    assert by_label["New Person"]["result"]["created"] is True
    dee = await db().people.find_one({"name.v": "Dee Evans"})
    new = await db().people.find_one({"name.v": "New Person"})
    assert dee["tier"] == "plus_one" and dee["company"]["v"] == "Y"
    assert new["tier"] == "member" and new["rev"] == 1 and "source" not in new
    assert new["name"]["h"][0]["src"] == {"kind": "ui", "by": "wendy"}   # no file: the caller's


async def test_remerging_the_same_record_writes_nothing(writer):
    await import_people(writer, "Name,Email,Bio\nDee Evans,dee@y.io,Operator\n", "crm.csv", "member")
    before = await db().people.find_one({"name.v": "Dee Evans"})
    items = await _merge_run(writer, [{"name": "Dee Evans", "email": "dee@y.io", "bio": "Operator"}],
                             review=False)
    res = items[0]["result"]
    assert res["written"] == [] and res["changes"] == [] and "nothing new" in res["text"]
    after = await db().people.find_one({"name.v": "Dee Evans"})
    assert after["rev"] == before["rev"] and after["updated_at"] == before["updated_at"]


async def test_record_naming_its_person_and_records_without_a_name(writer):
    bo = await _create(writer, name="Bo Chen")
    items = await _merge_run(writer, [{"person_id": bo, "notes": "met at demo day"},
                                      {"email": "nobody@x.io", "notes": "no name here"},
                                      {"name": "Quinn", "mystery": "v"}],
                             agent=False, review=False)
    named, nameless, quinn = items
    assert named["person_id"] == bo and named["result"]["match"]["tier"] == "person_id"
    assert named["result"]["written"] == ["notes"]
    assert nameless["status"] == "skipped" and nameless["result"]["text"].startswith("No name")
    assert quinn["result"]["created"] and quinn["result"]["plan"]["unknown"] == ["mystery"]
    assert quinn["result"]["skipped"] == [{"step": "mystery", "reason": "no such property"}]
    assert await db().people.count_documents({}) == 2


async def test_dry_run_over_records_writes_nothing(writer):
    ada = await _create(writer, name="Ada Okafor", email="ada@x.io")
    items = await _merge_run(writer, [{"name": "Ada Okafor", "email": "ada@x.io", "bio": LONG},
                                      {"name": "New Person"}], dry_run=True)
    first, second = (it["result"] for it in items)
    assert first["dry_run"] and first["match"]["person_id"] == ada and first["plan"]["agent"] == ["bio"]
    assert "would match (identifier)" in first["text"]
    assert second["match"]["decision"] == "new" and "would create" in second["text"]
    assert await db().people.count_documents({}) == 1
    assert "bio" not in await db().people.find_one({"_id": {"$exists": True}})


async def test_runs_need_records_and_a_wrong_shape_is_refused(writer):
    r = await writer.post("/api/assistants/merge/runs", json={"options": {}})
    assert r.status_code == 422 and "records is required" in r.text
    r = await writer.post("/api/assistants/merge/runs", json={"records": ["not a record"]})
    assert r.status_code == 422


# --- the synchronous surfaces ------------------------------------------------------------------------

async def test_merge_endpoint_merges_one_record_now(writer, reader):
    r = await writer.post("/api/people/merge", json={
        "record": {"name": "New Person", "email": "np@x.io"}, "options": {"review": False}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] and body["status"] == "done" and body["run_id"]
    pid = body["person_id"]
    assert (await db().people.find_one({"_id": {"$exists": True}}))["tier"] == "member"

    r = await writer.post("/api/people/merge", json={
        "record": {"name": "Renamed Person", "email": "NP@x.io", "company": "Z"},
        "options": {"review": False}})
    body = r.json()
    assert body["person_id"] == pid and body["match"]["tier"] == "identifier"
    assert body["written"] == ["company", "name"]          # write freely: history keeps the old name
    assert (await reader.post("/api/people/merge", json={"record": {"name": "x"}})).status_code == 403


async def test_mcp_merge_person(client_factory, writer):
    ada = await _create(writer, name="Ada Okafor", email="ada@x.io")
    key = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    err, res = await call(c, key, "merge_person", {
        "record": {"name": "Ada Okafor", "email": "ada@x.io", "sectors": ["fintech"]},
        "review": False})
    assert not err, res
    assert res["person_id"] == ada and res["match"]["tier"] == "identifier"
    assert res["written"] == ["sectors"] and res["status"] == "done"
    doc = await db().people.find_one({"_id": {"$exists": True}})
    assert doc["sectors"]["h"][0]["src"]["kind"] == "mcp"

    err, res = await call(c, key, "merge_person", {
        "record": {"name": "Ada Okafor"}, "dry_run": True})
    assert not err and res["dry_run"] and res["match"]["decision"] == "matched"

    err, res = await call(c, key, "merge_person", {"record": {"email": "lonely@x.io"}})
    assert not err and res["status"] == "skipped"
    assert await db().people.count_documents({}) == 1
