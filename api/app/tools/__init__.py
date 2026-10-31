"""Level 2 — tools.

A tool is one function, one shot: it may read the stores, call its model agent (defined inside
the tool module) and write through the level-1 writers, and it returns a Pydantic result for
programs — never prose for a human. Every result that touches a document carries `changes`, the
shared `history.Change` vocabulary, so callers at level 3 (assistants) and level 4 (surfaces) can
report what happened without re-deriving it.

    search     SearchRequest → SearchResponse          (read-only)
    plan       question → PlanResult{request}          (the planner agent)
    store      person + passage → StoreResult           (the storage agent, then normalize)
    normalize  person → NormalizeResult                 (the parser, then the normaliser agent)
    site       page → SiteFindings                      (the site reader agent)
    company    name → CompanySummary                    (the company agent)

Tools may use other tools (store → normalize, plan → search) but never anything above them.
"""

from pydantic import BaseModel, Field

from app.history import Change


class ToolResult(BaseModel):
    """Base of every tool result that concerns a person."""

    person_id: str | None = None
    changes: list[Change] = Field(default_factory=list,
                                  description="What happened to the document, in order")


__all__ = ["Change", "ToolResult"]
