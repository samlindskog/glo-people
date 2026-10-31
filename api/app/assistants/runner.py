"""Assistant runs: Postgres-backed state, in-process execution (the API runs one worker).

A run executes one per-person assistant over many people, one item each. Items run concurrently
up to `workflow_concurrency`, each in its own Logfire span; a run-level credit cap stops the run
early and marks the rest `skipped`. The runner is generic: an assistant builds its own
dependencies (`spec.make_deps`), reports spend through `deps.budget.credits`, returns text and
changes per person, and raises `StopRun` when the whole run must stop. The tables keep their
historical names (`workflow_runs`, `workflow_run_items`; the `workflow` column is the key).
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import logfire
from opentelemetry import context as otel_context
from sqlalchemy import func, select, update

from app import activity, db
from app import people as people_svc
from app.assistants.base import REGISTRY, AssistantSpec, StopRun
from app.assistants.context import AssistantDeps
from app.auth import Principal
from app.config import get_settings

log = logging.getLogger(__name__)

_tasks: dict[int, asyncio.Task] = {}
_cancelled: set[int] = set()


def _now() -> datetime:
    return datetime.now(UTC)


def _public(row: Any) -> dict:
    d = dict(row._mapping)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


def _item_public(row: Any) -> dict:
    d = _public(row)
    base = get_settings().logfire_project_url.rstrip("/")
    d["logfire_url"] = f"{base}?q=trace_id%3D%27{d['trace_id']}%27" if base and d.get(
        "trace_id") else None
    return d


# --- creating and reading runs --------------------------------------------------------------------

async def create_run(spec: AssistantSpec, principal: Principal, person_ids: list[str],
                     params: dict[str, Any]) -> dict:
    async with db.engine().begin() as conn:
        run_id = (await conn.execute(db.workflow_runs.insert().values(
            workflow=spec.key, status="queued", params=params, user_id=principal.user_id,
            total=len(person_ids)).returning(db.workflow_runs.c.id))).scalar_one()
        if person_ids:
            await conn.execute(db.workflow_run_items.insert(), [
                {"run_id": run_id, "workflow": spec.key, "person_id": pid, "status": "queued"}
                for pid in person_ids])
    await activity.record("assistant.run", principal, str(run_id),
                          {"assistant": spec.key, "people": len(person_ids), **params})
    return await get_run(run_id)


async def get_run(run_id: int) -> dict | None:
    async with db.engine().connect() as conn:
        row = (await conn.execute(select(db.workflow_runs).where(
            db.workflow_runs.c.id == run_id))).first()
    return _public(row) if row else None


async def get_items(run_id: int) -> list[dict]:
    async with db.engine().connect() as conn:
        rows = (await conn.execute(select(db.workflow_run_items).where(
            db.workflow_run_items.c.run_id == run_id).order_by(db.workflow_run_items.c.id))).all()
    return [_item_public(r) for r in rows]


async def list_runs(limit: int, offset: int) -> dict:
    r, u = db.workflow_runs, db.users
    async with db.engine().connect() as conn:
        rows = (await conn.execute(
            select(r, u.c.username).select_from(r.outerjoin(u, r.c.user_id == u.c.id))
            .order_by(r.c.id.desc()).limit(limit + 1).offset(offset))).all()
    items = [_public(x) for x in rows[:limit]]
    return {"items": items, "has_more": len(rows) > limit}


async def last_done_item(workflow: str, person_id: str) -> dict | None:
    """The most recent finished item for this person and workflow (for TTL skips)."""
    i = db.workflow_run_items
    async with db.engine().connect() as conn:
        row = (await conn.execute(select(i).where(
            i.c.workflow == workflow, i.c.person_id == person_id, i.c.status == "done")
            .order_by(i.c.finished_at.desc()).limit(1))).first()
    return _public(row) if row else None


# --- execution ------------------------------------------------------------------------------------

def start(run_id: int, principal: Principal) -> None:
    task = asyncio.create_task(_run(run_id, principal))
    _tasks[run_id] = task
    task.add_done_callback(lambda _: _tasks.pop(run_id, None))


async def cancel(run_id: int) -> bool:
    run = await get_run(run_id)
    if not run or run["status"] not in ("queued", "running"):
        return False
    _cancelled.add(run_id)
    async with db.engine().begin() as conn:
        await conn.execute(update(db.workflow_run_items).where(
            db.workflow_run_items.c.run_id == run_id,
            db.workflow_run_items.c.status == "queued").values(status="skipped",
                                                              error="cancelled"))
    if run_id not in _tasks:   # nothing is executing it (e.g. after a restart)
        await _finish(run_id, "cancelled")
    return True


async def reconcile() -> None:
    """At startup: runs left running by a previous process cannot resume."""
    async with db.engine().begin() as conn:
        await conn.execute(update(db.workflow_run_items).where(
            db.workflow_run_items.c.status.in_(("queued", "running")),
            db.workflow_run_items.c.run_id.in_(select(db.workflow_runs.c.id).where(
                db.workflow_runs.c.status.in_(("queued", "running"))))
        ).values(status="failed", error="interrupted by restart", finished_at=_now()))
        await conn.execute(update(db.workflow_runs).where(
            db.workflow_runs.c.status.in_(("queued", "running"))
        ).values(status="failed", error="interrupted by restart", finished_at=_now()))


async def drain() -> None:
    while _tasks:
        await asyncio.gather(*list(_tasks.values()), return_exceptions=True)


async def _finish(run_id: int, status: str, error: str | None = None,
                  summary: dict | None = None) -> None:
    i = db.workflow_run_items
    async with db.engine().begin() as conn:
        counts = (await conn.execute(
            select(i.c.status, func.count(), func.coalesce(func.sum(i.c.credits), 0))
            .where(i.c.run_id == run_id).group_by(i.c.status))).all()
        by = {s: (n, c) for s, n, c in counts}
        await conn.execute(update(db.workflow_runs).where(db.workflow_runs.c.id == run_id).values(
            status=status, finished_at=_now(), error=error,
            done=by.get("done", (0, 0))[0] + by.get("skipped", (0, 0))[0],
            failed=by.get("failed", (0, 0))[0],
            credits_used=sum(c for _, c in by.values()),
            summary={**(summary or {}), "by_status": {s: n for s, (n, _) in by.items()}}))


async def _run(run_id: int, principal: Principal) -> None:
    # Detach from the HTTP request's trace (this task owns a copy of the request context), so
    # every item span below starts its own trace: one Logfire link per person.
    otel_context.attach(otel_context.Context())
    run = await get_run(run_id)
    spec = REGISTRY.get(run["workflow"]) if run else None
    if spec is None:
        await _finish(run_id, "failed", "unknown assistant")
        return
    options = spec.options_model.model_validate(run["params"].get("options") or {})
    run_cap = int(getattr(options, "max_credits", 0) or 0)
    async with db.engine().begin() as conn:
        await conn.execute(update(db.workflow_runs).where(db.workflow_runs.c.id == run_id)
                           .values(status="running", started_at=_now()))
    items = await get_items(run_id)
    sem = asyncio.Semaphore(get_settings().workflow_concurrency)
    per_item_cap = spec.item_cap(options)
    credits_used = 0
    in_flight = 0
    started = 0
    stopped: str | None = None
    cond = asyncio.Condition()

    async def one(item: dict) -> None:
        nonlocal credits_used, in_flight, started, stopped
        async with sem:
            async with cond:
                # Each in-flight item reserves its worst case, so the run can overshoot the cap
                # by at most one per-item cap. An item that does not fit yet WAITS for running
                # ones to report their real spend; it is only skipped once the cap is really
                # used up (or the run stopped). The first item always runs.
                while True:
                    if run_id in _cancelled or stopped:
                        await _mark(item["id"], "skipped", error=stopped or "cancelled")
                        return
                    if run_cap and started and credits_used >= run_cap:
                        stopped = "credit cap"
                        await _mark(item["id"], "skipped", error="run credit cap reached")
                        return
                    if not run_cap or not started or \
                            credits_used + in_flight * per_item_cap < run_cap:
                        break
                    await cond.wait()
                started += 1
                in_flight += 1
            try:
                credits = await _run_item(spec, options, run_id, item, principal)
            finally:
                async with cond:
                    in_flight -= 1
                    cond.notify_all()
            async with cond:
                credits_used += max(credits, 0)
                if credits == -1:                 # the workflow asked to stop the run
                    stopped = "stopped by the assistant"
                cond.notify_all()

    await asyncio.gather(*(one(it) for it in items))
    if run_id in _cancelled:
        _cancelled.discard(run_id)
        await _finish(run_id, "cancelled")
        return
    await _finish(run_id, "done", summary={"stopped_reason": stopped} if stopped else {})


async def _mark(item_id: int, status: str, result: dict | None = None, error: str | None = None,
                credits: int = 0, trace_id: str | None = None) -> None:
    async with db.engine().begin() as conn:
        await conn.execute(update(db.workflow_run_items).where(
            db.workflow_run_items.c.id == item_id).values(
            status=status, result=result or {}, error=error, credits=credits,
            trace_id=trace_id, finished_at=_now()))


async def _run_item(spec: AssistantSpec, options: Any, run_id: int, item: dict,
                    principal: Principal) -> int:
    """Run one person; returns the credits spent, or -1 when the workflow raised StopRun."""
    async with db.engine().begin() as conn:
        res = await conn.execute(update(db.workflow_run_items).where(
            db.workflow_run_items.c.id == item["id"],
            db.workflow_run_items.c.status == "queued").values(status="running",
                                                              started_at=_now()))
    if res.rowcount == 0:        # cancel() skipped it between our check and this transition
        return 0
    trace_id = None
    deps: AssistantDeps | None = None

    def credits() -> int:
        return deps.budget.credits if deps is not None else 0

    def partial() -> dict:
        """What a failed item still has to show: writes and skips so far, and the spend."""
        out: dict[str, Any] = {"credits": credits()}
        if deps is not None:
            out.update(changes=[c.model_dump() for c in deps.changes], skipped=deps.skipped,
                       calls=getattr(deps.budget, "calls", None))
        return out

    try:
        with logfire.span("assistant {assistant} item {item_id}", assistant=spec.key,
                          run_id=run_id, item_id=item["id"],
                          person_id=item["person_id"]) as span:
            ctx = span.get_span_context()
            if ctx and ctx.trace_id:
                trace_id = f"{ctx.trace_id:032x}"
            person = await people_svc.get(item["person_id"])
            if person is None:
                await _mark(item["id"], "failed", error="person not found", trace_id=trace_id)
                return 0
            deps = await spec.make_deps(run_id, item["id"], person, options, principal)
            result = await spec.run_item(deps)
            result = {**result, "credits": credits(), "calls": getattr(deps.budget, "calls", None)}
            span.set_attribute("credits", credits())
            await _mark(item["id"], result.pop("status", "done"), result=result,
                        credits=credits(), trace_id=trace_id)
            return credits()
    except StopRun as e:
        await _mark(item["id"], "failed", result=partial(), error=str(e), credits=credits(),
                    trace_id=trace_id)
        return -1
    except Exception as e:
        log.exception("assistant %s item %s failed", spec.key, item["id"])
        await _mark(item["id"], "failed", result=partial(), error=f"{type(e).__name__}: {e}"[:500],
                    credits=credits(), trace_id=trace_id)
        return credits()
    finally:
        if deps is not None:
            await deps.aclose()
