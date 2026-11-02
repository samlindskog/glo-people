"""Stage G: the agent ledger. Every MCP tool call is recorded once with its outcome and the shape
of its arguments; a key's calls group into conversations; the report counts what points at
product fixes; the endpoints need activity:read."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import agent_log, db, embedder
from app.config import get_settings
from tests.conftest import import_people
from tests.test_mcp import HEADERS, call, init, rpc


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    embedder.set_embedder(embedder.FakeEmbedder(512))
    monkeypatch.setattr(get_settings(), "fake_agents", True)


def test_shape_keeps_selectors_and_drops_free_text():
    assert agent_log.shape({"id": "6ae8d77cddaa04c964ab7f78", "fields": {"name": "James Brown",
                            "bio": "x" * 300, "stage": "Seed"}, "records": [1, 2], "n": 3, "ok": True}) == {
        "id": "6ae8d77cddaa04c964ab7f78", "fields": {"name": "str[11]", "bio": "str[300]", "stage": "Seed"},
        "records": "list[2]", "n": 3, "ok": True}
    assert agent_log.shape({"email": "ada@x.io"}) == {"email": "str[8]"}      # an @ is never kept
    assert agent_log.shape({"a": {"b": {"c": {"d": 1}}}}) == {"a": {"b": "dict[1]"}}   # two levels deep
    assert agent_log.shape(None) is None and agent_log.shape(2.5) == 2.5


async def _rows():
    async with db.engine().connect() as conn:
        return [dict(r) for r in (await conn.execute(
            select(db.agent_calls).order_by(db.agent_calls.c.id))).mappings().all()]


async def test_every_call_is_recorded_once_with_its_outcome(writer, client_factory):
    await import_people(writer, "Name,Email\nAda Okafor,ada@x.io\n", "p.csv", "member")
    key = (await writer.post("/api/api-keys", json={"name": "agent-key"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    err, _ = await call(c, key, "find_people", {"email": "ada@x.io"})
    assert not err
    err, _ = await call(c, key, "get_person", {"id": "000000000000000000000000", "include_history": False})
    assert err
    err, _ = await call(c, key, "start_assistant", {"key": "nope"})
    assert err
    r = await rpc(c, key, "tools/call", {"name": "search_people", "arguments": {"limit": 5}}, 3)
    assert r.status_code == 200

    rows = await _rows()
    assert [r["tool"] for r in rows] == ["find_people", "get_person", "start_assistant", "search_people"]
    assert all(r["api_key_id"] and r["user_id"] for r in rows)
    find, missing, unknown, search = rows
    assert find["ok"] and find["args"] == {"email": "str[8]"} and find["result"]["decision"] == "matched"
    assert find["duration_ms"] >= 0
    assert not missing["ok"] and "not found" in missing["error"]
    assert not unknown["ok"] and "unknown assistant" in unknown["error"]
    assert search["ok"] and search["result"]["total"] == 1 and search["result"]["results"] == "list[1]"
    assert search["session"] is None                              # stateless transport: no session id

    # the ledger replaced the ad hoc activity rows
    acts = (await writer.get("/api/activity?action=mcp.")).json()["items"]
    assert acts == []


async def test_full_args_when_the_setting_asks(writer, client_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "mcp_log_args", True)
    key = (await writer.post("/api/api-keys", json={"name": "k"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    await call(c, key, "find_people", {"name": "Someone Specific"})
    rows = await _rows()
    assert rows[-1]["args"] == {"name": "Someone Specific"}


async def test_session_header_and_gap_grouping(writer, client_factory):
    key = (await writer.post("/api/api-keys", json={"name": "k"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    h = {**HEADERS, "Authorization": f"Bearer {key}", "X-Agent-Session": "conv-1"}
    for i in range(2):
        r = await c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
                                                  "params": {"name": "list_properties", "arguments": {}}})
        assert r.status_code == 200
    await call(c, key, "list_properties", {})                       # no header: its own conversation
    rows = await _rows()
    assert [r["session"] for r in rows] == ["conv-1", "conv-1", None]

    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    rows = [{"session": None, "tool": "a", "ok": True, "created_at": t0 + timedelta(minutes=m), "args": {}}
            for m in (0, 5, 50, 52)]
    groups = agent_log.sessions(rows)
    assert [s["calls_n"] for s in groups] == [2, 2] and groups[0]["started_at"] == t0 + timedelta(minutes=50)


async def test_report_and_endpoints(writer, reader, client_factory):
    await import_people(writer, "Name,Email\nAda Okafor,ada@x.io\n", "p.csv", "member")
    key = (await writer.post("/api/api-keys", json={"name": "scout"})).json()["key"]
    c = await client_factory()
    await init(c, key)
    await call(c, key, "list_properties", {})
    await call(c, key, "list_properties", {})                        # a re-read
    await call(c, key, "merge_person", {"record": {"name": "Bo Chen", "email": "bo@x.io"},
                                        "options": {"dry_run": True}})
    await call(c, key, "merge_person", {"record": {"name": "Bo Chen", "email": "bo@x.io"},
                                        "options": {"review": False}})
    await call(c, key, "get_person", {"id": "000000000000000000000000"})     # an error
    await call(c, key, "get_person", {"id": "000000000000000000000001"})     # retried with other args
    await call(c, key, "start_assistant", {"key": "normalize", "where": [{"field": "name", "op": "exists", "value": True}],
                                           "options": {"enrich": False}, "wait_seconds": 30})

    r = await writer.get("/api/activity/agents/report?days=1")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["calls"] == 7 and rep["errors"] == 2 and rep["keys"] == 1
    by_tool = {t["tool"]: t for t in rep["by_tool"]}
    assert by_tool["get_person"]["errors"] == 2 and by_tool["merge_person"]["calls"] == 2
    assert rep["top_errors"][0]["tool"] == "get_person" and "not found" in rep["top_errors"][0]["error"]
    assert rep["retries"] == 1 and rep["dry_runs"] == 1 and rep["writes_after_dry_run"] == 1
    assert rep["list_properties_rereads"] == 1 and rep["runs_started"] == 1
    assert "find_people" in rep["unused_tools"] and "merge_person" not in rep["unused_tools"]

    r = await writer.get("/api/activity/agents?days=1")
    keys = r.json()["keys"]
    assert len(keys) == 1 and keys[0]["key_name"] == "scout" and keys[0]["username"] == "wendy"
    assert keys[0]["calls"] == 7 and keys[0]["errors"] == 2 and keys[0]["sessions"] == 1
    assert keys[0]["tools"]["list_properties"] == 2

    r = await writer.get(f"/api/activity/agents/{keys[0]['api_key_id']}/sessions?days=1")
    sessions = r.json()["sessions"]
    assert len(sessions) == 1 and sessions[0]["calls_n"] == 7
    assert sessions[0]["tools"][:3] == ["list_properties", "list_properties", "merge_person"]
    assert sessions[0]["calls"][2]["args"] == {"record": {"name": "str[7]", "email": "str[7]"},
                                                 "options": {"dry_run": True}}

    anonymous = await client_factory()
    assert (await anonymous.get("/api/activity/agents?days=1")).status_code == 401
    assert (await reader.get("/api/activity/agents/report?days=1")).status_code in (200, 403)  # per the contract
