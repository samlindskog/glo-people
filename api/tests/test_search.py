import pytest

from app import embedder
from app.tools.search import FieldFilter, filter_to_mongo
from tests.conftest import import_people

PEOPLE = """Name,Role,Bio,One-liner,City,MRR
Ada,founder,payments infrastructure for merchants,fintech payments founder,Lagos,9
Bo,investor,climate venture investor,climate investor,Berlin,4
Cy,founder,payments and lending,,Lagos,7
Di,engineer,,robotics engineer,Austin,
"""


@pytest.fixture(autouse=True)
async def _data(writer):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    await import_people(writer, PEOPLE, "p.csv", "member")


def names(resp):
    return [h["person"]["name"] for h in resp.json()["results"]]


async def test_a_passage_against_every_embeddable_property(writer, reader):
    """`*` stands for every embeddable property: short values (role, company) and the provider
    profile count too, and the planner's validator accepts it."""
    from app import properties
    from app.tools import plan as plan_tool
    from app.tools.search import FieldQuery, SemanticQuery
    await import_people(writer, "Name,Role,Company,Bio\nAda Okafor,Head of Partnerships,PayRail,payments\n"
                                "Bo Chen,Farmer,Leafy,vertical farming\n", "p.csv", "member")
    cat = {p.key: p for p in await properties.catalog()}
    assert cat["role"].embeddable and cat["company"].embeddable and cat["enrichment_passage"].embeddable
    assert not cat["name"].embeddable and not cat["stage"].embeddable
    r = await reader.post("/api/search", json={"semantic": {"query": "partnerships lead", "fields": ["*"],
                                                            "mode": "max", "threshold": 0.1}})
    assert r.status_code == 200, r.text
    hits = r.json()["results"]
    assert hits and hits[0]["person"]["name"] == "Ada Okafor"
    assert "role" in hits[0]["field_scores"] and "*" not in hits[0]["field_scores"]
    sem = SemanticQuery(queries=[FieldQuery(field="bio", query="x"), FieldQuery(field="*", query="y")])
    expanded = sem.expanded(["bio", "role", "company"])
    assert [(q.field, q.query) for q in expanded.queries] == [("bio", "x"), ("company", "y"), ("role", "y")]
    assert SemanticQuery(query="q", fields=["bio"]).expanded(["bio", "role"]).fields == ["bio"]
    r = await reader.post("/api/search", json={"semantic": {"query": "q", "fields": ["*", "*"]}})
    assert r.status_code == 422                                           # compared twice
    plan = plan_tool.SearchPlan(semantic=[FieldQuery(field="*", query="payments")])
    assert plan.semantic[0].field == "*"


async def test_literal_filters(reader):
    s = lambda body: reader.post("/api/search", json=body)
    assert names(await s({"filters": [{"field": "name", "op": "contains", "value": "a"}]})) == [
        "Ada"]
    assert names(await s({"filters": [{"field": "city", "op": "eq", "value": "lagos"}]})) == [
        "Ada", "Cy"]
    assert names(await s({"filters": [{"field": "mrr", "op": "gte", "value": 7}]})) == [
        "Ada", "Cy"]
    assert names(await s({"filters": [{"field": "bio", "op": "exists", "value": False}]})) == [
        "Di"]
    assert names(await s({"filters": [{"field": "role", "op": "in",
                                       "value": ["Investor", "engineer"]}]})) == ["Bo", "Di"]
    assert names(await s({"filters": [{"field": "name", "op": "starts_with", "value": "c"},
                                      {"field": "city", "op": "regex", "value": "^lag"}]})) == [
        "Cy"]
    r = await s({"sort": {"field": "mrr", "dir": "desc"}, "limit": 2})
    assert names(r) == ["Ada", "Cy"] and r.json()["total"] == 4
    assert (await s({"filters": [{"field": "x y", "op": "eq", "value": 1}]})).status_code == 422
    assert (await s({"filters": [{"field": "name", "op": "regex", "value": "("}]})
            ).status_code == 422


def test_filter_translation_escapes():
    q = filter_to_mongo(FieldFilter(field="name", op="contains", value="a.b*"))
    assert q == {"name.v": {"$regex": r"a\.b\*", "$options": "i"}}


async def test_filters_see_current_values_only(reader, writer):
    pid = (await reader.post("/api/search", json={
        "filters": [{"field": "name", "op": "eq", "value": "Bo"}]})).json()["results"][0]["id"]
    await writer.patch(f"/api/people/{pid}", json={"fields": {"city": "Munich", "bio": None}})
    s = lambda f: reader.post("/api/search", json={"filters": [f]})
    assert names(await s({"field": "city", "op": "eq", "value": "berlin"})) == []
    assert names(await s({"field": "city", "op": "eq", "value": "munich"})) == ["Bo"]
    assert names(await s({"field": "bio", "op": "exists", "value": False})) == ["Bo", "Di"]
    r = await reader.post("/api/search", json={"sort": {"field": "city", "dir": "asc"}})
    assert names(r) == ["Di", "Ada", "Cy", "Bo"]


