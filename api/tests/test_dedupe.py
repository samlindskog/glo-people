"""Stage E: an export's preamble, null tokens, header aliases and column overrides; the repair
fast path (a full name behind a LinkedIn profile is a Person without a model call); folding a
duplicate document into another over REST, MCP and the CLI dedupe command."""

import pytest

from app import embedder, importer, people
from app.config import get_settings
from app.mongo import db
from tests.conftest import import_people
from tests.test_mcp import call, init

LINKEDIN_EXPORT = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Dan,Billson,https://www.linkedin.com/in/danbillson,,Attio,Product Engineer,17 Jul 2026
Ada,Okafor,https://www.linkedin.com/in/adaokafor,ada@x.io,PayRail,Founder,29 Nov 2015
"""

LEADS = """mongo_id,name,email,linkedin,phone,company,title
6a8c860ed7380c793c775507,Belyaninov Alexander,a.belyaninov@make.com,http://www.linkedin.com/in/belyaninov,null,Make,Head of Partnership
6a8c806dd7380c793c763866,Anna Chadenkova,a.chadenkova@arduino.cc,http://www.linkedin.com/in/anna-chadenkova-b1595b16,N/A,Arduino,Partner Marketing Manager
"""


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    monkeypatch.setattr(get_settings(), "fake_agents", True)


def test_export_preamble_aliases_and_null_tokens_are_handled():
    parsed = importer.parse_csv(LINKEDIN_EXPORT, "Connections.csv")
    cols = {c["header"]: c for c in parsed.columns}
    assert cols["URL"]["key"] == "linkedin" and cols["URL"]["alias"] is True
    assert cols["Email Address"]["key"] == "email" and cols["Position"]["key"] == "role"
    assert cols["Connected On"]["key"] == "linkedin_connected" and cols["Company"]["alias"] is False
    assert parsed.total == 2 and [r.row_no for r in parsed.rows] == [5, 6]       # real line numbers
    assert parsed.rows[0].values == {"first_name": "Dan", "last_name": "Billson", "name": "Dan Billson",
                                     "linkedin": "https://www.linkedin.com/in/danbillson",
                                     "company": "Attio", "role": "Product Engineer",
                                     "linkedin_connected": "17 Jul 2026"}

    leads = importer.parse_csv(LEADS, "leads.csv")
    cols = {c["header"]: c for c in leads.columns}
    assert cols["mongo_id"]["role"] == "source_id" and cols["title"]["key"] == "role"
    assert cols["phone"]["key"] == "phone_number"
    assert leads.rows[0].source_id == "6a8c860ed7380c793c775507"
    assert "phone_number" not in leads.rows[0].values and "phone_number" not in leads.rows[1].values

    remapped = importer.parse_csv(LEADS, "leads.csv",
                                  columns={"title": "title_enriched", "company": "skip", "Phone": "notes"})
    cols = {c["header"]: c for c in remapped.columns}
    assert cols["title"]["key"] == "title_enriched" and cols["company"]["role"] == "skip"
    assert cols["phone"]["key"] == "notes" and "company" not in remapped.rows[0].values


async def test_preview_and_import_take_a_column_mapping(writer):
    r = await writer.post("/api/people/import/preview",
                          files={"file": ("Connections.csv", LINKEDIN_EXPORT.encode(), "text/csv")})
    body = r.json()
    cols = {c["header"]: c for c in body["columns"]}
    assert cols["URL"]["key"] == "linkedin" and cols["URL"]["plan"] == "verbatim"
    assert body["rows"] == 2 and [m["decision"] for m in body["matches"]] == ["new", "new"]
    r = await writer.post("/api/people/import/preview", data={"columns": '{"Company": "skip"}'},
                          files={"file": ("Connections.csv", LINKEDIN_EXPORT.encode(), "text/csv")})
    assert {c["header"]: c["plan"] for c in r.json()["columns"]}["Company"] == "skip"
    r = await writer.post("/api/people/import/preview", data={"columns": "not json"},
                          files={"file": ("c.csv", LINKEDIN_EXPORT.encode(), "text/csv")})
    assert r.status_code == 422

    res = await import_people(writer, LINKEDIN_EXPORT, "Connections.csv", "plus_one",
                              columns={"Company": "skip"})
    assert res["items"] == 2 and res["new_properties"] == []
    dan = await db().people.find_one({"name.v": "Dan Billson"})
    assert dan["linkedin"]["v"] == "https://www.linkedin.com/in/danbillson" and "company" not in dan
    assert dan["role"]["v"] == "Product Engineer"
    assert dan["linkedin_connected"]["v"] == "17 Jul 2026"
    assert dan["linkedin_connected"]["norm"]["v"].date().isoformat() == "2026-07-17"
    assert dan["source"] == {"file": "Connections.csv", "row": 5}


async def test_repair_classifies_a_full_name_behind_a_profile_without_the_agent(writer):
    from app.history import Source
    from app.tools import invoke
    from app.tools import repair as repair_tool

    calls: list[str] = []

    def spy(messages, info):                           # a model that must never be asked
        calls.append("agent")
        raise AssertionError("the repair agent was consulted")

    r = await writer.post("/api/people", json={"tier": "plus_one", "fields": {
        "name": "Dan Billson", "linkedin": "https://www.linkedin.com/in/danbillson"}})
    pid = r.json()["id"]
    from pydantic_ai.models.function import FunctionModel
    with repair_tool.agent.override(model=FunctionModel(spy)):
        ctx = await invoke.make_context(pid, Source("agent", "repair", "test"), None)
        res = await invoke.run(repair_tool.repair, ctx)
    assert res.kind == "Person" and res.agent_called is False and calls == []
    assert "LinkedIn profile" in res.note
    assert (await db().people.find_one({"_id": people._oid(pid)}))["record_kind"]["v"] == "Person"

    # a name that is not first + last, or a record with something wrong, still goes to the agent
    r = await writer.post("/api/people", json={"tier": "plus_one", "fields": {
        "name": "Dan", "linkedin": "https://www.linkedin.com/in/danbillson2"}})
    ctx = await invoke.make_context(r.json()["id"], Source("agent", "repair", "test"), None)
    res = await invoke.run(repair_tool.repair, ctx)
    assert res.agent_called is True


async def test_merge_documents_folds_every_entry_and_deletes_the_duplicate(writer, reader):
    await import_people(writer, "Name,LinkedIn,Company\nAda Okafor,https://linkedin.com/in/adaokafor,PayRail\n",
                        "a.csv", "member")
    keep = await db().people.find_one({"name.v": "Ada Okafor"})
    r = await writer.post("/api/people", json={"tier": "plus_one", "fields": {
        "name": "Ada Okafor", "linkedin": "https://www.linkedin.com/in/adaokafor/",
        "company": "Leafy", "bio": "Builds payments infrastructure"}})
    drop = r.json()["id"]
    groups = await people.duplicate_groups()
    assert len(groups) == 1 and groups[0]["same_name"] and groups[0]["ids"][0] == str(keep["_id"])

    r = await writer.post(f"/api/people/{keep['_id']}/merge/{drop}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {c["key"] for c in body["changes"]} >= {"company", "bio"}
    assert await db().people.count_documents({}) == 1
    doc = await db().people.find_one({"_id": keep["_id"]})
    assert doc["tier"] == "member"                                        # the kept document's tier
    assert [e["v"] for e in doc["company"]["h"]] == ["PayRail", "Leafy"]   # both entries, in order
    assert doc["company"]["h"][0]["src"]["kind"] == "import" and doc["company"]["h"][1]["src"]["kind"] == "ui"
    assert doc["bio"]["v"] == "Builds payments infrastructure"
    assert len(doc["linkedin"]["h"]) == 2                                  # a different spelling: kept too
    assert (await reader.get(f"/api/people/{drop}")).status_code == 404
    acts = (await writer.get("/api/activity?action=person.merge")).json()["items"]
    assert acts[0]["meta"]["dropped"] == drop
    assert (await writer.post(f"/api/people/{keep['_id']}/merge/{drop}")).status_code == 404
    assert (await writer.post(f"/api/people/{keep['_id']}/merge/{keep['_id']}")).status_code == 404


async def test_mcp_merge_people_and_cli_dedupe(writer, client_factory, capsys):
    from app import cli
    for tier, company in (("member", "PayRail"), ("plus_one", "Leafy")):
        await writer.post("/api/people", json={"tier": tier, "fields": {
            "name": "Bo Chen", "email": "bo@x.io", "company": company}})
    await writer.post("/api/people", json={"tier": "plus_one", "fields": {
        "name": "Robert Chen", "email": "bo@x.io"}})           # same email, another name: listed only
    await cli.dedupe(apply=False)
    out = capsys.readouterr().out
    assert "1 duplicate group(s)" in out and "DIFFERENT names" in out and "0 merged" in out
    assert await db().people.count_documents({}) == 3

    await writer.delete(f"/api/people/{(await db().people.find_one({'name.v': 'Robert Chen'}))['_id']}")
    await cli.dedupe(apply=True)
    out = capsys.readouterr().out
    assert "same name" in out and "1 merged" in out
    assert await db().people.count_documents({}) == 1
    doc = await db().people.find_one({})
    assert doc["tier"] == "member" and [e["v"] for e in doc["company"]["h"]] == ["PayRail", "Leafy"]

    key = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    r = await writer.post("/api/people", json={"tier": "plus_two", "fields": {
        "name": "Bo Chen", "email": "bo@x.io", "role": "CEO"}})
    err, res = await call(c, key, "merge_people", {"keep_id": str(doc["_id"]), "drop_id": r.json()["id"]})
    assert not err, res
    assert res["person"]["role"] == "CEO" and await db().people.count_documents({}) == 1
    err, _ = await call(c, key, "merge_people", {"keep_id": str(doc["_id"]), "drop_id": r.json()["id"]})
    assert err
