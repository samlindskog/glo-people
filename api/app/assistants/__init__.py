"""Level 3 — assistants (user-facing agents).

An assistant composes tools into something a person asks for and answers in text meant for a
human, plus the normalised list of `changes` it caused through the tools, so every surface can
show both. `ask` answers a question synchronously; `enrich` and `normalize` are per-person
assistants that the runner (`assistants.runner`) executes over many people as a run, with
budgets, progress and traces.
"""

from pydantic import BaseModel, Field

from app.assistants.base import REGISTRY, AssistantSpec, Budget, StopRun, register
from app.history import Change


class AssistantResult(BaseModel):
    """Base of every assistant result: the text for the human, and what was changed."""

    text: str
    changes: list[Change] = Field(default_factory=list)


def load_all() -> None:
    """Import every per-person assistant so it registers itself."""
    from app.assistants import enrich, normalize  # noqa: F401


__all__ = ["REGISTRY", "AssistantResult", "AssistantSpec", "Budget", "Change", "StopRun",
           "load_all", "register"]
