"""What a workflow's run_item sees: the person, its budgeted Firecrawl client, and a `write`
helper that appends to the person's fields with an agent source."""

from dataclasses import dataclass, field
from typing import Any

from app import people
from app.auth import Principal
from app.history import Source
from app.workflows.firecrawl import AsyncFirecrawl, Budget


@dataclass
class WorkflowDeps:
    workflow: str
    run_id: int
    item_id: int
    person: dict[str, Any]              # public_person(..., include_history=True)
    principal: Principal                # who started the run (for the activity log)
    firecrawl: AsyncFirecrawl
    budget: Budget
    options: Any
    written: dict[str, Any] = field(default_factory=dict)
    skipped: list[dict[str, str]] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def person_id(self) -> str:
        return self.person["id"]

    def source(self, agent: str) -> Source:
        return Source("agent", f"{self.workflow}/{agent}", f"run:{self.run_id}/item:{self.item_id}")

    def current(self, key: str) -> Any:
        return self.person.get(key)

    def history_values(self, key: str) -> list[Any]:
        """Every value the field ever held (tombstones excluded), oldest first."""
        return [e["v"] for e in (self.person.get("history") or {}).get(key, [])
                if e.get("v") is not None]

    def skip(self, step: str, reason: str) -> None:
        self.skipped.append({"step": step, "reason": reason})

    async def write(self, agent: str, values: dict[str, Any]) -> None:
        """Append `values` to the person's fields. The history layer drops values equal to the
        current one, so `written` only records what this run item actually added."""
        values = {k: v for k, v in values.items() if v not in (None, "", [])}
        if not values:
            return
        src = self.source(agent)
        updated = await people.update(self.principal, self.person_id, values, source=src)
        if updated is None:
            return
        self.person = updated
        for key, value in values.items():
            entries = (updated.get("history") or {}).get(key) or []
            if entries and (entries[-1].get("src") or {}).get("ref") == src.ref:
                self.written[key] = value
