"""The owned telemetry: every request in the request log with its id, every warning in the app
log tied to its request, nginx's lines over syslog, the reads behind the ops MCP."""

import json
import logging
import uuid

import pytest
from sqlalchemy import text

from app import db, ops
from tests.test_mcp import HEADERS, body


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
    h = (await reader.get("/ops/health")).json()
    assert h["postgres"] and h["git_sha"]
    assert (await reader.get("/ops/nope")).status_code == 404

    await writer.put("/api/boards/companies/acme", json={"name": "Acme"})
    await ops.flush()

    async def call(tool, args):
        r = await reader.post("/ops/mcp", headers=HEADERS,
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": tool, "arguments": args}})
        assert r.status_code == 200, r.text
        res = body(r)["result"]
        assert not res.get("isError"), res
        if "structuredContent" in res:
            data = res["structuredContent"]
            return data.get("result", data) if isinstance(data, dict) and len(data) == 1 else data
        return json.loads(res["content"][0]["text"])

    r = await reader.post("/ops/mcp", headers=HEADERS, json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}})
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
    assert isinstance(await call("ops_mcp_calls", {"minutes": 5}), list)
