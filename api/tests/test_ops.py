"""The owned telemetry: every request in the request log with its id, every warning in the app
log tied to its request, nginx's lines over syslog, the reads behind the ops MCP."""

import json
import logging
import sys
import uuid

import pytest
from sqlalchemy import text

from app import db, ops
from tests.test_mcp import HEADERS, body

OPS = {"host": "127.0.0.1"}            # one of ops_hosts; the test client's own Host is "test"


@pytest.fixture(autouse=True)
async def _clean_ops():
    await fresh()
    yield


async def fresh() -> None:
    """Forget every request so far (the fixtures' logins included)."""
    await ops.flush()
    async with db.engine().begin() as conn:
        await conn.execute(text("TRUNCATE request_log, app_log"))
    ops._requests.clear()
    ops._logs.clear()


async def test_every_request_is_logged_with_its_id(writer, reader):
    await fresh()
    r = await reader.get("/api/boards/guide")
    rid = r.headers["x-request-id"]
    assert r.status_code == 200 and len(rid) == 16
    r2 = await writer.get("/api/boards/talent/draft?person_id=000000000000000000000000")
    assert r2.status_code == 404
    assert (await reader.get("/api/health")).status_code == 200      # never logged
    await ops.flush()

    rows = await ops.requests(minutes=5)
    assert [x["route"] for x in rows] == ["/api/boards/talent/draft", "/api/boards/guide"]
    assert rows[0]["path"] == "/api/boards/talent/draft?person_id=000000000000000000000000"
    assert rows[0]["status"] == 404 and rows[1]["request_id"] == rid and rows[1]["source"] == "api"
    assert all(x["path"] != "/api/health" for x in rows)
    assert (await ops.requests(minutes=5, min_status=400))[0]["route"] == "/api/boards/talent/draft"
    assert (await ops.requests(minutes=5, path="guide"))[0]["request_id"] == rid


async def test_pages_tools_and_warnings_land_on_the_request(writer, client_factory):
    anon = await client_factory()
    r = await anon.get(f"/glojobs/{uuid.uuid4()}")
    assert r.status_code == 404
    rid = r.headers["x-request-id"]
    await ops.flush()
    row = (await ops.request(rid))["requests"][0]
    assert row["route"] == "/glojobs/{board_id}" and row["attrs"] == {"page": True, "outcome": "unknown"}

    # a tool call over the public MCP: the tool and its outcome on the row
    key = (await writer.post("/api/api-keys", json={"name": "k"})).json()["key"]
    r = await anon.post("/mcp", headers={**HEADERS, "Authorization": f"Bearer {key}"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "get_person", "arguments": {"id": "000000000000000000000000"}}})
    assert r.status_code == 200
    await ops.flush()
    row = (await ops.request(r.headers["x-request-id"]))["requests"][0]
    assert row["route"] == "/mcp" and row["attrs"]["tool"] == "get_person" and row["attrs"]["ok"] is False
    assert row["attrs"]["method"] == "tools/call"

    # every MCP POST notes its JSON-RPC method, so a handshake or a held stream is named too
    r = await anon.post("/mcp", headers={**HEADERS, "Authorization": f"Bearer {key}"},
                        json={"jsonrpc": "2.0", "id": 2, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "0"}}})
    assert r.status_code == 200
    await ops.flush()
    assert (await ops.request(r.headers["x-request-id"]))["requests"][0]["attrs"] == {"method": "initialize"}
    summary = await ops.summary(5)
    assert [(t["tool"], t["n"], t["failed"]) for t in summary["mcp_tools"]] == [("get_person", 1, 1)]
    assert {(o["method"], o["n"]) for o in summary["mcp_other_posts"]} == {("initialize", 1)}
    assert all(s["route"] != "/mcp" for s in summary["slowest_routes"])
    groups = await ops.request_groups("tool", minutes=5)
    assert [(g["tool"], g["n"], g["failed"]) for g in groups] == [("get_person", 1, 0)]
    by_status = {g["status"]: g["n"] for g in await ops.request_groups("status", minutes=5, path="/mcp")}
    assert by_status == {200: 2}
    with pytest.raises(ValueError):
        await ops.request_groups("attrs", minutes=5)

    # a warning logged during a request carries the request id; errors carry the traceback
    ops.begin("req-abc")
    logging.getLogger("app.test").warning("careful %s", 1)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("app.test").exception("it broke")
    logging.getLogger("app.test").info("not recorded")
    await ops.flush()
    got = await ops.request("req-abc")
    assert [(ln["level"], ln["message"]) for ln in got["logs"]] == [("WARNING", "careful 1"), ("ERROR", "it broke")]
    assert "RuntimeError: boom" in got["logs"][1]["traceback"]
    assert [ln["message"] for ln in await ops.logs(minutes=5, level="ERROR")] == ["it broke"]
    assert [ln["message"] for ln in await ops.logs(minutes=5, contains="careful")] == ["careful 1"]
    assert (await ops.summary(5))["log_levels"] == {"WARNING": 1, "ERROR": 1}


