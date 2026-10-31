"""Level 2 — tools.

A tool is one function acting on ONE record, one shot: it may read the stores, call its model
agent (defined inside the tool module) and reach a provider, and it returns a Pydantic result
for programs — never prose for a human. A writing tool never writes: it stages its changes on
the transaction in its `ToolContext` and `tools.invoke` commits once after it returns, so a tool
that is stopped has changed nothing. While it runs it reports what it does through
`ctx.emit(kind, message, **data)`; what it spent is on the context's Firecrawl budget. Every
result that concerns a document carries `changes`, the shared `history.Change` vocabulary, plus
`credits` and `calls`, so level 3 (assistants) and level 4 (surfaces) report without re-deriving.

    search     SearchRequest → SearchResponse           (read-only)
    plan       question → PlanResult{request}           (the planner agent; read-only)
    record     ctx + values → ToolResult                (the smallest write)
    store      ctx + passage → StoreResult              (the storage agent, then normalize)
    normalize  ctx → NormalizeResult                    (the parser, the normaliser agent, repair)
    repair     ctx → RepairResult                       (identity data; the repair agent)
    site       page → SiteFindings                      (the site reader agent; read-only)
    company    name → CompanySummary                    (the company agent; read-only)

Tools may use other tools on the same context (store → normalize → repair share one transaction
and one commit; plan → search) but never anything above them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.auth import Principal
from app.history import Change, Source
from app.integrations.firecrawl import AsyncFirecrawl
from app.people import Transaction

EventKind = Literal["step", "agent", "provider", "write", "note", "error", "result"]
Emit = Callable[..., None]            # emit(kind, message, **data)


class ToolEvent(BaseModel):
    """One line of a tool's activity, as surfaces stream it."""

    seq: int
    ts: datetime
    kind: EventKind
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Base of every tool result that concerns a person."""

    person_id: str | None = None
    changes: list[Change] = Field(default_factory=list,
                                  description="What happened to the document, in order")
    credits: int = Field(0, description="Firecrawl credits this call spent")
    calls: list[dict[str, Any]] = Field(default_factory=list,
                                        description="The paid calls: kind, target, credits")


def silent(kind: str, message: str, **data: Any) -> None:
    """The emit of a context nobody listens to."""


@dataclass
class ToolContext:
    """What a writing tool acts on: the record's transaction, who and what the write is
    attributed to, the Firecrawl client (with its budget) it may spend, and where its activity
    goes. `invoke.make_context` builds one; a tool derives a `child` context for a tool it calls."""

    tx: Transaction
    source: Source
    principal: Principal | None = None
    firecrawl: AsyncFirecrawl | None = None
    emit: Emit = silent
    owns_firecrawl: bool = False          # the invocation closes the client when it is done

    @property
    def person_id(self) -> str:
        return self.tx.person_id

    @property
    def credits(self) -> int:
        budget = self.firecrawl.budget if self.firecrawl is not None else None
        return budget.credits if budget is not None else 0

    @property
    def calls(self) -> list[dict[str, Any]]:
        budget = self.firecrawl.budget if self.firecrawl is not None else None
        return list(budget.calls) if budget is not None else []

    def child(self, agent: str) -> ToolContext:
        """The context for a tool this tool calls: same transaction, budget and activity, the
        entries attributed to `agent` with this write's reference."""
        return ToolContext(tx=self.tx, source=Source("agent", agent, self.source.ref),
                           principal=self.principal, firecrawl=self.firecrawl, emit=self.emit)


__all__ = ["Change", "Emit", "EventKind", "ToolContext", "ToolEvent", "ToolResult", "silent"]
