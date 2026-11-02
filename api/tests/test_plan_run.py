"""Planning and watching runs over MCP: a run's items carry the model tokens they spent, a run
reports its progress, `plan_run` estimates an enrichment from history before it starts, and an
agent can read the balance and the recent runs."""

import pytest

from app import embedder
from app.assistants import credits as credits_svc
from app.assistants import runner
from app.config import get_settings
from app.mongo import db
from tests.conftest import import_people
from tests.test_mcp import call, init

CSV = """Name,LinkedIn,Website,Stage
Ada Okafor,https://www.linkedin.com/in/adaokafor,,pre seed
Bo Chen,,https://bochen.me,
Cy Diaz,,,seed
"""


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    monkeypatch.setattr(get_settings(), "fake_agents", True)


async def _key_client(writer, client_factory):
    key = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    return c, key


async def test_items_carry_model_tokens_and_runs_report_progress(writer, client_factory):
    await import_people(writer, CSV, "p.csv", "member")
    # Cy has no LinkedIn profile, so the repair agent (not the fast path) judges the record
    cy = str((await db().people.find_one({"name.v": "Cy Diaz"}))["_id"])
    r = await writer.post("/api/assistants/normalize/runs",
                          json={"person_ids": [cy], "options": {"enrich": False}})
    run = await runner.wait(r.json()["id"])
    detail = (await writer.get(f"/api/assistants/runs/{run['id']}")).json()
    item = detail["items"][0]
    assert item["result"]["tokens"]["requests"] >= 1          # the fakes count as model requests
    assert item["result"]["tokens"]["input"] > 0
    prog = detail["progress"]
    assert prog["finished"] == 1 and prog["remaining"] == 0 and prog["percent"] == 100
    assert prog["by_status"] == {"done": 1} and prog["tokens"]["input"] > 0
    assert prog["elapsed_s"] is not None and prog["eta_min"] is None     # finished: nothing left

    averages = await runner.history_averages("normalize")
    assert averages["items"] == 1 and averages["tokens_per_item"] > 0
    assert averages["seconds_per_item"] is not None and averages["credits_per_item"] == 0


async def test_plan_run_estimates_an_enrichment_from_history(writer, client_factory, monkeypatch):
    await import_people(writer, CSV, "p.csv", "member")
    ids = {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}
    # pretend Ada was enriched yesterday for 6 credits through FullEnrich, and someone else for 60
    # through People Data Labs: the plan averages only the items that asked the same providers
    from sqlalchemy import insert

    from app import db as pg
    from app.assistants.runner import _now
    async with pg.engine().begin() as conn:
        rid = (await conn.execute(insert(pg.workflow_runs).values(
            workflow="enrich", status="done", total=2, done=2, credits_used=66,
            params={}).returning(pg.workflow_runs.c.id))).scalar_one()
        await conn.execute(insert(pg.workflow_run_items).values(
            run_id=rid, workflow="enrich", person_id=ids["Ada Okafor"], status="done", credits=6,
            result={"tokens": {"input": 3000, "output": 500}, "providers": {"fullenrich": "matched"}},
            started_at=_now(), finished_at=_now()))
        await conn.execute(insert(pg.workflow_run_items).values(
            run_id=rid, workflow="enrich", person_id="000000000000000000000009", status="done",
            credits=60, result={"tokens": {"input": 9000, "output": 900},
                                "providers": {"people-data-labs": "matched", "fullenrich": "skipped"}},
            started_at=_now(), finished_at=_now()))

    async def balance():
        return {"remaining": 100_000, "plan": 500_000, "period_end": None, "reserved": 0, "error": None}
    monkeypatch.setattr(credits_svc, "summary", balance)

    c, key = await _key_client(writer, client_factory)
    err, plan = await call(c, key, "plan_run", {"key": "enrich", "all_with_field": "name"})
    assert not err, plan
    assert plan["people"] == 3 and plan["with_linkedin"] == 1 and plan["with_site_only"] == 1
    assert plan["nothing_to_work_with"] == 1 and plan["already_enriched"] == 1
    assert plan["eligible"] == 1                                        # Bo: a site, not yet enriched
    assert plan["averages"]["items"] == 1 and plan["averages"]["with"] == {"providers": ["fullenrich"]}
    assert plan["averages"]["credits_per_item"] == 6 and plan["averages"]["tokens_per_item"] == 3500
    assert plan["estimate"]["credits"] == 6 and plan["estimate"]["tokens"] == 3500
    assert plan["estimate"]["basis"] == "history"
    assert plan["cost"]["providers"] == [{"key": "fullenrich", "label": "FullEnrich",
                                         "credits_per_match": 5, "last_billed": None}]
    assert plan["cost"]["provider_policy"] == "gaps" and plan["warnings"] == []
    assert plan["fits_balance"] is True and plan["reserved_per_item"] == 16
    assert plan["minimum_cap"] == 16 * plan["estimate"]["concurrency"]
    assert plan["recommended_options"]["max_credits"] == plan["minimum_cap"]   # 8 would throttle
    assert plan["balance"]["remaining"] == 100_000

    err, plan = await call(c, key, "plan_run", {"key": "enrich", "all_with_field": "name",
                                                "options": {"refresh": True}})
    assert not err and plan["already_enriched"] == 0 and plan["eligible"] == 2
    # opting into People Data Labs: its own history (60 a person), its own reservation, a warning
    err, plan = await call(c, key, "plan_run", {"key": "enrich", "all_with_field": "name",
                                                "options": {"providers": ["people-data-labs", "fullenrich"]}})
    assert not err and plan["averages"]["items"] == 1 and plan["averages"]["credits_per_item"] == 60
    assert plan["estimate"]["credits"] == 60 and plan["reserved_per_item"] == 76
    assert [p["credits_per_match"] for p in plan["cost"]["providers"]] == [60, 5]
    assert len(plan["warnings"]) == 1 and "People Data Labs bills 60" in plan["warnings"][0]
    # no comparable history at all: the estimate comes from the listed prices
    err, plan = await call(c, key, "plan_run", {"key": "enrich", "all_with_field": "name",
                                                "options": {"providers": []}})
    assert not err and plan["averages"]["items"] == 0 and plan["estimate"]["basis"] == "listed prices"
    assert plan["estimate"]["credits"] == 2 and plan["cost"]["typical_per_item"] == 2

    err, plan = await call(c, key, "plan_run", {"key": "normalize", "person_ids": list(ids.values())})
    assert not err and plan["people"] == 3 and "eligible" not in plan and plan["balance"] is None
    err, _ = await call(c, key, "plan_run", {"key": "merge", "person_ids": list(ids.values())})
    assert err
    r = await writer.post("/api/assistants/enrich/plan", json={"where": [{"field": "linkedin", "op": "exists", "value": True}]})
    assert r.status_code == 200 and r.json()["people"] == 1


