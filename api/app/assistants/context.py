"""What a per-person assistant's `run_item` sees: the person, the run/item identity, the item
budget, and a `write` helper that appends to the person's properties with an agent source and
collects the resulting changes. Extends the agents' dependency object so the same instance can
be handed to the tools' agents."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.agents.common import AgentDeps
from app.assistants.base import Budget
from app.auth import Principal
from app.history import Change, Source
from app.tools import ToolEvent, ToolResult, invoke, record


@dataclass(kw_only=True)
class AssistantDeps(AgentDeps):
    assistant: str
    run_id: int
    item_id: int
    person: dict[str, Any]              # people.public(..., include_history=True)
    principal: Principal                # who started the run (for the activity log)
    budget: Budget
    options: Any
    changes: list[Change] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    @property
    def person_id(self) -> str:
        return self.person["id"]

    @property
    def written(self) -> dict[str, Any]:
        """Property → value this item added (set or append changes)."""
        return {c.key: c.value for c in self.changes if c.action in ("set", "append")}

    def source(self, agent: str) -> Source:
        return Source("agent", f"{self.assistant}/{agent}",
                      f"run:{self.run_id}/item:{self.item_id}")

    def current(self, key: str) -> Any:
        return self.person.get(key)

    def history_values(self, key: str) -> list[Any]:
        """Every value the field ever held (tombstones excluded), oldest first."""
        return [e["v"] for e in (self.person.get("history") or {}).get(key, [])
                if e.get("v") is not None]

    def skip(self, step: str, reason: str) -> None:
        self.skipped.append({"step": step, "reason": reason})

    def on_tool_event(self, event: ToolEvent) -> None:
        """Where the events of this item's tools go; the runner replaces it per item."""

    async def tool(self, fn: Callable[..., Awaitable[ToolResult | None]], agent: str,
                   **kwargs: Any) -> ToolResult | None:
        """Run a tool on this item's person: the entries it writes are attributed to
        `assistant/agent` with the run and item as reference, it spends from this item's budget,
        its events reach `on_tool_event`, and its changes join the item's. None when the person
        no longer exists."""
        ctx = await invoke.make_context(self.person_id, self.source(agent), self.principal,
                                        firecrawl=self.firecrawl, sink=self.on_tool_event)
        if ctx is None:
            return None
        inv = invoke.invoke(fn, ctx, **kwargs)
        result = await inv.wait()
        if inv.person is not None:
            self.person = inv.person
        self.changes.extend(result.changes)
        return result

    async def write(self, agent: str, values: dict[str, Any]) -> None:
        """Append `values` to the person's properties through the record tool; the writer
        reports what actually changed (values equal to the current one append nothing)."""
        values = {k: v for k, v in values.items() if v not in (None, "", [])}
        if values:
            await self.tool(record.write, agent, values=values)
