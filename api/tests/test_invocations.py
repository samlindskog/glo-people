"""The staged transaction (level 1) and the tool invocation contract (level 2): one commit per
record, rebasing on a concurrent write, stopping before the commit, streaming activity."""

import asyncio
import json

import pytest
from pydantic_ai import ModelResponse
from pydantic_ai.exceptions import AgentRunError, UnexpectedModelBehavior
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app import people, properties
from app.history import Source
from app.mongo import db
from app.normalize import Parsed
from app.tools import invoke
from app.tools import normalize as normalize_tool
from app.tools import repair as repair_tool
from tests.conftest import import_people
from tests.test_tools import CSV, by_change
from tests.test_tools import _fakes as _fakes  # noqa: PLC0414  (the autouse fixture: fake embedder + agents)

SRC = Source("agent", "test", "t")


@pytest.fixture
async def people_by_name(writer):
    await import_people(writer, CSV, "a.csv", "member")
    return {d["name"]["v"]: str(d["_id"]) async for d in db().people.find({})}


async def _doc(pid: str) -> dict:
    return await db().people.find_one({"_id": people._oid(pid)})


# --- the transaction -----------------------------------------------------------------------------------

async def test_transaction_stages_values_and_norm_in_one_write(people_by_name, reader):
    cy = people_by_name["Cy"]
    before = await _doc(cy)
    tx = await people.Transaction.open(cy)
    assert tx is not None and not tx.dirty
    staged = await tx.set_values({"mrr": "20k"}, SRC, tag="a")
    assert [(c.key, c.action, c.value) for c in staged] == [("mrr", "set", "20k"),
                                                            ("mrr", "normalized", 20000)]
    assert tx.view["mrr"]["v"] == "20k"                      # later stages read the staged state
    staged = await tx.set_norm({"stage": Parsed("Pre-seed")}, SRC, tag="b")
    assert [(c.key, c.action, c.value) for c in staged] == [("stage", "normalized", "Pre-seed")]
    assert tx.changes("b") == staged and len(tx.changes()) == 3
    assert (await _doc(cy))["rev"] == before["rev"]          # nothing written yet
    res = await tx.commit(None)
    after = await _doc(cy)
    assert after["rev"] == before["rev"] + 1                 # one write
    assert after["mrr"]["v"] == "20k" and after["mrr"]["norm"]["v"] == 20000
    assert after["stage"]["norm"]["v"] == "Pre-seed" and after["stage"]["norm"]["h"][-1]["src"] == SRC.doc()
    assert res.person["norm"] == {"mrr": 20000, "stage": "Pre-seed"}
    with pytest.raises(people.PersonError):
        await tx.commit(None)                                # a transaction commits once
    acts = (await reader.get("/api/activity?action=person.update")).json()["items"]
    mine = [a for a in acts if a["target"] == cy]
    assert len(mine) == 1 and mine[0]["meta"]["set"] == ["mrr", "stage"]


async def test_transaction_rebases_on_a_concurrent_write(people_by_name, writer, reader):
    ada = people_by_name["Ada"]
    tx = await people.Transaction.open(ada)
    await tx.set_values({"bio": "payments for SMBs"}, SRC)
    # someone else writes while the tool thinks
    r = await writer.patch(f"/api/people/{ada}", json={"fields": {"notes": "called today"}})
    assert r.status_code == 200
    res = await tx.commit(None)
    person = res.person
    assert person["bio"] == "payments for SMBs" and person["notes"] == "called today"
    assert [e["v"] for e in person["history"]["notes"]] == ["met at demo day", "called today"]
    assert (await _doc(ada))["rev"] == 3                      # import, the patch, the commit
    assert [(c.key, c.action) for c in res.changes] == [("bio", "set")]


