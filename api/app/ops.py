"""Owned operational telemetry (level 1): what the application itself records about its own
running, kept in the same Postgres and read over the ops MCP on the tunnel (`app.ops_mcp`).

Two logs. `request_log` holds one row per HTTP request: the API's own (`source` api, written by
the dispatcher in `app.main` around every request, with the route template, the status, the
duration and an `X-Request-Id` that goes back to the client) and nginx's (`source` nginx, shipped
over syslog/UDP as JSON lines, which is how a 429 from rate limiting or a 502 while the API
restarts — requests the app never sees — still get recorded). `app_log` holds every warning and
error the app logs, with its traceback and the request id of the request it happened in, through
a logging handler installed at startup. Pages and tools add facts to their request's row with
`note(...)`. Rows older than `ops_retention_days` are pruned hourly.

Writes never block a request: rows queue in memory and a background task flushes them in
batches; if the database is down the rows wait (the oldest giving way once the queue is full),
and a row the database refuses is dropped on its own, never holding the rest back.
Logfire stays for the agents (the pydantic-ai runs); everything here is ours."""

import asyncio
import contextvars
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, text

from app import db, mongo
from app.config import get_settings

log = logging.getLogger(__name__)

STARTED_AT = datetime.now(UTC)
MAX_QUEUE = 20_000                 # rows waiting for a flush, each log; beyond that, dropped
TRACEBACK_CHARS = 20_000           # the end of a traceback is the part that matters
PROBE_ROWS = 3                     # refused rows tried alone before a batch counts as not the rows' fault
FLUSH_EVERY_S = 1.0
PRUNE_EVERY_S = 3600.0
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("ops_request_id", default=None)
_notes: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("ops_notes", default=None)

_requests: list[dict[str, Any]] = []
_logs: list[dict[str, Any]] = []
_flusher: asyncio.Task | None = None
_transport: asyncio.DatagramTransport | None = None
_dropped = 0


# --- the request in flight -------------------------------------------------------------------------

def begin(request_id: str | None = None) -> str:
    """Give the current task a request id (a fresh one unless the caller has one) and an empty
    note sheet; returns the id, which the dispatcher sends back as X-Request-Id."""
    rid = request_id or uuid.uuid4().hex[:16]
    _request_id.set(rid)
    _notes.set({})
    return rid


def request_id() -> str | None:
    return _request_id.get()


def note(**facts: Any) -> None:
    """Add facts to the current request's row (`attrs`): a page's outcome, the MCP tool called…
    Outside a request this is a no-op."""
    sheet = _notes.get()
    if sheet is not None:
        sheet.update({k: v for k, v in facts.items() if v is not None})


def notes() -> dict[str, Any]:
    return dict(_notes.get() or {})


def sub_scope(scope: dict[str, Any], prefix: str) -> dict[str, Any]:
    """The scope of a request handed on to an app mounted at `prefix`: the path without it."""
    sub = dict(scope)
    sub["path"] = scope["path"][len(prefix):] or "/"
    sub["raw_path"] = sub["path"].encode()
    return sub


def _text(s: str, limit: int) -> str:
    """Cut to `limit` and free of NUL, which Postgres refuses in text and json alike."""
    return s.replace("\x00", "")[:limit]


def record_request(*, source: str, host: str, method: str, path: str, route: str, status: int,
                   duration_ms: int, client_ip: str, user_agent: str, request_id: str | None,
                   attrs: dict[str, Any] | None = None, ts: datetime | None = None) -> None:
    _enqueue(_requests, {"ts": ts or datetime.now(UTC), "source": source, "host": _text(host, 200),
                         "method": _text(method, 8), "path": _text(path, 2000),
                         "route": _text(route, 200), "status": status, "duration_ms": duration_ms,
                         "client_ip": _text(client_ip, 64), "user_agent": _text(user_agent, 500),
                         "request_id": request_id, "attrs": attrs or {}})


def record_log(record: logging.LogRecord) -> None:
    tb = None
    if record.exc_info and record.exc_info[0] is not None:
        tb = logging.Formatter().formatException(record.exc_info).replace("\x00", "")[-TRACEBACK_CHARS:]
    try:
        message = record.getMessage()
    except (TypeError, ValueError, KeyError, IndexError):                 # a bad format string
        message = str(record.msg)
    _enqueue(_logs, {"ts": datetime.fromtimestamp(record.created, UTC), "level": record.levelname[:8],
                     "logger": _text(record.name, 200), "message": _text(message, 4000),
                     "traceback": tb, "request_id": _request_id.get()})


def _enqueue(queue: list[dict[str, Any]], row: dict[str, Any]) -> None:
    global _dropped
    if len(queue) >= MAX_QUEUE:
        _dropped += 1
        return
    queue.append(row)


