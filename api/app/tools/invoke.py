"""Running a tool: the invocation.

`invoke(tool, ctx, **kwargs)` starts a tool on one record and returns an `Invocation`, the handle
a surface streams, stops and waits on. A *driver* task runs the tool in its own *tool* task and
commits the context's transaction once the tool has returned; `stop()` cancels the tool task only,
so a stop either lands before the commit (nothing written, status `stopped`) or is too late
(`False`). The driver is the only thing that touches Postgres (hooks and the activity row run
inside the commit), after the cancellable part is over. Events the tool emits are kept on the
invocation and fanned out to subscribers; the context's Firecrawl budget reports each paid call
as a `provider` event. Finished invocations stay around for a while so a late reader can still
fetch the result and replay the events.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from app import people, properties
from app.auth import Principal
from app.history import Source
from app.integrations.firecrawl import AsyncFirecrawl
from app.tools import ToolContext, ToolEvent, ToolResult, silent

log = logging.getLogger(__name__)

KEEP = 200                                     # finished invocations kept for late readers
Status = Literal["running", "done", "failed", "stopped"]
Sink = Callable[[ToolEvent], None]
DoneCallback = Callable[["Invocation"], Awaitable[None]]

_recent: deque[Invocation] = deque(maxlen=KEEP)


class Stopped(Exception):
    """The invocation was stopped before it committed; nothing was written."""


class Invocation:
    """One run of one tool on one record."""

    def __init__(self, tool: str, ctx: ToolContext, sink: Sink | None) -> None:
        self.id = uuid.uuid4().hex
        self.tool = tool
        self.person_id = ctx.person_id
        self.status: Status = "running"
        self.events: list[ToolEvent] = []
        self.result: ToolResult | None = None
        self.person: dict[str, Any] | None = None       # the record after the commit
        self.error: str | None = None
        self.exception: BaseException | None = None
        self.credits = 0
        self.calls: list[dict[str, Any]] = []
        self.started_at = datetime.now(UTC)
        self.finished_at: datetime | None = None
        self._ctx = ctx
        self._sink = sink
        self._queues: list[asyncio.Queue[ToolEvent]] = []
        self._done = asyncio.Event()
        self._callbacks: list[DoneCallback] = []
        self._tool_task: asyncio.Task | None = None
        self._driver: asyncio.Task | None = None

    # --- activity ------------------------------------------------------------------------------------

    def emit(self, kind: str, message: str, **data: Any) -> None:
        event = ToolEvent(seq=len(self.events) + 1, ts=datetime.now(UTC), kind=kind,  # type: ignore[arg-type]
                          message=message, data=data)
        self.events.append(event)
        for q in self._queues:
            q.put_nowait(event)
        if self._sink is not None:
            try:
                self._sink(event)
            except Exception:
                log.exception("event sink failed for invocation %s", self.id)

    async def subscribe(self, after: int = 0) -> AsyncIterator[ToolEvent]:
        """Every event after sequence number `after`, replayed then live, up to and including
        the final `result` event."""
        q: asyncio.Queue[ToolEvent] = asyncio.Queue()
        self._queues.append(q)
        try:
            last = after
            for event in list(self.events):
                if event.seq > last:
                    yield event
                    last = event.seq
            if self._done.is_set() and last >= len(self.events):
                return
            while True:
                event = await q.get()
                if event.seq <= last:
                    continue
                yield event
                last = event.seq
                if event.kind == "result":
                    return
        finally:
            self._queues.remove(q)

    # --- control -------------------------------------------------------------------------------------

    def stop(self) -> bool:
        """Cancel the tool before it commits. False when it already returned (the commit may be
        under way) or finished."""
        if self.status != "running" or self._tool_task is None or self._tool_task.done():
            return False
        self._tool_task.cancel()
        return True

    def on_done(self, callback: DoneCallback) -> None:
        """Run `callback(invocation)` when it finishes, before waiters are released."""
        self._callbacks.append(callback)

    async def wait(self) -> ToolResult:
        """The result; raises `Stopped`, or the tool's own exception when it failed."""
        await self._done.wait()
        if self.status == "done":
            assert self.result is not None
            return self.result
        if self.status == "stopped":
            raise Stopped(f"{self.tool} on {self.person_id} was stopped; nothing was written")
        assert self.exception is not None
        raise self.exception

    def public(self, include_result: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "tool": self.tool, "person_id": self.person_id, "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "credits": self.credits, "events": len(self.events), "error": self.error,
            "events_url": f"/api/invocations/{self.id}/events"}
        if include_result:
            out["result"] = self.result.model_dump(mode="json") if self.result else None
        return out

    async def _finish(self, status: Status, result: ToolResult | None = None,
                      error: str | None = None) -> None:
        self.status, self.result, self.error = status, result, error
        self.finished_at = datetime.now(UTC)
        self.emit("result", {"done": "finished", "failed": f"failed: {error}",
                             "stopped": "stopped; nothing was written",
                             "running": ""}[status], status=status, credits=self.credits)
        for callback in self._callbacks:
            try:
                await callback(self)
            except Exception:
                log.exception("done callback failed for invocation %s", self.id)
        self._done.set()


# --- starting and finding invocations ----------------------------------------------------------------------

