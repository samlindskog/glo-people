"""The ledger of what agents do over MCP (level 0, beside `activity`): one row per tool call,
with who called (api key, user), the tool, the outcome the agent saw, how long it took, and the
*shape* of the arguments and the result rather than their values, so the ledger holds no one's
free text. `sessions` turns one key's rows into conversations; `report` computes the numbers that
point at product fixes (which tools fail agents and how, what they retry, what they never use)."""

import logging
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import delete, select

from app import db
from app.auth import Principal

log = logging.getLogger(__name__)

SESSION_GAP = timedelta(minutes=30)       # calls of one key closer than this are one conversation
RETRY_WINDOW = timedelta(seconds=60)      # the same tool again this soon is a retry
KEEP_SCALAR_CHARS = 24                    # a short selector-like string is kept as a value
MAX_ERROR_CHARS = 400


@dataclass
class Call:
    tool: str
    principal: Principal | None
    session: str | None
    args: Any
    ok: bool
    error: str | None
    duration_ms: int
    result: Any = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# --- shapes ---------------------------------------------------------------------------------------

def shape(value: Any, depth: int = 0) -> Any:
    """A value reduced to its shape: dict keys with the shapes of their values, `list[n]`,
    `str[n]`; numbers, booleans and short strings without spaces or an @ (ids, keys, enum names,
    tiers) are kept as they are. Two levels deep; deeper values become their type."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) <= KEEP_SCALAR_CHARS and " " not in value and "@" not in value:
            return value
        return f"str[{len(value)}]"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        if depth >= 2:
            return f"dict[{len(value)}]"
        return {str(k): shape(v, depth + 1) for k, v in list(value.items())[:40]}
    return type(value).__name__


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)


# --- writing ----------------------------------------------------------------------------------------

async def record(call: Call, *, full_args: bool = False) -> None:
    """Append one call. Never lets a logging failure break the tool call."""
    try:
        async with db.engine().begin() as conn:
            await conn.execute(db.agent_calls.insert().values(
                api_key_id=call.principal.api_key_id if call.principal else None,
                user_id=call.principal.user_id if call.principal else None,
                session=call.session[:64] if call.session else None,
                tool=call.tool[:64],
                args=_jsonable(call.args) if full_args else shape(call.args),
                result=shape(call.result) if call.result is not None else None,
                ok=call.ok,
                error=(call.error or "")[:MAX_ERROR_CHARS] or None,
                duration_ms=call.duration_ms,
                created_at=call.created_at,
            ))
    except Exception:
        log.exception("agent call not recorded: %s", call.tool)


async def prune(retention_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    async with db.engine().begin() as conn:
        res = await conn.execute(delete(db.agent_calls).where(db.agent_calls.c.created_at < cutoff))
    return res.rowcount or 0


# --- reading ----------------------------------------------------------------------------------------

def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    for k in ("created_at",):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    return d


async def calls(since: datetime | None, api_key_id: int | None = None, limit: int = 20_000) -> list[dict]:
    a = db.agent_calls
    q = select(a).order_by(a.c.created_at, a.c.id).limit(limit)
    if since is not None:
        q = q.where(a.c.created_at >= since)
    if api_key_id is not None:
        q = q.where(a.c.api_key_id == api_key_id)
    async with db.engine().connect() as conn:
        return [dict(r) for r in (await conn.execute(q)).mappings().all()]


def sessions(rows: list[dict]) -> list[dict[str, Any]]:
    """One key's calls (oldest first) grouped into conversations: by the session header when the
    client sends one, else by a gap of `SESSION_GAP` between calls. Newest conversation first."""
    out: list[dict[str, Any]] = []
    by_header: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for r in rows:
        if r.get("session"):
            s = by_header.get(r["session"])
            if s is None:
                s = by_header[r["session"]] = {"session": r["session"], "calls": []}
                out.append(s)
            s["calls"].append(r)
            continue
        if current is None or r["created_at"] - current["calls"][-1]["created_at"] > SESSION_GAP:
            current = {"session": None, "calls": []}
            out.append(current)
        current["calls"].append(r)
    for s in out:
        cs = s["calls"]
        s["started_at"] = cs[0]["created_at"]
        s["ended_at"] = cs[-1]["created_at"]
        s["calls_n"] = len(cs)
        s["errors"] = sum(1 for c in cs if not c["ok"])
        s["tools"] = [c["tool"] for c in cs]
    out.sort(key=lambda s: s["started_at"], reverse=True)
    return out


async def by_key(since: datetime | None) -> list[dict[str, Any]]:
    """Per api key: calls, errors, conversations, tool mix, first and last seen, who owns it."""
    rows = await calls(since)
    groups: dict[int | None, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r["api_key_id"]].append(r)
    k, u = db.api_keys, db.users
    async with db.engine().connect() as conn:
        keys = {r["id"]: dict(r) for r in (await conn.execute(
            select(k.c.id, k.c.name, k.c.user_id, u.c.username).join(u, u.c.id == k.c.user_id)
        )).mappings().all()}
    out = []
    for key_id, rs in groups.items():
        info = keys.get(key_id, {})
        out.append({"api_key_id": key_id, "key_name": info.get("name"), "username": info.get("username"),
                    "calls": len(rs), "errors": sum(1 for r in rs if not r["ok"]),
                    "sessions": len(sessions(rs)),
                    "tools": dict(Counter(r["tool"] for r in rs).most_common()),
                    "first_seen": rs[0]["created_at"].isoformat(),
                    "last_seen": rs[-1]["created_at"].isoformat()})
    out.sort(key=lambda d: d["last_seen"], reverse=True)
    return out


async def key_sessions(api_key_id: int, since: datetime | None, limit: int = 50) -> list[dict[str, Any]]:
    rows = await calls(since, api_key_id)
    out = sessions(rows)[:limit]
    for s in out:
        s["started_at"] = s["started_at"].isoformat()
        s["ended_at"] = s["ended_at"].isoformat()
        s["calls"] = [_row(c) for c in s["calls"]]
    return out


def _dry(c: dict) -> bool:
    args = c.get("args") or {}
    return bool(args.get("dry_run")) or bool((args.get("options") or {}).get("dry_run")) \
        if isinstance(args, dict) else False


async def report(since: datetime | None, known_tools: list[str] | None = None) -> dict[str, Any]:
    """The numbers that point at product fixes, over every key."""
    rows = await calls(since)
    by_tool: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_tool[r["tool"]].append(r)
    tools = [{"tool": t, "calls": len(rs), "errors": sum(1 for r in rs if not r["ok"]),
              "p50_ms": int(statistics.median(r["duration_ms"] for r in rs)) if rs else 0}
             for t, rs in sorted(by_tool.items(), key=lambda kv: -len(kv[1]))]
    errors = Counter((r["tool"], (r["error"] or "")[:160]) for r in rows if not r["ok"])
    top_errors = [{"tool": t, "error": e, "n": n} for (t, e), n in errors.most_common(15)]

    retries = 0
    dry_runs = writes_after_dry = 0
    rereads = 0
    by_key_rows: dict[int | None, list[dict]] = defaultdict(list)
    for r in rows:
        by_key_rows[r["api_key_id"]].append(r)
    for rs in by_key_rows.values():
        for prev, cur in pairwise(rs):
            # the same tool again right away: after a failure, or with other arguments (a dry run
            # followed by the real call is the intended sequence, not a retry)
            if cur["tool"] == prev["tool"] and cur["created_at"] - prev["created_at"] <= RETRY_WINDOW \
                    and (not prev["ok"] or (cur["args"] != prev["args"] and not _dry(prev))):
                retries += 1
        for s in sessions(rs):
            cs = s["calls"]
            rereads += max(0, sum(1 for c in cs if c["tool"] == "list_properties") - 1)
            for i, c in enumerate(cs):
                if _dry(c):
                    dry_runs += 1
                    if any(d["tool"] == c["tool"] and not _dry(d) for d in cs[i + 1:]):
                        writes_after_dry += 1
    runs_started = sum(1 for r in rows if r["tool"] == "start_assistant" and r["ok"])
    unused = sorted(set(known_tools or []) - set(by_tool))
    return {"since": since.isoformat() if since else None, "calls": len(rows),
            "errors": sum(1 for r in rows if not r["ok"]), "keys": len(by_key_rows),
            "by_tool": tools, "top_errors": top_errors, "retries": retries,
            "dry_runs": dry_runs, "writes_after_dry_run": writes_after_dry,
            "list_properties_rereads": rereads, "runs_started": runs_started,
            "unused_tools": unused}