def test_nginx_lines_over_syslog():
    line = (b'<190>Nov  6 04:00:01 nginx: {"ts":"2026-11-06T04:00:01+00:00","host":"glojobs.app",'
            b'"method":"GET","path":"/abc?x=1","status":429,"ms":0.004,"ip":"1.2.3.4",'
            b'"ua":"curl/8","ray":"9a1b2c3d4e5f-SJC","up":"-","bytes":162}')
    row = ops.parse_nginx(line)
    assert row["source"] == "nginx" and row["host"] == "glojobs.app" and row["status"] == 429
    assert row["duration_ms"] == 4 and row["request_id"] == "9a1b2c3d4e5f-SJC" and row["path"] == "/abc?x=1"
    assert row["attrs"] == {"bytes": 162} and row["ts"].isoformat().startswith("2026-11-06T04:00:01")
    assert ops.parse_nginx(b"<190>Nov  6 04:00:01 nginx: not json") is None
    assert ops.parse_nginx(b"garbage") is None
    assert ops.ingest_syslog(line) and ops._requests[-1]["status"] == 429


async def test_status_summary_and_prune(reader):
    st = await ops.status()
    assert st["postgres"] and st["mongo"] and st["alembic_head"] and st["git_sha"]
    await fresh()
    await reader.get("/api/boards/guide")
    await ops.flush()
    s = await ops.summary(5)
    assert any(r["source"] == "api" and r["status_class"] == 200 and r["n"] >= 1 for r in s["requests"])
    assert s["slowest_routes"][0]["route"] == "/api/boards/guide"
    async with db.engine().begin() as conn:
        await conn.execute(text("UPDATE request_log SET ts = ts - interval '40 days'"))
    assert await ops.prune(30) >= 1
    assert await ops.requests(minutes=60 * 24 * 60) == []


async def test_ops_mcp_reads_everything(reader, writer):
    """The ops MCP under /ops/mcp: no key, read-only; /ops/health answers JSON."""
    h = (await reader.get("/ops/health", headers=OPS)).json()
    assert h["postgres"] and h["git_sha"]
    assert (await reader.get("/ops/nope", headers=OPS)).status_code == 404

    await writer.put("/api/boards/companies/acme", json={"name": "Acme"})
    await ops.flush()

    async def call(tool, args):
        r = await reader.post("/ops/mcp", headers={**HEADERS, **OPS},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": tool, "arguments": args}})
        assert r.status_code == 200, r.text
        res = body(r)["result"]
        assert not res.get("isError"), res
        if "structuredContent" in res:
            data = res["structuredContent"]
            return data.get("result", data) if isinstance(data, dict) and len(data) == 1 else data
        return json.loads(res["content"][0]["text"])

    r = await reader.post("/ops/mcp", headers={**HEADERS, **OPS},
                          json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}})
    tools = {t["name"] for t in body(r)["result"]["tools"]}
    assert {"ops_status", "ops_summary", "ops_requests", "ops_request", "ops_logs", "ops_errors",
            "ops_mcp_calls", "ops_activity", "ops_runs"} <= tools

    st = await call("ops_status", {})
    assert st["postgres"] and "runs_in_flight_items" in st
    reqs = await call("ops_requests", {"minutes": 5, "path": "companies"})
    assert reqs[0]["route"] == "/api/boards/companies/{slug}" and reqs[0]["method"] == "PUT"
    one = await call("ops_request", {"request_id": reqs[0]["request_id"]})
    assert one["requests"][0]["status"] == 200
    acts = await call("ops_activity", {"minutes": 5, "action": "board"})
    assert acts[0]["action"] == "board_company.put" and acts[0]["target"] == "acme"
    assert await call("ops_errors", {}) == []
    assert await call("ops_runs", {}) == {"live": []}

    # compact rows leave out what repeats; the full columns on request; groups instead of rows
    assert set(reqs[0]) <= {"ts", "source", "host", "method", "path", "route", "status", "duration_ms",
                            "client_ip", "user_agent", "request_id", "attrs"}
    assert "id" not in reqs[0] and "attrs" not in reqs[0] and len(reqs[0]["ts"]) == 19
    full = await call("ops_requests", {"minutes": 5, "path": "companies", "compact": False})
    assert "id" in full[0] and full[0]["attrs"] == {}
    groups = await call("ops_requests", {"minutes": 5, "group_by": "route", "source": "api"})
    assert any(g["route"] == "/api/boards/companies/{slug}" and g["n"] == 1 for g in groups)
    assert {"n", "p95_ms", "max_ms", "failed"} <= set(groups[0])

    # the ledger without shapes, each call labelled with its conversation
    key = (await writer.post("/api/api-keys", json={"name": "k"})).json()["key"]
    for _ in range(2):
        r = await reader.post("/mcp", headers={**HEADERS, "Authorization": f"Bearer {key}"},
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "list_properties", "arguments": {}}})
        assert r.status_code == 200
    calls = await call("ops_mcp_calls", {"minutes": 5})
    assert [c["tool"] for c in calls] == ["list_properties", "list_properties"]
    assert "args" not in calls[0] and "result" not in calls[0] and "error" not in calls[0]
    assert calls[0]["conversation"] == calls[1]["conversation"] and calls[0]["conversation"][:2] == "20"
    calls = await call("ops_mcp_calls", {"minutes": 5, "shapes": True, "limit": 1})
    assert len(calls) == 1 and calls[0]["args"] == {} and calls[0]["result"] is not None