async def test_semantic_max_over_passages(reader, writer):
    ada = (await reader.post("/api/search", json={
        "filters": [{"field": "name", "op": "eq", "value": "Ada"}]})).json()["results"][0]["id"]
    await writer.patch(f"/api/people/{ada}", json={"fields": {"bio": "climate venture investor"}})
    for query in ("payments infrastructure for merchants", "climate venture investor"):
        r = await reader.post("/api/search", json={
            "semantic": {"query": query, "fields": ["bio"], "threshold": -1}})
        by = {h["person"]["name"]: h for h in r.json()["results"]}
        assert by["Ada"]["field_scores"]["bio"] == pytest.approx(1.0, abs=1e-3)
        assert by["Ada"]["score"] == pytest.approx(1.0, abs=1e-3)
    assert by["Bo"]["field_scores"]["bio"] == pytest.approx(1.0, abs=1e-3)
    assert by["Cy"]["field_scores"]["bio"] < 0.9


async def test_semantic_single_field_threshold(reader):
    body = {"semantic": {"query": "payments infrastructure", "fields": ["bio"],
                         "threshold": 0.0}}
    r = await reader.post("/api/search", json=body)
    hits = r.json()["results"]
    assert hits[0]["person"]["name"] == "Ada"
    assert hits[0]["score"] >= hits[-1]["score"]
    assert set(hits[0]["field_scores"]) == {"bio"}
    assert "Di" not in names(r)  # Di has no bio

    body["semantic"]["threshold"] = 0.99
    assert (await reader.post("/api/search", json=body)).json()["total"] == 0


async def test_semantic_avg_max_require_all(reader):
    base = {"query": "fintech payments founder", "fields": ["bio", "one_liner"],
            "threshold": -1}
    avg = (await reader.post("/api/search", json={"semantic": {**base, "mode": "avg"}})).json()
    mx = (await reader.post("/api/search", json={"semantic": {**base, "mode": "max"}})).json()
    by = lambda resp: {h["person"]["name"]: h for h in resp["results"]}
    a, m = by(avg), by(mx)
    ada_fs = a["Ada"]["field_scores"]
    assert a["Ada"]["score"] == pytest.approx(sum(ada_fs.values()) / 2, abs=1e-3)
    assert m["Ada"]["score"] == pytest.approx(max(ada_fs.values()), abs=1e-3)
    # Cy has bio but no one_liner: included unless require_all_fields
    assert "Cy" in a and set(a["Cy"]["field_scores"]) == {"bio"}
    req_all = (await reader.post("/api/search", json={
        "semantic": {**base, "require_all_fields": True}})).json()
    assert "Cy" not in by(req_all) and "Ada" in by(req_all)


async def test_semantic_plus_filters_and_tier(reader):
    r = await reader.post("/api/search", json={
        "filters": [{"field": "city", "op": "eq", "value": "Lagos"}],
        "semantic": {"query": "climate", "fields": ["bio"], "threshold": -1}})
    assert sorted(names(r)) == ["Ada", "Cy"]
    r = await reader.post("/api/search", json={
        "tier": "plus_one", "semantic": {"query": "x", "fields": ["bio"], "threshold": -1}})
    assert r.json()["total"] == 0


async def test_search_history(reader, writer):
    r = await reader.post("/api/search", json={
        "semantic": {"query": "payments", "fields": ["bio"]}})
    sid = r.json()["search_id"]
    mine = (await reader.get("/api/searches")).json()["items"]
    assert mine[0]["id"] == sid and mine[0]["params"]["semantic"]["query"] == "payments"
    assert mine[0]["source"] == "ui"
    assert (await writer.get("/api/searches")).json()["items"] == []
    everyone = (await writer.get("/api/searches?all=1")).json()["items"]
    assert any(s["id"] == sid for s in everyone)
    detail = (await writer.get(f"/api/searches/{sid}")).json()
    assert detail["result_ids"] == [h["id"] for h in r.json()["results"]]
    acts = (await writer.get("/api/activity?action=search")).json()["items"]
    assert acts and acts[0]["meta"]["semantic"]["query"] == "payments"


async def test_per_field_passages(reader):
    one = {"semantic": {"query": "fintech payments founder", "fields": ["bio", "one_liner"],
                        "threshold": -1}}
    split = {"semantic": {"queries": [{"field": "bio", "query": "fintech payments founder"},
                                      {"field": "one_liner", "query": "fintech payments founder"}],
                          "threshold": -1}}
    a = (await reader.post("/api/search", json=one)).json()
    b = (await reader.post("/api/search", json=split)).json()
    assert [(h["id"], h["score"], h["field_scores"]) for h in a["results"]] == \
        [(h["id"], h["score"], h["field_scores"]) for h in b["results"]]

    # a different passage per property: each property is scored against its own passage
    r = await reader.post("/api/search", json={"semantic": {
        "queries": [{"field": "bio", "query": "climate venture investor"},
                    {"field": "one_liner", "query": "robotics engineer"}],
        "mode": "max", "threshold": 0.5}})
    by = {h["person"]["name"]: h for h in r.json()["results"]}
    assert by["Bo"]["field_scores"]["bio"] == pytest.approx(1.0, abs=1e-3)
    assert by["Di"]["field_scores"]["one_liner"] == pytest.approx(1.0, abs=1e-3)
    assert "Ada" not in by
    # both forms together
    r = await reader.post("/api/search", json={"semantic": {
        "query": "payments", "fields": ["bio"],
        "queries": [{"field": "one_liner", "query": "climate investor"}], "threshold": -1}})
    assert set(r.json()["results"][0]["field_scores"]) <= {"bio", "one_liner"}
    acts = (await reader.get("/api/searches")).json()["items"]
    assert acts[0]["params"]["semantic"]["queries"][0]["field"] == "one_liner"