async def make_context(person_id: str | None, source: Source, principal: Principal | None = None,
                       *, tier: str | None = None, source_doc: dict[str, Any] | None = None,
                       firecrawl: AsyncFirecrawl | None = None, owns_firecrawl: bool = False,
                       sink: Sink | None = None) -> ToolContext | None:
    """Open the record's transaction and build the tool's context; None when the person does not
    exist (so a surface answers 404 before anything starts). With `person_id` None the context is
    for a person that does not exist yet (`tier` required; `source_doc` its `source` field when a
    file's row creates it): the first commit creates it. `sink` receives every event."""
    if person_id is None:
        tx: people.Transaction | None = people.Transaction.new(tier or "member", source_doc)
    else:
        tx = await people.Transaction.open(person_id)
    if tx is None:
        return None
    ctx = ToolContext(tx=tx, source=source, principal=principal, firecrawl=firecrawl,
                      owns_firecrawl=owns_firecrawl)
    ctx.emit = _SinkEmit(sink) if sink is not None else silent
    return ctx


class _SinkEmit:
    """Carries the caller's sink on the context until `invoke` installs the invocation's emit."""

    def __init__(self, sink: Sink) -> None:
        self.sink = sink

    def __call__(self, kind: str, message: str, **data: Any) -> None:
        self.sink(ToolEvent(seq=0, ts=datetime.now(UTC), kind=kind,  # type: ignore[arg-type]
                            message=message, data=data))


def invoke(tool: Callable[..., Awaitable[ToolResult | None]], ctx: ToolContext,
           **kwargs: Any) -> Invocation:
    """Start `tool(ctx, **kwargs)` and return its invocation at once."""
    sink = ctx.emit.sink if isinstance(ctx.emit, _SinkEmit) else None
    inv = Invocation(tool.__name__, ctx, sink)
    ctx.emit = inv.emit
    _recent.append(inv)
    inv._driver = asyncio.create_task(_drive(inv, tool, ctx, kwargs),
                                      name=f"invoke:{tool.__name__}:{inv.id[:8]}")
    return inv


async def run(tool: Callable[..., Awaitable[ToolResult | None]], ctx: ToolContext,
              **kwargs: Any) -> ToolResult:
    """Invoke and wait: what assistants and MCP use."""
    return await invoke(tool, ctx, **kwargs).wait()


def get(invocation_id: str) -> Invocation | None:
    return next((inv for inv in _recent if inv.id == invocation_id), None)


def running() -> list[Invocation]:
    return [inv for inv in _recent if inv.status == "running"]


async def drain(timeout: float = 30) -> None:
    """At shutdown: give running invocations a moment to commit, then cancel the rest."""
    drivers = [inv._driver for inv in running() if inv._driver is not None]
    if not drivers:
        return
    _, pending = await asyncio.wait(drivers, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# --- the driver ---------------------------------------------------------------------------------------------

async def _drive(inv: Invocation, tool: Callable[..., Awaitable[ToolResult | None]],
                 ctx: ToolContext, kwargs: dict[str, Any]) -> None:
    before_credits, before_calls = ctx.credits, len(ctx.calls)
    budget = ctx.firecrawl.budget if ctx.firecrawl is not None else None
    previous_hook = budget.on_charge if budget is not None else None
    if budget is not None:
        budget.on_charge = lambda call: inv.emit(
            "provider", f"{call['target']} · {call['credits']} credit(s)",
            call=call["kind"], target=call["target"], credits=call["credits"])

    def spent() -> None:
        inv.credits = ctx.credits - before_credits
        inv.calls = ctx.calls[before_calls:]

    inv._tool_task = asyncio.create_task(tool(ctx, **kwargs), name=f"tool:{inv.tool}:{inv.id[:8]}")
    try:
        try:
            result = await inv._tool_task
        except asyncio.CancelledError:
            spent()
            await _rollback(ctx)
            if asyncio.current_task().cancelling():          # the driver itself is being cancelled
                await inv._finish("stopped")
                raise
            await inv._finish("stopped")
            return
        except Exception as e:
            log.exception("tool %s failed on %s", inv.tool, inv.person_id)
            spent()
            await _rollback(ctx)
            inv.exception = e
            await inv._finish("failed", error=f"{type(e).__name__}: {e}"[:500])
            return
        spent()
        if result is None:
            await inv._finish("done")
            return
        try:
            write = await ctx.tx.commit(ctx.principal)
        except Exception as e:
            log.exception("commit of %s on %s failed", inv.tool, inv.person_id)
            inv.exception = e
            await inv._finish("failed", error=f"{type(e).__name__}: {e}"[:500])
            return
        if write is not None:
            inv.person = write.person
            result.changes = write.changes
        result.credits, result.calls = inv.credits, inv.calls
        await inv._finish("done", result=result)
    finally:
        if budget is not None:
            budget.on_charge = previous_hook
        if ctx.owns_firecrawl and ctx.firecrawl is not None:
            await ctx.firecrawl.aclose()


async def _rollback(ctx: ToolContext) -> None:
    """Best effort after a stop or failure: registry entries the tool created and nothing uses."""
    for change in ctx.tx.catalog:
        try:
            if change.action == "property_created":
                await properties.remove_unused(change.key)
            elif change.action == "enum_value_added" and isinstance(change.value, str):
                await properties.remove_enum_value_if_unused(change.key, change.value)
        except Exception:
            log.exception("could not clean up %s %s after a stopped tool", change.action, change.key)
