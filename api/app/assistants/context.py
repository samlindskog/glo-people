"""What an assistant's `run_item` sees: the item (its person when it has one, its payload), the
run/item identity, the item budget, where its activity goes, and `tool()` — the one way an
assistant changes data: a tool invocation on the item's person, attributed to the assistant,
spending from the item's budget, its events joining the item's. Extends the agents' dependency
object so the same instance can be handed to the tools' agents."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.agents.common import AgentDeps
from app.assistants.base import Budget
from app.auth import Principal
from app.config import get_settings
from app.history import Change, Source
from app.tools import ToolEvent, ToolResult, invoke, record


@dataclass(kw_only=True)
class AssistantDeps(AgentDeps):
    assistant: str
    run_id: int
    item_id: int
    person: dict[str, Any] | None = None   # people.public(..., include_history=True)
    payload: dict[str, Any] = field(default_factory=dict)
    label: str | None = None
    principal: Principal | None = None     # who started the run (for the activity log)
    budget: Budget
    options: Any
    new_tier: str | None = None            # an item whose person does not exist yet: the first
                                           # tool commit creates it in this tier ...
    new_source_doc: dict[str, Any] | None = None   # ... with this `source` field ({file, source_id, row})
    changes: list[Change] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    event_sink: Callable[[ToolEvent], None] | None = None   # the runner's; None when run alone

    @property
    def person_id(self) -> str:
        assert self.person is not None, "this item has no person"
        return self.person["id"]

    @property
    def written(self) -> dict[str, Any]:
        """Property → value this item added (set or append changes)."""
        return {c.key: c.value for c in self.changes if c.action in ("set", "append")}

    def source(self, agent: str) -> Source:
        return Source("agent", f"{self.assistant}/{agent}",
                      f"run:{self.run_id}/item:{self.item_id}")

    def current(self, key: str) -> Any:
        return (self.person or {}).get(key)

    def history_values(self, key: str) -> list[Any]:
        """Every value the field ever held (tombstones excluded), oldest first."""
        return [e["v"] for e in ((self.person or {}).get("history") or {}).get(key, [])
                if e.get("v") is not None]

    def skip(self, step: str, reason: str) -> None:
        self.skipped.append({"step": step, "reason": reason})

    def emit(self, kind: str, message: str, **data: Any) -> None:
        """A line of this item's own activity (a step, a note)."""
        if self.event_sink is not None:
            self.event_sink(ToolEvent(seq=0, ts=datetime.now(UTC), kind=kind,  # type: ignore[arg-type]
                                      message=message, data=data))

    def recent_write(self, by: str, key: str | None = None) -> str | None:
        """The date of the latest history entry written `by` an agent (exact, or a prefix when
        `by` ends with "/") inside the enrich TTL, on `key` or on any property; None when there
        is none. The one answer to "did this assistant already do this recently"."""
        ttl = datetime.now(UTC) - timedelta(days=get_settings().enrich_ttl_days)
        history = (self.person or {}).get("history") or {}
        latest = None
        for k, entries in history.items():
            if key is not None and k != key:
                continue
            for e in entries:
                who = (e.get("src") or {}).get("by") or ""
                hit = who.startswith(by) if by.endswith("/") else who == by
                if hit and e.get("ts") and datetime.fromisoformat(e["ts"]) >= ttl:
                    latest = max(latest or e["ts"], e["ts"])
        return latest[:10] if latest else None

    async def tool(self, fn: Callable[..., Awaitable[ToolResult | None]], agent: str, *,
                   source: Source | None = None, **kwargs: Any) -> ToolResult | None:
        """Run a tool on this item's person: the entries it writes are attributed to
        `assistant/agent` with the run and item as reference (or to `source`, for data that keeps
        its own provenance), it spends from this item's budget, its events join the item's, and
        its changes join the item's. Cancelling the item stops the tool before it commits. None
        when the person no longer exists. With no person yet (`new_tier`), the first commit
        creates one."""
        pid = self.person["id"] if self.person is not None else None
        if pid is None and self.new_tier is None:
            raise AssertionError("this item has no person and no tier to create one in")
        ctx = await invoke.make_context(pid, source or self.source(agent), self.principal,
                                        tier=self.new_tier, source_doc=self.new_source_doc,
                                        firecrawl=self.firecrawl, sink=self.event_sink)
        if ctx is None:
            return None
        inv = invoke.invoke(fn, ctx, **kwargs)
        try:
            result = await inv.wait()
        except asyncio.CancelledError:
            inv.stop()
            raise
        if inv.person is not None:
            self.person = inv.person
        self.changes.extend(result.changes)
        return result

    async def write(self, agent: str, values: dict[str, Any], *, append: Iterable[str] = (),
                    source: Source | None = None) -> None:
        """Write `values` to the person's properties through the record tool (keys in `append`
        are merged into the current value); the writer reports what actually changed (values
        equal to the current one append nothing)."""
        values = {k: v for k, v in values.items() if v not in (None, "", [])}
        if values:
            await self.tool(record.write, agent, source=source, values=values,
                            append=[k for k in append if k in values])
