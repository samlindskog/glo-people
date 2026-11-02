"""Assistant runs: Postgres-backed state, one scheduler per process, runs that pause, resume and
survive restarts.

A run is an assistant over items (people, batches, or one item). Its state lives in Postgres
(`workflow_runs`, `workflow_run_items`, `workflow_run_events`; the tables keep their historical
names, the `workflow` column is the assistant key) so it can be paused, resumed with other
options, stopped, and picked up by another process. The scheduler of the process that owns a run
(`worker`) dispatches its queued items round-robin with the other runs it owns, up to
`assistant_concurrency` items in flight for the whole process; every item runs in its own Logfire
trace. Credits: each in-flight item reserves its worst case; when the run's cap is reached the
run pauses (`credit cap`), as it does when the Firecrawl account is empty (`account credits`), and
a resume with a higher cap continues it. Pause is graceful (in-flight items finish), stop is hard
(in-flight tool invocations are stopped and roll back). The owner heartbeats; a run whose owner
died is adopted by the API's scheduler, its running items requeued. Every tool event during an
item is persisted with the item and published live; surfaces replay then follow.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import secrets
import socket
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import logfire
from opentelemetry import context as otel_context
from sqlalchemy import Integer, delete, func, or_, select, update

from app import activity, agent_log, db, hub
from app import people as people_svc
from app.agents import common as agent_common
from app.assistants.base import REGISTRY, AssistantSpec, ItemSpec, PauseRun
from app.assistants.context import AssistantDeps
from app.auth import Principal
from app.config import get_settings
from app.integrations.firecrawl import CreditsExhausted
from app.tools import ToolEvent

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running", "pausing", "paused")     # not finished
LIVE = ("queued", "running", "pausing")                  # executed by a worker
TERMINAL = ("done", "failed", "cancelled")
ITEM_PAGE = 20
MAX_ITEM_EVENTS = 300
RETENTION_EVERY = timedelta(days=1)


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

async def create_run(spec: AssistantSpec, principal: Principal | None, items: list[ItemSpec],
                     params: dict[str, Any], label: str | None = None) -> dict:
    async with db.engine().begin() as conn:
        run_id = (await conn.execute(db.workflow_runs.insert().values(
            workflow=spec.key, status="queued", params=params,
            user_id=principal.user_id if principal else None, total=len(items),
            label=label).returning(db.workflow_runs.c.id))).scalar_one()
        if items:
            await conn.execute(db.workflow_run_items.insert(), [
                {"run_id": run_id, "workflow": spec.key, "person_id": it.get("person_id"),
                 "payload": it.get("payload") or {}, "label": it.get("label"), "status": "queued"}
                for it in items])
    await activity.record("assistant.run", principal, str(run_id),
                          {"assistant": spec.key, "items": len(items),
                           "people": sum(1 for it in items if it.get("person_id")), **params})
    return await get_run(run_id)


async def get_run(run_id: int) -> dict | None:
    r, u = db.workflow_runs, db.users
    async with db.engine().connect() as conn:
        row = (await conn.execute(
            select(r, u.c.username).select_from(r.outerjoin(u, r.c.user_id == u.c.id))
            .where(r.c.id == run_id))).first()
    return _public(row) if row else None


async def progress(run: dict) -> dict[str, Any]:
    """Where a run stands, for a reader who cannot watch it: items by status, credits and model
    tokens spent so far, the rate and an estimate of the time left."""
    i = db.workflow_run_items
    tok_in = func.coalesce(func.sum(i.c.result["tokens"]["input"].astext.cast(Integer)), 0)
    tok_out = func.coalesce(func.sum(i.c.result["tokens"]["output"].astext.cast(Integer)), 0)
    async with db.engine().connect() as conn:
        by_status = dict((await conn.execute(
            select(i.c.status, func.count()).where(i.c.run_id == run["id"]).group_by(i.c.status))).all())
        toks = (await conn.execute(select(tok_in, tok_out).where(i.c.run_id == run["id"]))).one()
    finished = run["done"] + run["failed"]
    started = run.get("started_at")
    elapsed = None
    if started:
        t0 = datetime.fromisoformat(started) if isinstance(started, str) else started
        end = run.get("finished_at")
        t1 = (datetime.fromisoformat(end) if isinstance(end, str) else end) if end else _now()
        elapsed = max(0.0, (t1 - t0).total_seconds())
    rate = finished / elapsed * 60 if elapsed and finished else None
    remaining = max(0, run["total"] - finished)
    eta = remaining / rate if rate and remaining and run["status"] in LIVE else None
    done_n = by_status.get("done", 0)
    st = _scheduler.runs.get(run["id"]) if _scheduler is not None else None
    cap = int((run.get("params") or {}).get("options", {}).get("max_credits") or 0) or None
    return {"finished": finished, "remaining": remaining, "credit_cap": cap,
            "reserved": len(st.inflight) * st.per_item_cap if st else 0,
            "in_flight": len(st.inflight) if st else 0,
            "percent": round(100 * finished / run["total"]) if run["total"] else 100,
            "by_status": by_status, "credits_used": run["credits_used"],
            "credits_per_done": round(run["credits_used"] / done_n, 2) if done_n else None,
            "tokens": {"input": int(toks[0]), "output": int(toks[1])},
            "elapsed_s": round(elapsed) if elapsed is not None else None,
            "items_per_min": round(rate, 1) if rate else None,
            "eta_min": round(eta) if eta is not None else None}


async def history_averages(workflow: str, days: int = 90) -> dict[str, Any]:
    """What one finished item of this assistant cost lately: credits, model tokens and seconds,
    averaged over the done items of the last `days` days (skipped and failed items excluded)."""
    i = db.workflow_run_items
    cutoff = _now() - timedelta(days=days)
    secs = func.extract("epoch", i.c.finished_at - i.c.started_at)
    tok = (func.coalesce(i.c.result["tokens"]["input"].astext.cast(Integer), 0)
           + func.coalesce(i.c.result["tokens"]["output"].astext.cast(Integer), 0))
    q = select(func.count(), func.avg(i.c.credits), func.avg(tok), func.avg(secs)).where(
        i.c.workflow == workflow, i.c.status == "done", i.c.finished_at >= cutoff,
        i.c.started_at.is_not(None))
    async with db.engine().connect() as conn:
        n, credits_avg, tokens_avg, secs_avg = (await conn.execute(q)).one()
    return {"items": int(n or 0), "days": days,
            "credits_per_item": round(float(credits_avg), 2) if credits_avg is not None else None,
            "tokens_per_item": int(tokens_avg) if tokens_avg is not None else None,
            "seconds_per_item": round(float(secs_avg), 1) if secs_avg is not None else None}


async def recently_done(workflow: str, person_ids: list[str], days: int) -> set[str]:
    """The people among `person_ids` with a done item of this assistant within `days`."""
    if not person_ids:
        return set()
    i = db.workflow_run_items
    cutoff = _now() - timedelta(days=days)
    out: set[str] = set()
    async with db.engine().connect() as conn:
        for start in range(0, len(person_ids), 5000):
            chunk = person_ids[start:start + 5000]
            rows = await conn.execute(select(i.c.person_id).distinct().where(
                i.c.workflow == workflow, i.c.status == "done", i.c.finished_at >= cutoff,
                i.c.person_id.in_(chunk)))
            out.update(r[0] for r in rows.all())
    return out


async def get_items(run_id: int, *, status: str | None = None, after: int = 0,
                    limit: int = 200) -> list[dict]:
    i = db.workflow_run_items
    q = select(i).where(i.c.run_id == run_id, i.c.id > after)
    if status:
        q = q.where(i.c.status == status)
    async with db.engine().connect() as conn:
        rows = (await conn.execute(q.order_by(i.c.id).limit(limit))).all()
    return [_item_public(r) for r in rows]


async def list_runs(limit: int, offset: int, status: str | None = None) -> dict:
    """`status`: None for every run, "active" for the unfinished ones, or one status."""
    r, u = db.workflow_runs, db.users
    q = select(r, u.c.username).select_from(r.outerjoin(u, r.c.user_id == u.c.id))
    if status == "active":
        q = q.where(r.c.status.in_(ACTIVE))
    elif status:
        q = q.where(r.c.status == status)
    async with db.engine().connect() as conn:
        rows = (await conn.execute(q.order_by(r.c.id.desc()).limit(limit + 1).offset(offset))).all()
    return {"items": [_public(x) for x in rows[:limit]], "has_more": len(rows) > limit}


async def events(run_id: int, after: int = 0, limit: int = 1000) -> list[dict]:
    e = db.workflow_run_events
    async with db.engine().connect() as conn:
        rows = (await conn.execute(select(e).where(e.c.run_id == run_id, e.c.id > after)
                                   .order_by(e.c.id).limit(limit))).all()
    return [_public(r) for r in rows]


async def last_done_item(workflow: str, person_id: str) -> dict | None:
    """The most recent finished item for this person and workflow (for TTL skips)."""
    i = db.workflow_run_items
    async with db.engine().connect() as conn:
        row = (await conn.execute(select(i).where(
            i.c.workflow == workflow, i.c.person_id == person_id, i.c.status == "done")
            .order_by(i.c.finished_at.desc()).limit(1))).first()
    return _public(row) if row else None


async def subscribe(run_id: int, after: int = 0) -> AsyncIterator[dict]:
    """The run's events after event id `after` (replayed, then live) interleaved with the run's
    state (`{"type": "run", ...}`), until the run is finished. A run owned by another process
    (the CLI) is followed by polling its table instead of the in-process hub."""
    topic = f"run:{run_id}"
    q = hub.attach(topic)
    try:
        last = after
        for ev in await events(run_id, after):
            yield {"type": "event", **ev}
            last = ev["id"]
        run = await get_run(run_id)
        if run is None:
            return
        yield {"type": "run", **run}
        if run["status"] in TERMINAL:
            return
        own = _scheduler is not None and run.get("worker") == _scheduler.worker
        while True:
            if own:
                msg = await q.get()
                if msg.get("type") == "event":
                    if msg["id"] <= last:
                        continue
                    last = msg["id"]
                yield msg
                if msg.get("type") == "run" and msg.get("status") in TERMINAL:
                    return
                continue
            await asyncio.sleep(2)
            for ev in await events(run_id, last):
                yield {"type": "event", **ev}
                last = ev["id"]
            run = await get_run(run_id)
            if run is None:
                return
            yield {"type": "run", **run}
            if run["status"] in TERMINAL:
                return
            own = _scheduler is not None and run.get("worker") == _scheduler.worker
    finally:
        hub.detach(topic, q)


# --- the scheduler --------------------------------------------------------------------------------

# (run id, item id) of the assistant item executing in the current task, for observers that must
# tell an assistant's write from a person's (the auto-review leaves assistant items alone).
CURRENT_ITEM: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar(
    "current_item", default=None)


@dataclass
class _RunState:
    run: dict
    spec: AssistantSpec
    options: Any
    principal: Principal | None
    run_cap: int
    per_item_cap: int
    used: int
    inflight: dict[int, asyncio.Task] = field(default_factory=dict)
    deps: dict[int, AssistantDeps] = field(default_factory=dict)
    queue: deque[dict] = field(default_factory=deque)
    cursor: int = 0
    exhausted: bool = False
    pausing: bool = False
    item_events: dict[int, int] = field(default_factory=dict)
    cancelled: list[int] = field(default_factory=list)     # items whose task we cancelled
    # The starter's context (its agent overrides, settings): items run inside a copy of it, as
    # they did when a request spawned them; adopted runs use the scheduler's own.
    context: contextvars.Context | None = None

    @property
    def run_id(self) -> int:
        return self.run["id"]

    @property
    def started_any(self) -> bool:
        return bool(self.run["done"] + self.run["failed"] + len(self.inflight))


class _Scheduler:
    def __init__(self, worker: str) -> None:
        self.worker = worker
        self.runs: dict[int, _RunState] = {}
        self.wake = asyncio.Event()
        self.events_q: asyncio.Queue[tuple[int, int | None, str, str, dict]] = asyncio.Queue()
        self._rotate = 0
        self._tasks: list[asyncio.Task] = []
        self._stopping = False
        self._last_retention: datetime | None = None

    # --- lifecycle ------------------------------------------------------------------------------------

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._loop(), name=f"runner:{self.worker}"),
                       asyncio.create_task(self._tick(), name=f"runner-tick:{self.worker}"),
                       asyncio.create_task(self._writer(), name=f"runner-events:{self.worker}")]

    async def stop(self) -> None:
        """Hand the owned runs back: in-flight items are cancelled (their tool invocations roll
        back) and requeued, the runs lose their worker; the next start adopts them."""
        self._stopping = True
        for st in list(self.runs.values()):
            await self._cancel_inflight(st)
            if st.cancelled:
                await self._requeue(st.run_id, st.cancelled)
                self.emit(st.run_id, "note",
                          f"{self.worker} stopped; {len(st.cancelled)} item(s) requeued")
        async with db.engine().begin() as conn:
            await conn.execute(update(db.workflow_runs).where(
                db.workflow_runs.c.worker == self.worker,
                db.workflow_runs.c.status.in_(LIVE)).values(worker=None))
        self.runs.clear()
        await self.events_q.join()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def abort(self) -> None:
        """Drop every run this process executes without touching the table (tests)."""
        for st in list(self.runs.values()):
            await self._cancel_inflight(st)
        self.runs.clear()
        await self.events_q.join()

    def emit(self, run_id: int, kind: str, message: str, item_id: int | None = None,
             **data: Any) -> None:
        self.events_q.put_nowait((run_id, item_id, kind, message, data))

    async def _writer(self) -> None:
        while True:
            run_id, item_id, kind, message, data = await self.events_q.get()
            try:
                e = db.workflow_run_events
                async with db.engine().begin() as conn:
                    row = (await conn.execute(e.insert().values(
                        run_id=run_id, item_id=item_id, kind=kind[:16], message=message,
                        data=data).returning(e))).first()
                hub.publish(f"run:{run_id}", {"type": "event", **_public(row)})
            except Exception:
                log.exception("could not persist a run event")
            finally:
                self.events_q.task_done()

    async def _publish_run(self, run_id: int) -> dict | None:
        run = await get_run(run_id)
        if run is not None:
            hub.publish(f"run:{run_id}", {"type": "run", **run})
        return run

    # --- loading and dispatching ----------------------------------------------------------------------

    async def load(self, run_id: int, principal: Principal | None = None,
                   context: contextvars.Context | None = None) -> _RunState | None:
        run = await get_run(run_id)
        if run is None:
            return None
        spec = REGISTRY.get(run["workflow"])
        if spec is None:
            await self._set_status(run_id, "failed", error="unknown assistant")
            return None
        options = spec.options_model.model_validate(run["params"].get("options") or {})
        st = _RunState(run=run, spec=spec, options=options, principal=principal,
                       run_cap=int(getattr(options, "max_credits", 0) or 0),
                       per_item_cap=spec.item_cap(options), used=run["credits_used"],
                       context=context)
        self.runs[run_id] = st
        self.wake.set()
        return st

    async def _loop(self) -> None:
        otel_context.attach(otel_context.Context())   # every item below starts its own trace
        while True:
            await self.wake.wait()
            self.wake.clear()
            try:
                await self._dispatch()
            except Exception:
                log.exception("scheduler dispatch failed")

    def _inflight_total(self) -> int:
        return sum(len(st.inflight) for st in self.runs.values())

    async def _dispatch(self) -> None:
        limit = get_settings().assistant_concurrency
        while not self._stopping:
            progressed = False
            states = [st for st in self.runs.values() if st.run["status"] == "running"
                      and not st.pausing]
            if not states:
                for st in list(self.runs.values()):
                    await self._settle(st)
                return
            self._rotate += 1
            for st in states[self._rotate % len(states):] + states[:self._rotate % len(states)]:
                if self._inflight_total() >= limit:
                    return
                if not st.queue and not st.exhausted:
                    await self._fetch(st)
                if not st.queue:
                    await self._settle(st)
                    continue
                if st.run_cap and st.started_any:
                    if st.used >= st.run_cap:
                        await self._pause(st, "credit cap")
                        continue
                    if st.inflight and st.used + len(st.inflight) * st.per_item_cap >= st.run_cap:
                        continue                       # wait for the real spend of in-flight items
                item = st.queue.popleft()
                if not await self._claim(item["id"]):
                    continue
                task = asyncio.create_task(self._run_item(st, item), context=st.context,
                                           name=f"item:{st.run_id}:{item['id']}")
                st.inflight[item["id"]] = task
                task.add_done_callback(lambda _: self.wake.set())
                progressed = True
            if not progressed:
                return

    async def _fetch(self, st: _RunState) -> None:
        i = db.workflow_run_items
        async with db.engine().connect() as conn:
            rows = (await conn.execute(select(i).where(
                i.c.run_id == st.run_id, i.c.status == "queued", i.c.id > st.cursor)
                .order_by(i.c.id).limit(ITEM_PAGE))).all()
        for row in rows:
            st.queue.append(_public(row))
            st.cursor = row.id
        if len(rows) < ITEM_PAGE:
            st.exhausted = True

    async def _claim(self, item_id: int) -> bool:
        i = db.workflow_run_items
        async with db.engine().begin() as conn:
            res = await conn.execute(update(i).where(i.c.id == item_id, i.c.status == "queued")
                                     .values(status="running", started_at=_now(),
                                             attempts=i.c.attempts + 1))
        return res.rowcount == 1

    async def _settle(self, st: _RunState) -> None:
        """A run with nothing in flight and nothing queued is done; a pausing one is paused."""
        if st.inflight:
            return
        if st.pausing:
            await self._set_status(st.run_id, "paused")
            self.runs.pop(st.run_id, None)
            return
        if st.run["status"] != "running":
            return
        if not st.exhausted:
            await self._fetch(st)
            if st.queue:
                self.wake.set()
                return
        # requeued items (a pause while something was in flight) may sit below the cursor
        st.cursor, st.exhausted = 0, False
        await self._fetch(st)
        if st.queue:
            self.wake.set()
            return
        await self._finish(st.run_id, "done")
        self.runs.pop(st.run_id, None)

    # --- one item -------------------------------------------------------------------------------------

    async def _run_item(self, st: _RunState, item: dict) -> None:
        trace_id = None
        deps: AssistantDeps | None = None
        run_id, item_id = st.run_id, item["id"]
        dropped = 0

        def sink(ev: ToolEvent) -> None:
            nonlocal dropped
            n = st.item_events.get(item_id, 0)
            if n >= MAX_ITEM_EVENTS:
                dropped += 1
                return
            st.item_events[item_id] = n + 1
            self.emit(run_id, ev.kind, ev.message, item_id, **ev.data)

        def credits() -> int:
            return deps.budget.credits if deps is not None else 0

        def partial() -> dict:
            out: dict[str, Any] = {"credits": credits()}
            if deps is not None:
                out.update(changes=[c.model_dump() for c in deps.changes], skipped=deps.skipped,
                           calls=getattr(deps.budget, "calls", None))
            return out

        otel_context.attach(otel_context.Context())      # its own trace, not the starter's
        CURRENT_ITEM.set((run_id, item_id))              # writes below happen inside this item
        try:
            with logfire.span("assistant {assistant} item {item_id}", assistant=st.spec.key,
                              run_id=run_id, item_id=item_id, person_id=item["person_id"]) as span:
                ctx = span.get_span_context()
                if ctx and ctx.trace_id:
                    trace_id = f"{ctx.trace_id:032x}"
                person = None
                if item["person_id"]:
                    person = await people_svc.get(item["person_id"])
                    if person is None:
                        await self._mark(st, item, "failed", error="person not found",
                                         trace_id=trace_id)
                        return
                deps = await st.spec.make_deps(run_id, {**item, "person": person}, st.options,
                                               st.principal)
                deps.event_sink = sink
                st.deps[item_id] = deps
                self.emit(run_id, "step", f"{item.get('label') or item_id}: started", item_id)
                usage: dict[str, int] = {}
                agent_common.USAGE.set(usage)
                result = await st.spec.run_item(deps)
                result = {**result, "credits": credits(),
                          "calls": getattr(deps.budget, "calls", None), "tokens": usage or None}
                span.set_attribute("credits", credits())
                if dropped:
                    self.emit(run_id, "note", f"{dropped} more events not kept", item_id)
                await self._mark(st, item, result.pop("status", "done"), result=result,
                                 credits=credits(), trace_id=trace_id)
        except asyncio.CancelledError:
            st.cancelled.append(item_id)
            raise
        except (PauseRun, CreditsExhausted) as e:
            reason = e.reason if isinstance(e, PauseRun) else "account credits"
            await self._requeue(run_id, [item_id], credits=credits())
            self.emit(run_id, "note", f"{item.get('label') or item_id}: {e}; back in the queue",
                      item_id)
            await self._pause(st, reason, cascade=reason == "account credits")
        except Exception as e:
            log.exception("assistant %s item %s failed", st.spec.key, item_id)
            await self._mark(st, item, "failed", result=partial(),
                             error=f"{type(e).__name__}: {e}"[:500], credits=credits(),
                             trace_id=trace_id)
        finally:
            st.inflight.pop(item_id, None)
            st.deps.pop(item_id, None)
            st.item_events.pop(item_id, None)
            if deps is not None:
                await deps.aclose()
            self.wake.set()

    async def _mark(self, st: _RunState, item: dict, status: str, result: dict | None = None,
                    error: str | None = None, credits: int = 0, trace_id: str | None = None) -> None:
        i, r = db.workflow_run_items, db.workflow_runs
        values: dict[str, Any] = {"status": status, "result": result or {}, "error": error,
                                  "credits": credits, "trace_id": trace_id, "finished_at": _now()}
        if not item.get("person_id") and isinstance((result or {}).get("person_id"), str):
            values["person_id"] = result["person_id"]      # an item that found or created its person
        async with db.engine().begin() as conn:
            await conn.execute(update(i).where(i.c.id == item["id"]).values(**values))
            counter = r.c.failed if status == "failed" else r.c.done
            await conn.execute(update(r).where(r.c.id == st.run_id).values(
                **{counter.name: counter + 1}, credits_used=r.c.credits_used + credits))
        st.used += credits
        st.run[counter.name] += 1
        label = item.get("label") or item["id"]
        self.emit(st.run_id, "item", f"{label}: {status}" + (f" ({credits} credits)" if credits else "")
                  + (f" — {error}" if error else ""), item["id"], status=status, credits=credits)
        await self._publish_run(st.run_id)

    async def _requeue(self, run_id: int, item_ids: list[int], credits: int = 0) -> None:
        """Back to the queue (an interrupted or paused item); credits already spent are kept on
        the run. An item interrupted too often fails instead."""
        if not item_ids:
            return
        i, r = db.workflow_run_items, db.workflow_runs
        max_attempts = get_settings().item_max_attempts
        async with db.engine().begin() as conn:
            poisoned = (await conn.execute(update(i).where(
                i.c.id.in_(item_ids), i.c.attempts >= max_attempts).values(
                status="failed", error="interrupted repeatedly", finished_at=_now())
                .returning(i.c.id))).scalars().all()
            await conn.execute(update(i).where(i.c.id.in_(item_ids), i.c.status == "running")
                               .values(status="queued", started_at=None))
            if poisoned or credits:
                await conn.execute(update(r).where(r.c.id == run_id).values(
                    failed=r.c.failed + len(poisoned), credits_used=r.c.credits_used + credits))
        st = self.runs.get(run_id)
        if st is not None:
            st.used += credits
            st.run["failed"] += len(poisoned)
            st.cursor, st.exhausted = 0, False

    # --- run state ------------------------------------------------------------------------------------

    async def _set_status(self, run_id: int, status: str, **values: Any) -> None:
        if status in TERMINAL:
            values.setdefault("finished_at", _now())
        if status == "running":
            values.setdefault("pause_reason", None)
        async with db.engine().begin() as conn:
            await conn.execute(update(db.workflow_runs).where(db.workflow_runs.c.id == run_id)
                               .values(status=status, **values))
        st = self.runs.get(run_id)
        if st is not None:
            st.run["status"] = status
            st.run.update({k: (v.isoformat() if hasattr(v, "isoformat") else v)
                           for k, v in values.items()})
        self.emit(run_id, "run", status)
        if status in TERMINAL or status == "paused":
            await self.events_q.join()          # a live follower sees every event before the end
        await self._publish_run(run_id)

    async def _pause(self, st: _RunState, reason: str, cascade: bool = False) -> None:
        if st.run["status"] not in ("running", "pausing"):
            return
        st.pausing = True
        if st.run["status"] == "running":
            await self._set_status(st.run_id, "pausing", pause_reason=reason)
            await activity.record("assistant.pause", st.principal, str(st.run_id),
                                  {"assistant": st.spec.key, "reason": reason})
        if cascade:
            for other in list(self.runs.values()):
                if other is not st and other.per_item_cap and other.run["status"] == "running":
                    await self._pause(other, reason)
        if not st.inflight:
            await self._settle(st)

    async def _finish(self, run_id: int, status: str, error: str | None = None) -> None:
        i = db.workflow_run_items
        async with db.engine().connect() as conn:
            counts = (await conn.execute(select(i.c.status, func.count())
                                         .where(i.c.run_id == run_id).group_by(i.c.status))).all()
        by = {s: n for s, n in counts}
        await self._set_status(run_id, status, error=error, summary={"by_status": by})

    async def _cancel_inflight(self, st: _RunState) -> None:
        st.cancelled = []
        tasks = list(st.inflight.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- control from outside -------------------------------------------------------------------------

    async def pause(self, run_id: int) -> bool:
        st = self.runs.get(run_id)
        if st is None:
            return False
        await self._pause(st, "requested")
        return True

    async def stop_run(self, run_id: int) -> None:
        st = self.runs.pop(run_id, None)
        if st is None:
            return
        await self._cancel_inflight(st)
        cancelled = st.cancelled
        spent = sum(st.deps[i].budget.credits for i in cancelled if i in st.deps)
        i, r = db.workflow_run_items, db.workflow_runs
        async with db.engine().begin() as conn:
            if cancelled:
                await conn.execute(update(i).where(i.c.id.in_(cancelled)).values(
                    status="skipped", error="stopped", finished_at=_now()))
            await conn.execute(update(r).where(r.c.id == run_id).values(
                done=r.c.done + len(cancelled), credits_used=r.c.credits_used + spent))

    # --- the tick: heartbeat, control sync, adoption, retention ---------------------------------------

    async def _tick(self) -> None:
        every = get_settings().run_heartbeat_s
        while True:
            await asyncio.sleep(every)
            try:
                await self._heartbeat()
                await self._sync_control()
                if self.worker.startswith("api:"):
                    await self.adopt(all_api_runs=False)
                await self._retention()
            except Exception:
                log.exception("scheduler tick failed")

    async def _heartbeat(self) -> None:
        r = db.workflow_runs
        async with db.engine().begin() as conn:
            await conn.execute(update(r).where(r.c.worker == self.worker, r.c.status.in_(LIVE))
                               .values(heartbeat_at=_now()))

    async def _sync_control(self) -> None:
        """Another process (or a REST call for a run we own) changed the state in the table."""
        if not self.runs:
            return
        r = db.workflow_runs
        async with db.engine().connect() as conn:
            rows = (await conn.execute(select(r.c.id, r.c.status, r.c.params).where(
                r.c.id.in_(list(self.runs))))).all()
        for run_id, status, params in rows:
            st = self.runs.get(run_id)
            if st is None:
                continue
            if status == "cancelled":
                await self.stop_run(run_id)
            elif status == "pausing" and not st.pausing:
                st.run["status"] = "pausing"
                st.pausing = True
                await self._settle(st)
            elif status in ("paused", "done", "failed"):
                self.runs.pop(run_id, None)
            elif status == "running":
                st.run["params"] = params

    async def adopt(self, all_api_runs: bool) -> list[int]:
        """Take over runs whose worker is gone: none, stale, or (at startup) any API worker."""
        r, i = db.workflow_runs, db.workflow_run_items
        stale = _now() - timedelta(seconds=get_settings().run_stale_s)
        not_mine = or_(r.c.worker.is_(None), r.c.worker != self.worker)
        gone = or_(r.c.worker.is_(None), r.c.heartbeat_at.is_(None), r.c.heartbeat_at < stale)
        if all_api_runs:
            gone = or_(gone, r.c.worker.like("api:%"))
        async with db.engine().begin() as conn:
            rows = (await conn.execute(update(r).where(r.c.status.in_(LIVE), not_mine, gone)
                                       .values(worker=self.worker, heartbeat_at=_now())
                                       .returning(r.c.id, r.c.status))).all()
        adopted = {row[0]: row[1] for row in rows}
        for run_id, status in adopted.items():
            running = [row.id for row in await _rows(select(i.c.id).where(
                i.c.run_id == run_id, i.c.status == "running"))]
            await self._requeue(run_id, running)
            if status == "pausing":
                await self._set_status(run_id, "paused")
                continue
            if status == "queued":
                await self._set_status(run_id, "running", started_at=_now())
            self.emit(run_id, "run", f"adopted by {self.worker}" +
                      (f"; {len(running)} item(s) requeued" if running else ""))
            await self.load(run_id)
        return list(adopted)

    async def _retention(self) -> None:
        now = _now()
        if self._last_retention and now - self._last_retention < RETENTION_EVERY:
            return
        self._last_retention = now
        cutoff = now - timedelta(days=get_settings().run_events_retention_days)
        e, r = db.workflow_run_events, db.workflow_runs
        async with db.engine().begin() as conn:
            await conn.execute(delete(e).where(e.c.run_id.in_(
                select(r.c.id).where(r.c.finished_at < cutoff))))
        await agent_log.prune(get_settings().agent_calls_retention_days)


async def _rows(q: Any) -> list[Any]:
    async with db.engine().connect() as conn:
        return (await conn.execute(q)).all()


_scheduler: _Scheduler | None = None


def worker_id(kind: str) -> str:
    """A worker name unique to this process start: `api:<host>:<pid>:<nonce>`. The pid alone
    repeats across container restarts, and a worker must never mistake a dead predecessor's
    runs for its own."""
    return f"{kind}:{socket.gethostname()[:16]}:{os.getpid()}:{secrets.token_hex(3)}"


def start_scheduler(worker: str) -> _Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = _Scheduler(worker)
        _scheduler.start()
    return _scheduler


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        await _scheduler.stop()
        _scheduler = None


def scheduler() -> _Scheduler:
    assert _scheduler is not None, "the scheduler is not running"
    return _scheduler


def reserved_credits() -> int:
    """Worst-case credits the in-flight items of this process may still spend."""
    if _scheduler is None:
        return 0
    return sum(len(st.inflight) * st.per_item_cap for st in _scheduler.runs.values())


# --- control --------------------------------------------------------------------------------------------

async def start(run_id: int, principal: Principal | None = None) -> None:
    """Begin executing a freshly created run in this process."""
    sched = scheduler()
    async with db.engine().begin() as conn:
        await conn.execute(update(db.workflow_runs).where(db.workflow_runs.c.id == run_id)
                           .values(status="running", started_at=_now(), worker=sched.worker,
                                   heartbeat_at=_now()))
    sched.emit(run_id, "run", "started")
    await sched.load(run_id, principal, contextvars.copy_context())
    await sched._publish_run(run_id)


async def pause(run_id: int, principal: Principal | None = None) -> bool:
    """Stop taking new items; in-flight ones finish. False when the run is not running."""
    run = await get_run(run_id)
    if run is None or run["status"] != "running":
        return False
    if _scheduler is not None and run_id in _scheduler.runs:
        return await _scheduler.pause(run_id)
    async with db.engine().begin() as conn:          # another worker owns it: it will notice
        await conn.execute(update(db.workflow_runs).where(
            db.workflow_runs.c.id == run_id, db.workflow_runs.c.status == "running")
            .values(status="pausing", pause_reason="requested"))
    await activity.record("assistant.pause", principal, str(run_id), {"reason": "requested"})
    return True


async def resume(run_id: int, options: dict[str, Any] | None = None,
                 principal: Principal | None = None) -> bool:
    """Continue a paused run in this process, with options patched (e.g. a higher cap)."""
    run = await get_run(run_id)
    if run is None or run["status"] != "paused":
        return False
    sched = scheduler()
    spec = REGISTRY.get(run["workflow"])
    params = dict(run["params"])
    if options and spec is not None:
        merged = spec.options_model.model_validate({**(params.get("options") or {}), **options})
        params["options"] = merged.model_dump()
    async with db.engine().begin() as conn:
        res = await conn.execute(update(db.workflow_runs).where(
            db.workflow_runs.c.id == run_id, db.workflow_runs.c.status == "paused").values(
            status="running", pause_reason=None, params=params, worker=sched.worker,
            heartbeat_at=_now()))
    if res.rowcount == 0:
        return False
    sched.emit(run_id, "run", "resumed" + (f" with {options}" if options else ""))
    await activity.record("assistant.resume", principal, str(run_id), {"options": options or {}})
    await sched.load(run_id, principal, contextvars.copy_context())
    await sched._publish_run(run_id)
    return True


async def stop(run_id: int, principal: Principal | None = None) -> bool:
    """Hard stop: in-flight tool invocations are stopped (nothing written), queued items are
    skipped, the run is cancelled. False when it is already finished."""
    run = await get_run(run_id)
    if run is None or run["status"] not in ACTIVE:
        return False
    i, r = db.workflow_run_items, db.workflow_runs
    async with db.engine().begin() as conn:
        n = (await conn.execute(update(i).where(i.c.run_id == run_id, i.c.status == "queued")
                                .values(status="skipped", error="stopped", finished_at=_now()))
             ).rowcount
        await conn.execute(update(r).where(r.c.id == run_id).values(
            status="cancelled", finished_at=_now(), done=r.c.done + n))
    if _scheduler is not None:
        await _scheduler.stop_run(run_id)
        await _scheduler._finish(run_id, "cancelled")
    await activity.record("assistant.stop", principal, str(run_id), {"skipped": n})
    return True


cancel = stop


async def adopt(all_api_runs: bool) -> list[int]:
    return await scheduler().adopt(all_api_runs)


async def run_one(spec: AssistantSpec, principal: Principal | None, items: list[ItemSpec],
                  params: dict[str, Any], *, label: str | None = None,
                  timeout: float = 120) -> dict[str, Any]:
    """A run of one item, started and waited for: how the synchronous surfaces (REST, MCP) run an
    assistant on one record. Returns the item's result with the run and item ids and the item's
    status (and error); still `running` when `timeout` passed first."""
    run = await create_run(spec, principal, items, params, label=label or items[0].get("label"))
    await start(run["id"], principal)
    try:
        await wait(run["id"], timeout)
    except TimeoutError:
        pass
    item = (await get_items(run["id"]))[0]
    return {**(item.get("result") or {}), "run_id": run["id"], "item_id": item["id"],
            "status": item["status"], "error": item.get("error"), "credits": item.get("credits")}


async def wait(run_id: int, timeout: float = 120) -> dict:
    """Until the run stops executing: finished or paused (tests, the CLI)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        run = await get_run(run_id)
        if run is None or run["status"] not in LIVE:
            if _scheduler is not None:
                await _scheduler.events_q.join()
            return run or {}
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"run {run_id} still {run['status']} after {timeout}s")
        await asyncio.sleep(0.05)


async def wait_idle(timeout: float = 120) -> None:
    """Until this process executes nothing (tests)."""
    if _scheduler is None:
        return
    deadline = asyncio.get_running_loop().time() + timeout
    while _scheduler.runs or _scheduler._inflight_total():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("the scheduler is still busy")
        await asyncio.sleep(0.05)
    await _scheduler.events_q.join()


drain = wait_idle


async def abort_all() -> None:
    """Cancel everything this process executes and forget it (tests, before truncating)."""
    if _scheduler is not None:
        await _scheduler.abort()


async def run_to_completion(run_id: int, worker: str,
                            on_event: Any = None) -> dict:
    """Execute one run in this process (the CLI): start a scheduler for it unless one runs
    already, follow its events, return the run when it has finished or paused."""
    own = _scheduler is None
    start_scheduler(worker)
    await start(run_id)
    try:
        if on_event is not None:
            async for msg in subscribe(run_id):
                if msg.get("type") == "event":
                    on_event(msg)
                elif msg.get("type") == "run" and msg.get("status") not in LIVE:
                    break
        return await wait(run_id, timeout=86_400)
    finally:
        if own:
            await stop_scheduler()
