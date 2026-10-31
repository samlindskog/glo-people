"""What a per-person assistant's `run_item` sees: the person, the run/item identity, the item
budget, and a `write` helper that appends to the person's properties with an agent source and
collects the resulting changes. Extends the agents' dependency object so the same instance can
be handed to the tools' agents."""

from dataclasses import dataclass, field
from typing import Any

from app import people
from app.agents.common import AgentDeps
from app.assistants.base import Budget
from app.auth import Principal
from app.history import Change, Source


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

    async def write(self, agent: str, values: dict[str, Any]) -> None:
        """Append `values` to the person's properties; the writer reports what actually changed
        (values equal to the current one append nothing)."""
        values = {k: v for k, v in values.items() if v not in (None, "", [])}
        if not values:
            return
        res = await people.update(self.principal, self.person_id, values,
                                  source=self.source(agent))
        if res is None:
            return
        self.person = res.person
        self.changes.extend(res.changes)