async def test_ops_answers_only_to_its_own_hosts(reader):
    """DNS rebinding: a page in the operator's browser that resolves its own name to the tunnel
    address still sends that name as Host, and an Origin; /ops/ refuses both."""
    listing = {"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}}
    assert (await reader.get("/ops/health")).status_code == 421                   # Host: test
    assert (await reader.post("/ops/mcp", headers=HEADERS, json=listing)).status_code == 421
    r = await reader.get("/ops/health", headers={**OPS, "origin": "http://evil.example"})
    assert r.status_code == 403
    assert (await reader.get("/ops/health", headers={"host": "localhost:8000"})).status_code == 200
    r = await reader.post("/ops/mcp", headers={**HEADERS, **OPS}, json=listing)
    assert r.status_code == 200 and body(r)["result"]["tools"]


def row(path: str, **over) -> dict:
    base = {"source": "api", "host": "h", "method": "GET", "path": path, "route": path, "status": 200,
            "duration_ms": 1, "client_ip": "1.1.1.1", "user_agent": "ua", "request_id": None, "attrs": {}}
    return {**base, **over, "ts": over.get("ts") or ops.datetime.now(ops.UTC)}


async def paths() -> list[str]:
    async with db.engine().begin() as conn:
        return list((await conn.execute(text("SELECT path FROM request_log ORDER BY path"))).scalars())


async def test_a_refused_row_is_dropped_alone():
    """One row Postgres refuses (a type it cannot bind) never holds the others back, and is
    counted as dropped rather than retried forever."""
    before = ops._dropped
    ops._requests[:] = [row("/bad", status="x"), row("/good")]
    await ops.flush()
    assert ops._requests == [] and ops._dropped == before + 1
    assert await paths() == ["/good"]


def test_nul_bytes_are_cleaned_and_tracebacks_capped():
    ops.record_request(**{**row("/a\x00b"), "ts": None})
    assert ops._requests[-1]["path"] == "/ab"
    try:
        raise ValueError("x" * 100_000)
    except ValueError:
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom\x00", (), sys.exc_info())
    ops.record_log(rec)
    entry = ops._logs[-1]
    assert entry["message"] == "boom" and len(entry["traceback"]) == ops.TRACEBACK_CHARS
    assert entry["traceback"].endswith("x" * 50)
    ops._requests.clear()
    ops._logs.clear()


async def test_rows_wait_when_none_goes_in(monkeypatch):
    """A database whose schema is behind the build refuses every row for a while (the app starts
    before the migration runs): that is not the rows' fault, so they wait and none is dropped."""
    before, calls = ops._dropped, []

    async def behind(table, rows):
        calls.append(len(rows))
        raise RuntimeError("column does not exist")

    monkeypatch.setattr(ops, "_insert", behind)
    ops._requests[:] = [row("/a"), row("/b"), row("/c"), row("/d"), row("/e")]
    await ops.flush()
    assert [r["path"] for r in ops._requests] == ["/a", "/b", "/c", "/d", "/e"]
    assert ops._dropped == before and calls == [5, 1, 1, 1]           # the batch, then three alone
    ops._requests.clear()


async def test_rows_wait_for_the_database_oldest_giving_way(monkeypatch):
    """While the database is down the rows wait; past the queue's capacity the oldest give way to
    the newest, and every row lost is counted."""
    monkeypatch.setattr(ops, "MAX_QUEUE", 3)
    ops._requests[:] = [row("/a"), row("/b"), row("/c")]
    before = ops._dropped

    class Down:
        def begin(self):
            if len(ops._requests) == 0:
                ops._requests.append(row("/d"))               # arrives while the batch is in flight
            raise ConnectionError("down")

    monkeypatch.setattr(db, "engine", lambda: Down())
    await ops.flush()
    assert [r["path"] for r in ops._requests] == ["/b", "/c", "/d"] and ops._dropped == before + 1
    ops._requests.clear()
