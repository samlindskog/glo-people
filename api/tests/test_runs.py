"""Runs that pause, resume, stop and stream: the scheduler's behaviour with items in flight, the
event stream, the credits balance and the MCP run tools."""

import asyncio
import json

import pytest
from pydantic_ai import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app import people
from app.assistants import credits, runner
from app.config import get_settings
from app.mongo import db
from app.tools import normalize as normalize_tool
from tests.conftest import import_people
from tests.test_mcp import call, init
from tests.test_tools import CSV
from tests.test_tools import _fakes as _fakes  # noqa: PLC0414  (fake embedder + agents)


@pytest.fixture
async def people_by_name(writer):
    await import_people(writer, CSV, "a.csv", "member")
    return {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}


class Gate:
    """A normaliser that waits at the gate: items with an unmapped value hang until released."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def model(self, messages, info):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"items": [
            {"field": "stage", "value": "Pre-seed"}]})])


async def _start(writer, ids, **options):
    r = await writer.post("/api/assistants/normalize/runs",
                          json={"person_ids": ids, "options": {"provider": False, **options}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _run_sse(client, url: str) -> list[dict]:
    """A run's Server-Sent Events until the run's state says it has finished."""
    events = []
    async with client.stream("GET", url) as r:
        assert r.status_code == 200, await r.aread()
        current: dict = {}
        async for line in r.aiter_lines():
            if line.startswith("event:"):
                current["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("id:"):
                current["id"] = int(line.split(":", 1)[1])
            elif line.startswith("data:"):
                current["data"] = json.loads(line.split(":", 1)[1])
            elif line == "" and current:
                events.append(current)
                d = current.get("data", {})
                if current["event"] == "run" and d.get("type") == "run" \
                        and d.get("status") in ("done", "failed", "cancelled"):
                    break
                current = {}
    return events


async def _until(predicate, timeout=10):
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)


async def test_stop_cancels_in_flight_tools_and_skips_the_queue(people_by_name, writer, reader,
                                                                  monkeypatch):
    monkeypatch.setattr(get_settings(), "assistant_concurrency", 1)
    cy, ada = people_by_name["Cy"], people_by_name["Ada"]
    before = await db().people.find_one({"_id": people._oid(cy)})
    gate = Gate()
    with normalize_tool.agent.override(model=FunctionModel(gate.model)):
        try:
            run_id = await _start(writer, [cy, ada])     # Cy first: its stage text needs the agent
            await asyncio.wait_for(gate.entered.wait(), 10)
            run = (await reader.get(f"/api/assistants/runs/{run_id}")).json()
            assert run["status"] == "running"
            statuses = {it["person_id"]: it["status"] for it in run["items"]}
            assert statuses == {cy: "running", ada: "queued"}
            r = await writer.post(f"/api/assistants/runs/{run_id}/stop")
            assert r.status_code == 200, r.text
        finally:
            gate.release.set()
    run = await runner.wait(run_id)
    assert run["status"] == "cancelled" and run["done"] == 2
    items = {it["person_id"]: it for it in (await reader.get(f"/api/assistants/runs/{run_id}")).json()["items"]}
    assert items[cy]["status"] == "skipped" and items[cy]["error"] == "stopped"
    assert items[ada]["status"] == "skipped" and items[ada]["error"] == "stopped"
    after = await db().people.find_one({"_id": people._oid(cy)})
    assert after == before                                  # the in-flight tool wrote nothing
    assert (await writer.post(f"/api/assistants/runs/{run_id}/stop")).status_code == 409
    assert (await writer.post(f"/api/assistants/runs/{run_id}/resume")).status_code == 409


async def test_pause_lets_in_flight_items_finish_then_resumes(people_by_name, writer, reader,
                                                               monkeypatch):
    monkeypatch.setattr(get_settings(), "assistant_concurrency", 1)
    cy, ada, bo = (people_by_name[n] for n in ("Cy", "Ada", "Bo"))
    gate = Gate()
    with normalize_tool.agent.override(model=FunctionModel(gate.model)):
        try:
            run_id = await _start(writer, [cy, ada, bo])
            await asyncio.wait_for(gate.entered.wait(), 10)
            r = await writer.post(f"/api/assistants/runs/{run_id}/pause")
            assert r.status_code == 200 and r.json()["status"] == "pausing", r.text
            assert r.json()["pause_reason"] == "requested"
        finally:
            gate.release.set()
        await _until(lambda: _status(reader, run_id, "paused"))
    run = (await reader.get(f"/api/assistants/runs/{run_id}")).json()
    statuses = {it["person_id"]: it["status"] for it in run["items"]}
    assert statuses == {cy: "done", ada: "queued", bo: "queued"} and run["done"] == 1
    assert (await reader.get(f"/api/people/{cy}")).json()["norm"]["stage"] == "Pre-seed"
    r = await writer.post(f"/api/assistants/runs/{run_id}/resume")
    assert r.status_code == 200 and r.json()["status"] == "running"
    run = await runner.wait(run_id)
    assert run["status"] == "done" and run["done"] == 3
    kinds = [e["kind"] for e in await runner.events(run_id)]
    assert kinds.count("item") == 3


async def _status(client, run_id, wanted):
    return (await client.get(f"/api/assistants/runs/{run_id}")).json()["status"] == wanted


async def test_two_runs_share_the_process_wide_limit(people_by_name, writer, reader, monkeypatch):
    monkeypatch.setattr(get_settings(), "assistant_concurrency", 2)
    cy = people_by_name["Cy"]
    # two more people whose stage needs the agent, so every item blocks at the gate
    extra = []
    for name in ("Di", "Ed"):
        r = await writer.post("/api/people", json={"tier": "member", "fields": {
            "name": name, "stage": "Pre-seed w/o revenue"}})
        extra.append(r.json()["id"])
    gate = Gate()
    with normalize_tool.agent.override(model=FunctionModel(gate.model)):
        try:
            a = await _start(writer, [cy, extra[0]])
            b = await _start(writer, [extra[1]])
            await asyncio.wait_for(gate.entered.wait(), 10)
            await asyncio.sleep(0.3)
            ra = (await reader.get(f"/api/assistants/runs/{a}")).json()
            rb = (await reader.get(f"/api/assistants/runs/{b}")).json()
            running = [it for it in ra["items"] + rb["items"] if it["status"] == "running"]
            assert len(running) == 2                               # the global limit, over both runs
            assert rb["status"] == "running" and rb["items"][0]["status"] == "queued"
            assert runner.reserved_credits() == 0                  # provider off: nothing to reserve
        finally:
            gate.release.set()
    for run_id in (a, b):
        assert (await runner.wait(run_id))["status"] == "done"


async def test_run_events_stream_replays_then_follows(people_by_name, writer, reader):
    cy = people_by_name["Cy"]
    gate = Gate()
    with normalize_tool.agent.override(model=FunctionModel(gate.model)):
        try:
            run_id = await _start(writer, [cy])
            await asyncio.wait_for(gate.entered.wait(), 10)
            task = asyncio.create_task(_run_sse(reader, f"/api/assistants/runs/{run_id}/events"))
            await asyncio.sleep(0.2)
        finally:
            gate.release.set()
        events = await asyncio.wait_for(task, 20)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run" and events[0]["data"]["message"] == "started"
    assert "step" in kinds and "agent" in kinds and "item" in kinds
    ids = [e["id"] for e in events if "id" in e]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    runs = [e["data"] for e in events if e["event"] == "run" and "status" in e["data"]
            and "type" in e["data"]]
    assert runs[-1]["status"] == "done"
    # a late reader replays everything and sees the finished state
    again = await _run_sse(reader, f"/api/assistants/runs/{run_id}/events")
    assert [e["id"] for e in again if "id" in e] == ids
    assert (await reader.get("/api/assistants/runs/999/events")).status_code == 404


async def test_credits_balance_endpoint(reader, monkeypatch):
    assert (await reader.get("/api/credits")).json() is None       # no key in tests

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def credit_usage(self):
            return {"remaining": 1234, "plan": 5000, "period_start": None, "period_end": "2026-11-30"}

        async def aclose(self):
            pass

    monkeypatch.setattr(get_settings(), "firecrawl_api_key", "x")
    monkeypatch.setattr(credits, "AsyncFirecrawl", FakeClient)
    credits.invalidate()
    body = (await reader.get("/api/credits")).json()
    assert body["remaining"] == 1234 and body["plan"] == 5000 and body["reserved"] == 0
    assert body["error"] is None


async def test_mcp_run_tools(people_by_name, writer, reader, client_factory):
    w = (await writer.post("/api/api-keys", json={"name": "w"})).json()["key"]
    rk = (await reader.post("/api/api-keys", json={"name": "r"})).json()["key"]
    c = await client_factory()
    await init(c, rk)
    err, specs = await call(c, rk, "list_assistants", {})
    assert not err, specs
    by_key = {s["key"]: s for s in specs}
    assert {"enrich", "normalize"} <= set(by_key) and by_key["enrich"]["scope"] == "people"
    err, _ = await call(c, rk, "start_assistant", {"key": "normalize", "person_ids": [people_by_name["Cy"]]})
    assert err                                                      # reader: no write tools
    await init(c, w)
    err, run = await call(c, w, "start_assistant", {
        "key": "normalize", "person_ids": [people_by_name["Cy"]], "options": {"provider": False},
        "label": "cy only"})
    assert not err, run
    assert run["status"] == "running" and run["label"] == "cy only" and run["total"] == 1
    await runner.wait(run["id"])
    err, got = await call(c, w, "get_run", {"run_id": run["id"]})
    assert not err, got
    assert got["status"] == "done" and got["items"][0]["status"] == "done"
    assert any(e["kind"] == "item" for e in got["events"])
    err, _ = await call(c, w, "pause_run", {"run_id": run["id"]})
    assert err
    err, _ = await call(c, w, "start_assistant", {"key": "nope", "person_ids": ["x"]})
    assert err