async def test_requests_are_validated_against_the_catalog(reader):
    s = lambda body: reader.post("/api/search", json=body)
    r = await s({"filters": [{"field": "nonexistent", "op": "eq", "value": 1}]})
    assert r.status_code == 422 and "unknown property" in r.text
    r = await s({"semantic": {"query": "x", "fields": ["mrr"]}})
    assert r.status_code == 422 and "not embeddable" in r.text
    r = await s({"semantic": {"query": "x", "fields": ["nonexistent"]}})
    assert r.status_code == 422
    r = await s({"semantic": {"query": "x", "fields": ["bio"],
                              "queries": [{"field": "bio", "query": "y"}]}})
    assert r.status_code == 422 and "more than once" in r.text
    assert (await s({"semantic": {"fields": ["bio"]}})).status_code == 422
    assert (await s({"semantic": {"query": "x"}})).status_code == 422
    assert (await s({"semantic": {"mode": "max"}})).status_code == 422
    r = await s({"sort": {"field": "nonexistent", "dir": "asc"}})
    assert r.status_code == 422 and "sort" in r.text


async def test_eq_on_a_url_or_email_matches_every_spelling(writer, reader):
    r = await writer.post("/api/people", json={"tier": "member", "fields": {
        "name": "Lin Ma", "linkedin": "https://www.linkedin.com/in/lin-ma/", "email": "Lin@Ma.io"}})
    lin = r.json()["id"]

    async def ids(field, value):
        res = await reader.post("/api/search", json={"filters": [{"field": field, "op": "eq", "value": value}]})
        assert res.status_code == 200, res.text
        return [p["id"] for p in res.json()["results"]]
    assert await ids("linkedin", "linkedin.com/in/lin-ma") == [lin]
    assert await ids("linkedin", "https://LinkedIn.com/in/lin-ma?utm_source=share") == [lin]
    assert await ids("email", "lin@ma.io") == [lin]
    assert await ids("name", "lin ma") == [lin]
    assert await ids("linkedin", "linkedin.com/in/someone-else") == []


async def test_a_tier_list_and_the_counts_above_tighter_thresholds(reader, writer):
    """`tier` takes several tiers in one call; a semantic result says how many hits reach each
    of a few tighter thresholds, so a wide result is narrowed without another search."""
    await import_people(writer, "Name,Bio\nEve,payments platform for merchants\n", "q.csv", "plus_one")
    s = lambda body: reader.post("/api/search", json=body)
    assert names(await s({"tier": "member", "filters": [{"field": "bio", "op": "contains", "value": "payments"}]})) == ["Ada", "Cy"]
    assert names(await s({"tier": ["member", "plus_one"],
                          "filters": [{"field": "bio", "op": "contains", "value": "payments"}]})) == ["Ada", "Cy", "Eve"]
    r = await s({"tier": ["plus_one"], "semantic": {"query": "payments", "fields": ["bio"], "threshold": 0.1}})
    assert names(r) == ["Eve"]
    r = await s({"tier": ["member", "plus_one"], "semantic": {"query": "payments infrastructure for merchants",
                                                            "fields": ["bio"], "threshold": 0.1}})
    data = r.json()
    assert set(names(r)) >= {"Ada", "Eve"}
    counts = data["at_or_above"]
    assert list(counts) == ["0.15", "0.2", "0.25", "0.3"]
    assert counts["0.15"] <= data["total"]
    assert all(counts[a] >= counts[b] for a, b in zip(list(counts), list(counts)[1:], strict=False))
    assert counts["0.3"] >= 1                                            # Ada's bio is the passage itself
    r = await s({"filters": [{"field": "bio", "op": "contains", "value": "payments"}]})
    assert r.json()["at_or_above"] is None                                # literal searches have no scores
    assert (await s({"tier": ["nope"]})).status_code == 422
    assert (await s({"tier": []})).status_code == 422                     # not "every tier"

    from app.tools.search import tighter_counts
    assert tighter_counts([0.5, 0.61, 0.9], 0.4) == {"0.45": 3, "0.5": 3, "0.55": 2, "0.6": 2}
    assert tighter_counts([0.99, 1.0], 0.9) == {"0.95": 2, "1": 1}        # steps stop at 1