async def test_get_credits_and_list_runs(writer, client_factory, monkeypatch):
    async def balance():
        return {"remaining": 42, "plan": 500, "period_end": None, "reserved": 0, "error": None}
    monkeypatch.setattr(credits_svc, "summary", balance)
    await import_people(writer, CSV, "p.csv", "member")
    c, key = await _key_client(writer, client_factory)
    err, res = await call(c, key, "get_credits", {})
    assert not err and res["credits"]["remaining"] == 42
    assert {p["key"]: p["credits_per_match"] for p in res["prices"]} == {"fullenrich": 5, "people-data-labs": 60}
    err, res = await call(c, key, "list_runs", {"limit": 5})
    assert not err and res["runs"][0]["workflow"] == "merge" and res["runs"][0]["status"] == "done"
    err, res = await call(c, key, "list_runs", {"status": "active"})
    assert not err and res["runs"] == []
    err, run = await call(c, key, "get_run", {"run_id": res and (await writer.get("/api/assistants/runs")).json()["items"][0]["id"]})
    assert not err and run["progress"]["percent"] == 100 and "items_per_min" in run["progress"]
    assert run["progress"]["reserved"] == 0 and run["progress"]["in_flight"] == 0


async def test_enrichment_on_the_record_counts_as_recent_even_without_run_history(writer, client_factory):
    """A database copied from another stack keeps the enrichment entries but not the runs: both
    the plan and the TTL skip read the record itself."""
    from datetime import UTC, datetime

    from app import people
    from app.history import Source
    await import_people(writer, CSV, "p.csv", "member")
    ids = {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}
    tx = await people.Transaction.open(ids["Ada Okafor"])
    await tx.set_values({"enrichment_passage": "# filed on another stack", "linkedin_headline": "CEO"},
                        Source("agent", "enrich/storage", "run:1/item:1"))
    await tx.commit(None)
    c, key = await _key_client(writer, client_factory)
    err, plan = await call(c, key, "plan_run", {"key": "enrich", "all_with_field": "linkedin"})
    assert not err and plan["people"] == 1 and plan["already_enriched"] == 1 and plan["eligible"] == 0
    assert plan["estimate"]["truncated"] is False and plan["estimate"]["concurrency"] >= 1

    r = await writer.post("/api/assistants/enrich/runs",
                          json={"person_ids": [ids["Ada Okafor"]], "options": {"max_credits": 10}})
    run = await runner.wait(r.json()["id"])
    item = (await writer.get(f"/api/assistants/runs/{run['id']}")).json()["items"][0]
    assert item["status"] == "skipped" and item["credits"] == 0
    assert "use refresh" in item["result"]["skipped"][0]["reason"]
    assert datetime.now(UTC).date().isoformat() in item["result"]["text"]
    # the run view says what the run's options cost, so a reader knows before resuming one
    err, view = await call(c, key, "get_run", {"run_id": run["id"]})
    assert not err and view["params"]["options"]["providers"] == ["fullenrich"]
    assert [p["key"] for p in view["cost"]["providers"]] == ["fullenrich"] and view["warnings"] == []
    err, view = await call(c, key, "get_run", {"run_id": (await writer.post(
        "/api/assistants/enrich/runs", json={"person_ids": [ids["Ada Okafor"]], "options": {
            "max_credits": 10, "providers": ["people-data-labs"]}})).json()["id"]})
    assert not err and len(view["warnings"]) == 1 and view["cost"]["worst_case_per_item"] == 71
    await runner.drain()