async def test_transaction_drops_a_normalised_value_whose_text_changed(people_by_name, writer):
    cy = people_by_name["Cy"]
    tx = await people.Transaction.open(cy)
    await tx.set_norm({"stage": Parsed("Pre-seed")}, SRC)      # judged "Pre-seed w/o revenue"
    r = await writer.patch(f"/api/people/{cy}", json={"fields": {"stage": "Series A"}})
    assert r.status_code == 200
    res = await tx.commit(None)
    assert res.person["stage"] == "Series A" and res.person["norm"]["stage"] == "Series A"
    assert [(c.key, c.action) for c in res.changes] == [("stage", "note")]
    assert "dropped" in res.changes[0].value


async def test_transaction_conflict_leaves_the_document_alone(people_by_name, monkeypatch):
    bo = people_by_name["Bo"]
    before = await _doc(bo)

    async def never(oid, doc, to_set):
        return False

    monkeypatch.setattr(people, "_cas_write", never)
    tx = await people.Transaction.open(bo)
    await tx.set_values({"notes": "x"}, SRC)
    with pytest.raises(people.ConflictError):
        await tx.commit(None)
    assert await _doc(bo) == before


# --- invocations ----------------------------------------------------------------------------------------

async def _sse(client, url: str) -> list[dict]:
    """The events of a Server-Sent Events stream, until the terminal `result` event."""
    events = []
    async with client.stream("GET", url) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
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
                if current["event"] == "result":
                    break
                current = {}
    return events


async def test_store_invocation_streams_its_activity_then_the_result(people_by_name, writer, reader):
    ada = people_by_name["Ada"]
    r = await writer.post(f"/api/people/{ada}/store", json={"passage": "notes: follows up next week"})
    assert r.status_code == 202, r.text
    handle = r.json()
    assert handle["tool"] == "store" and handle["status"] == "running" and handle["person_id"] == ada
    events = await _sse(reader, handle["events_url"])
    kinds = [e["event"] for e in events]
    assert kinds[0] == "step" and kinds[-1] == "result"
    assert "agent" in kinds and "write" in kinds
    assert [e["id"] for e in events] == list(range(1, len(events) + 1))
    assert any(e["data"]["message"] == "storage: thinking" for e in events)
    assert any(e["data"]["message"] == "notes: set" for e in events)
    assert events[-1]["data"]["data"] == {"status": "done", "credits": 0}
    done = (await reader.get(f"/api/invocations/{handle['id']}")).json()
    assert done["status"] == "done" and done["events"] == len(events)
    assert by_change(done["result"])[("notes", "set")]["value"] == "follows up next week"
    # replay from the middle
    later = await _sse(reader, handle["events_url"].replace("/events", "/events"))
    assert [e["id"] for e in later] == [e["id"] for e in events]
    # too late to stop
    assert (await writer.post(f"/api/invocations/{handle['id']}/stop")).status_code == 409
    assert (await reader.get("/api/invocations/nope")).status_code == 404
    acts = (await reader.get("/api/activity?action=person.store")).json()["items"]
    assert acts[0]["meta"]["invocation"] == handle["id"] and acts[0]["meta"]["status"] == "done"


async def test_a_stopped_tool_has_written_nothing(people_by_name, writer, reader):
    ada = people_by_name["Ada"]
    before = await _doc(ada)
    entered, gate = asyncio.Event(), asyncio.Event()

    async def slow_normaliser(messages, info):
        entered.set()
        await gate.wait()
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"items": []})])

    # the storage agent (fake) files the passage and creates a property; the normaliser then hangs
    with normalize_tool.agent.override(model=FunctionModel(slow_normaliser)):
        r = await writer.post(f"/api/people/{ada}/store", json={
            "passage": "stage: seed round closed. new hat size: 7"})
        assert r.status_code == 202, r.text
        inv_id = r.json()["id"]
        await asyncio.wait_for(entered.wait(), 10)
        assert await properties.get("hat_size") is not None          # created on the way
        r = await writer.post(f"/api/invocations/{inv_id}/stop")
        assert r.status_code == 200, r.text
        inv = invoke.get(inv_id)
        with pytest.raises(invoke.Stopped):
            await inv.wait()
        gate.set()
    assert inv.status == "stopped" and inv.events[-1].kind == "result"
    assert await _doc(ada) == before                                   # rev, history: untouched
    assert await properties.get("hat_size") is None                    # the unused property is gone
    assert (await reader.get(f"/api/invocations/{inv_id}")).json()["status"] == "stopped"
    assert (await writer.post(f"/api/invocations/{inv_id}/stop")).status_code == 409


