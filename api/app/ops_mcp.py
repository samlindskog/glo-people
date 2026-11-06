"""The ops MCP (level 4): read-only debugging tools over the owned telemetry, served under
`/ops/` on the same process and reachable only over the tunnel (prod's nginx exposes `/ops/` on
the WireGuard address alone; nothing public routes it). No key: the network is the boundary, and
every tool only reads. `/ops/health` answers JSON for a plain curl."""

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from pydantic import Field
from sqlalchemy import select
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app import agent_log, db, ops
from app.assistants import runner
from app.config import get_settings

INSTRUCTIONS = """\
glo-people operations, read-only: what the application recorded about its own running. Start
with ops_status (the build, uptime, schema head, databases) and ops_summary (the last hour at a
glance). ops_requests lists requests — the API's own rows carry the route template and the
X-Request-Id, nginx's rows (source nginx) carry what the app never saw: 429s, 502s, the CF-Ray.
ops_request(id) shows one request with the log lines written during it. ops_logs searches the
warnings and errors; ops_errors is the shortcut for tracebacks. ops_mcp_calls is the agent
ledger (every public MCP tool call, who, how long, outcome), ops_activity the audit log of writes,
ops_runs the assistant runs in flight. Logfire holds the agent runs themselves (the pydantic-ai
traces); everything here is the application's own record."""

mcp = MCPServer(name="glo-people-ops", instructions=INSTRUCTIONS)


@mcp.tool()
async def ops_status() -> dict:
    """Tool: the build (git sha), uptime, alembic head, whether Postgres and Mongo answer, the
    syslog listener, and the telemetry queues."""
    out = await ops.status()
    sched = runner._scheduler
    out["runs_in_flight_items"] = sched._inflight_total() if sched is not None else None
    return out


@mcp.tool()
async def ops_summary(minutes: Annotated[int, Field(ge=1, le=20160)] = 60) -> dict:
    """Tool: the last `minutes` at a glance: requests by source, host and status class, the ten
    slowest routes (p95, max), warning and error counts, and what is queued."""
    return await ops.summary(minutes)


@mcp.tool()
async def ops_requests(
    minutes: Annotated[int, Field(ge=1, le=20160)] = 60,
    source: Annotated[str | None, Field(description="api | nginx")] = None,
    host: Annotated[str | None, Field(description="Substring of the host, e.g. glojobs")] = None,
    path: Annotated[str | None, Field(description="Substring of the path")] = None,
    min_status: Annotated[int | None, Field(description="e.g. 400 for failures only")] = None,
    min_duration_ms: int | None = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
) -> list[dict]:
    """Tool: requests, newest first. API rows have the route template, the status, the
    duration, the client, and `attrs` (a public page's outcome, the MCP tool called…); nginx
    rows have what reached nginx, including what never reached the app."""
    return await ops.requests(minutes=minutes, source=source, host=host, path=path,
                              min_status=min_status, min_duration_ms=min_duration_ms, limit=limit)


@mcp.tool()
async def ops_request(request_id: Annotated[str, Field(description="X-Request-Id or CF-Ray")]) -> dict:
    """Tool: one request — every row carrying the id — and the log lines written during it."""
    return await ops.request(request_id)


@mcp.tool()
async def ops_logs(
    minutes: Annotated[int, Field(ge=1, le=20160)] = 60,
    level: Annotated[str, Field(description="WARNING (default) | ERROR | CRITICAL: this and above")] = "WARNING",
    contains: Annotated[str | None, Field(description="Substring of the message")] = None,
    logger: Annotated[str | None, Field(description="Logger name prefix, e.g. app.boards")] = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
) -> list[dict]:
    """Tool: the app's warnings and errors, newest first, each with its traceback and the
    request it happened in."""
    return await ops.logs(minutes=minutes, level=level, contains=contains, logger=logger, limit=limit)


@mcp.tool()
async def ops_errors(minutes: Annotated[int, Field(ge=1, le=20160)] = 1440,
                     limit: Annotated[int, Field(ge=1, le=500)] = 50) -> list[dict]:
    """Tool: errors and above in the last `minutes` (a day by default), with tracebacks."""
    return await ops.logs(minutes=minutes, level="ERROR", limit=limit)


@mcp.tool()
async def ops_mcp_calls(
    minutes: Annotated[int, Field(ge=1, le=20160)] = 60,
    tool: Annotated[str | None, Field(description="Only this tool")] = None,
    failed_only: bool = False,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
) -> list[dict]:
    """Tool: the public MCP's ledger: every tool call with its key, session, argument shape,
    duration and outcome, newest first."""
    rows = await agent_log.calls(datetime.now(UTC) - timedelta(minutes=minutes), limit=20_000)
    if tool:
        rows = [r for r in rows if r.get("tool") == tool]
    if failed_only:
        rows = [r for r in rows if not r.get("ok")]
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return [_jsonable(r) for r in rows[:limit]]


@mcp.tool()
async def ops_activity(minutes: Annotated[int, Field(ge=1, le=20160)] = 60,
                       action: Annotated[str | None, Field(description="Prefix, e.g. board or person")] = None,
                       limit: Annotated[int, Field(ge=1, le=1000)] = 100) -> list[dict]:
    """Tool: the audit log of writes (who did what to which record), newest first."""
    stmt = select(db.activity).where(db.activity.c.created_at >= datetime.now(UTC) - timedelta(minutes=minutes))
    if action:
        stmt = stmt.where(db.activity.c.action.like(f"{action}%"))
    stmt = stmt.order_by(db.activity.c.created_at.desc()).limit(limit)
    async with db.engine().connect() as conn:
        return [_jsonable(dict(r._mapping)) for r in await conn.execute(stmt)]


@mcp.tool()
async def ops_runs(status: Annotated[str | None, Field(description="running | paused | queued | "
                                                                   "done | failed | cancelled; "
                                                                   "default: the live ones")] = None,
                   limit: Annotated[int, Field(ge=1, le=200)] = 20) -> dict:
    """Tool: assistant runs — the live ones by default — with their progress."""
    if status:
        return await runner.list_runs(limit, 0, status)
    out: dict[str, Any] = {}
    for st in ("running", "pausing", "paused", "queued"):
        page = await runner.list_runs(limit, 0, st)
        if page.get("items"):
            out[st] = page["items"]
    return out or {"live": []}


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(row, default=str))


async def _health(scope: Scope, receive: Receive, send: Send) -> None:
    body = json.dumps(await ops.status()).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


def build_app() -> ASGIApp:
    """`/ops/mcp` is the MCP, `/ops/health` the JSON status; anything else under /ops/ is 404.
    Every request must carry one of `ops_hosts` as its Host (421 otherwise) and no foreign Origin
    (403): the surface has no key, so a page in the operator's browser must not be able to reach
    it by DNS rebinding while the tunnel is up."""
    hosts = [h.strip() for h in get_settings().ops_hosts.split(",") if h.strip()]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=[*hosts, *(h + ":*" for h in hosts)])
    gate = TransportSecurityMiddleware(security)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True,
                                      json_response=False, transport_security=security)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        refused = await gate.validate_request(Request(scope))
        if refused is not None:
            await refused(scope, receive, send)
            return
        path = scope["path"]
        if path in ("/health", "/health/"):
            await _health(scope, receive, send)
        elif path == "/mcp" or path.startswith("/mcp/"):
            await mcp_app(ops.sub_scope(scope, "/mcp"), receive, send)
        else:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"not found"})

    return app