class Handler(logging.Handler):
    """The logging handler that feeds `app_log`: warnings and above, from any logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(__name__):                     # never our own flush failures
            return
        try:
            record_log(record)
        except (TypeError, ValueError, OSError):                  # logging must not raise
            self.handleError(record)


# --- nginx's access log, over syslog ---------------------------------------------------------------

def parse_nginx(datagram: bytes | str) -> dict[str, Any] | None:
    """A JSON access-log line as nginx's syslog driver sends it (`<pri>date host tag: {...}`)
    into a request row, or None for anything else."""
    text_ = datagram.decode("utf-8", "replace") if isinstance(datagram, bytes) else datagram
    start = text_.find("{")
    if start < 0:
        return None
    try:
        d = json.loads(text_[start:])
    except ValueError:
        return None
    try:
        ts = datetime.fromisoformat(d["ts"]) if d.get("ts") else datetime.now(UTC)
        return {"ts": ts, "source": "nginx", "host": str(d.get("host", "")),
                "method": str(d.get("method", "")), "path": str(d.get("path", "")), "route": "",
                "status": int(d.get("status", 0)), "duration_ms": int(float(d.get("ms", 0)) * 1000),
                "client_ip": str(d.get("ip", "")), "user_agent": str(d.get("ua", "")),
                "request_id": (str(d.get("ray") or "")[:32] or None),
                "attrs": {k: v for k, v in (("upstream", d.get("up")), ("bytes", d.get("bytes")))
                          if v not in (None, "", "-")}}
    except (TypeError, ValueError):
        return None


def ingest_syslog(datagram: bytes) -> bool:
    row = parse_nginx(datagram)
    if row is None:
        return False
    record_request(**row)
    return True


class _Syslog(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr: Any) -> None:
        ingest_syslog(data)


# --- the background work -----------------------------------------------------------------------------

async def start() -> None:
    """Start the flusher and, when configured, the syslog listener (idempotent)."""
    global _flusher, _transport
    if _flusher is None:
        _flusher = asyncio.create_task(_run(), name="ops-flusher")
    port = get_settings().ops_syslog_port
    if port and _transport is None:
        try:
            _transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                _Syslog, local_addr=("0.0.0.0", port), reuse_port=True)
        except OSError as e:
            log.warning("ops: syslog listener not started on udp/%s: %s", port, e)


async def stop() -> None:
    global _flusher, _transport
    if _transport is not None:
        _transport.close()
        _transport = None
    if _flusher is not None:
        _flusher.cancel()
        try:
            await _flusher
        except asyncio.CancelledError:
            pass
        _flusher = None
    await flush()


async def flush() -> None:
    """Write everything queued now. When a batch is refused the rows go one by one: the ones
    refused among others that go in are bad rows, dropped and counted; when none goes in (the
    database is down, or its schema is behind this build) the rows wait in front of the queue,
    the oldest giving way once it is full."""
    global _dropped
    for table, queue in ((db.request_log, _requests), (db.app_log, _logs)):
        rows = list(queue)
        queue.clear()
        if not rows:
            continue
        try:
            await _insert(table, rows)
            continue
        except Exception:
            log.debug("ops: flush of %d %s rows failed", len(rows), table.name, exc_info=True)
        written, refused = 0, 0
        for row in rows:
            try:
                await _insert(table, [row])
                written += 1
            except Exception:
                refused += 1
                if written == 0 and refused >= PROBE_ROWS:       # nothing goes in: not the rows' fault
                    break
                if written:
                    log.warning("ops: a %s row was refused and dropped", table.name, exc_info=True)
        if written == 0:
            queue[:0] = rows                                      # back in front, the oldest give way
            overflow = len(queue) - MAX_QUEUE
            if overflow > 0:
                del queue[:overflow]
                _dropped += overflow
        else:
            _dropped += refused


async def _insert(table: Any, rows: list[dict[str, Any]]) -> None:
    async with db.engine().begin() as conn:
        await conn.execute(insert(table), rows)



async def _run() -> None:
    last_prune = 0.0
    while True:
        await asyncio.sleep(FLUSH_EVERY_S)
        await flush()
        if time.monotonic() - last_prune > PRUNE_EVERY_S:
            last_prune = time.monotonic()
            try:
                await prune(get_settings().ops_retention_days)
            except Exception:
                log.debug("ops: prune failed", exc_info=True)


async def prune(days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    async with db.engine().begin() as conn:
        a = await conn.execute(delete(db.request_log).where(db.request_log.c.ts < cutoff))
        b = await conn.execute(delete(db.app_log).where(db.app_log.c.ts < cutoff))
    return (a.rowcount or 0) + (b.rowcount or 0)


# --- reads ---------------------------------------------------------------------------------------------

def _row(r: Any) -> dict[str, Any]:
    out = dict(r._mapping)
    for k, v in out.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


def _since(minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=max(1, minutes))


async def requests(*, minutes: int = 60, source: str | None = None, host: str | None = None,
                   path: str | None = None, min_status: int | None = None,
                   min_duration_ms: int | None = None, limit: int = 100) -> list[dict]:
    stmt = select(db.request_log).where(db.request_log.c.ts >= _since(minutes))
    if source:
        stmt = stmt.where(db.request_log.c.source == source)
    if host:
        stmt = stmt.where(db.request_log.c.host.ilike(f"%{host}%"))
    if path:
        stmt = stmt.where(db.request_log.c.path.ilike(f"%{path}%"))
    if min_status:
        stmt = stmt.where(db.request_log.c.status >= min_status)
    if min_duration_ms:
        stmt = stmt.where(db.request_log.c.duration_ms >= min_duration_ms)
    stmt = stmt.order_by(db.request_log.c.ts.desc()).limit(min(max(1, limit), 1000))
    async with db.engine().connect() as conn:
        return [_row(r) for r in await conn.execute(stmt)]


async def request(request_id: str) -> dict[str, Any]:
    """One request (every row that carries the id: the API's and nginx's) and its log lines."""
    async with db.engine().connect() as conn:
        rows = [_row(r) for r in await conn.execute(
            select(db.request_log).where(db.request_log.c.request_id == request_id)
            .order_by(db.request_log.c.ts))]
        lines = [_row(r) for r in await conn.execute(
            select(db.app_log).where(db.app_log.c.request_id == request_id).order_by(db.app_log.c.ts))]
    return {"requests": rows, "logs": lines}


async def logs(*, minutes: int = 60, level: str = "WARNING", contains: str | None = None,
               logger: str | None = None, limit: int = 100) -> list[dict]:
    level = level.upper()
    wanted = LEVELS[LEVELS.index(level):] if level in LEVELS else LEVELS
    stmt = select(db.app_log).where(db.app_log.c.ts >= _since(minutes), db.app_log.c.level.in_(wanted))
    if contains:
        stmt = stmt.where(db.app_log.c.message.ilike(f"%{contains}%"))
    if logger:
        stmt = stmt.where(db.app_log.c.logger.ilike(f"{logger}%"))
    stmt = stmt.order_by(db.app_log.c.ts.desc()).limit(min(max(1, limit), 1000))
    async with db.engine().connect() as conn:
        return [_row(r) for r in await conn.execute(stmt)]


async def summary(minutes: int = 60) -> dict[str, Any]:
    """The last `minutes` at a glance: requests by source, host and status class, the slowest
    routes, and the warning and error counts."""
    since = _since(minutes)
    cls = (func.floor(db.request_log.c.status / 100) * 100).label("status_class")
    async with db.engine().connect() as conn:
        by_host = [_row(r) for r in await conn.execute(
            select(db.request_log.c.source, db.request_log.c.host, cls, func.count().label("n"))
            .where(db.request_log.c.ts >= since)
            .group_by(db.request_log.c.source, db.request_log.c.host, cls)
            .order_by(db.request_log.c.source, db.request_log.c.host, cls))]
        slow = [_row(r) for r in await conn.execute(
            select(db.request_log.c.route, func.count().label("n"),
                   func.percentile_cont(0.95).within_group(db.request_log.c.duration_ms).label("p95_ms"),
                   func.max(db.request_log.c.duration_ms).label("max_ms"))
            .where(db.request_log.c.ts >= since, db.request_log.c.source == "api",
                   db.request_log.c.route != "")
            .group_by(db.request_log.c.route).order_by(text("p95_ms DESC")).limit(10))]
        levels = {r.level: r.n for r in await conn.execute(
            select(db.app_log.c.level, func.count().label("n")).where(db.app_log.c.ts >= since)
            .group_by(db.app_log.c.level))}
    return {"minutes": minutes, "requests": by_host, "slowest_routes": slow, "log_levels": levels,
            "queued": {"requests": len(_requests), "logs": len(_logs), "dropped": _dropped}}


async def status() -> dict[str, Any]:
    """What is running: the build, uptime, the schema head, whether the databases answer."""
    s = get_settings()
    out: dict[str, Any] = {"git_sha": s.git_sha, "started_at": STARTED_AT.isoformat(),
                           "uptime_s": int((datetime.now(UTC) - STARTED_AT).total_seconds()),
                           "retention_days": s.ops_retention_days,
                           "syslog_listener": _transport is not None,
                           "queued": {"requests": len(_requests), "logs": len(_logs), "dropped": _dropped}}
    try:
        async with db.engine().connect() as conn:
            out["alembic_head"] = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            out["postgres"] = True
    except Exception as e:
        out["postgres"], out["postgres_error"] = False, str(e)[:200]
        log.debug("ops: postgres ping failed", exc_info=True)
    try:
        await mongo.db().command("ping")
        out["mongo"] = True
    except Exception as e:
        out["mongo"], out["mongo_error"] = False, str(e)[:200]
        log.debug("ops: mongo ping failed", exc_info=True)
    return out
