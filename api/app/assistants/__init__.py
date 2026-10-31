"""Level 3 — assistants (user-facing agents).

An assistant composes tools into something a person asks for and answers in text meant for a
human, plus the normalised list of `changes` it caused through the tools, so every surface can
show both. Every assistant runs as a run (`assistants.runner`): `enrich` and `normalize` per
person, `import`, `embed` and `reparse` over batches, `ask` as one item whose result is the
answer (MCP also calls `ask()` directly for a synchronous answer).
"""

from pydantic import BaseModel, Field

from app.assistants.base import REGISTRY, AssistantSpec, Budget, StopRun, register
from app.history import Change


class AssistantResult(BaseModel):
    """Base of every assistant result: the text for the human, and what was changed."""

    text: str
    changes: list[Change] = Field(default_factory=list)


def load_all() -> None:
    """Import every assistant so it registers itself."""
    from app.assistants import ask, embed, enrich, import_csv, normalize, reparse  # noqa: F401


__all__ = ["REGISTRY", "AssistantResult", "AssistantSpec", "Budget", "Change", "StopRun",
           "load_all", "register"]