async def test_a_composite_tool_commits_once(people_by_name, writer, reader):
    ada = people_by_name["Ada"]
    before = await _doc(ada)
    r = await writer.post(f"/api/people/{ada}/store?wait=true", json={
        "passage": "mrr: 20k. stage: seed round closed. sectors: lending"})
    assert r.status_code == 200, r.text
    res = r.json()
    by = by_change(res)
    assert by[("mrr", "set")] and by[("sectors", "append")] and by[("stage", "set")]
    assert by[("stage", "normalized")]["value"] == "Seed"            # the normaliser's decision
    assert res["normalization"]["agent_called"]
    after = await _doc(ada)
    assert after["rev"] == before["rev"] + 1                          # store + normalize: one write
    assert after["stage"]["norm"]["v"] == "Seed" and after["stage"]["norm"]["h"][-1]["src"]["by"] == "normalizer"
    acts = (await reader.get("/api/activity?action=person.update")).json()["items"]
    assert len([a for a in acts if a["target"] == ada]) == 1


async def test_repair_is_one_write_with_its_sources_kept(writer, reader):
    r = await writer.post("/api/people", json={"tier": "member", "fields": {
        "name": "andrÃ© niÃ±o", "website": "previously marketing manager at Gap Inc.",
        "notes": "met in June"}})
    pid = r.json()["id"]
    before = await _doc(pid)
    ctx = await invoke.make_context(pid, Source("agent", "repair", "t"))
    inv = invoke.invoke(repair_tool.repair, ctx)
    res = await inv.wait()
    assert isinstance(res, repair_tool.RepairResult)
    assert res.mechanical == ["name"] and res.kind == "Person"
    after = await _doc(pid)
    assert after["rev"] == before["rev"] + 1
    assert after["name"]["h"][-1]["src"] == {"kind": "system", "by": "repair"}
    assert after["record_kind"]["h"][-1]["src"] == {"kind": "agent", "by": "repair", "ref": "t"}
    assert after["website"]["v"] is None and after["notes"]["v"].endswith("Gap Inc.")
    kinds = [e.kind for e in inv.events]
    assert kinds.count("write") >= 2 and kinds[-1] == "result" and "agent" in kinds
    assert res.credits == 0 and res.calls == []
    assert inv.person is not None and inv.person["name"] == "André Niño"


async def test_a_failing_tool_reports_the_error_and_cleans_up(people_by_name, writer, reader):
    ada = people_by_name["Ada"]
    before = await _doc(ada)

    def broken(messages, info):
        raise UnexpectedModelBehavior("model down")

    with normalize_tool.agent.override(model=FunctionModel(broken)):
        r = await writer.post(f"/api/people/{ada}/store", json={
            "passage": "stage: seed round closed. new hat size: 7"})
        inv = invoke.get(r.json()["id"])
        with pytest.raises(AgentRunError):
            await inv.wait()
    assert inv.status == "failed" and "model down" in inv.error
    assert await _doc(ada) == before and await properties.get("hat_size") is None
    r = await writer.post(f"/api/people/{ada}/store?wait=true", json={"passage": "x: y"})
    assert r.status_code == 200                                    # fakes: nothing to file, fine
    with normalize_tool.agent.override(model=FunctionModel(broken)):
        r = await writer.post(f"/api/people/{ada}/store?wait=true", json={"passage": "stage: late"})
    assert r.status_code == 502 and "agent failed" in r.text


async def test_invocation_404_before_anything_starts(writer):
    r = await writer.post("/api/people/000000000000000000000000/store", json={"passage": "x"})
    assert r.status_code == 404
    r = await writer.post("/api/people/not-an-id/store", json={"passage": "x"})
    assert r.status_code == 422
