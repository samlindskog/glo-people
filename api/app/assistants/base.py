"""What a per-person assistant is: its spec, the registry, and the contract with the runner.

A per-person assistant composes tools for one person and is run over many people by
`app.assistants.runner`. The runner knows nothing about external services: an assistant builds
its own dependencies (`make_deps`), reports spend through `deps.budget.credits`, returns
`{"text", "changes", ...}` per person, and raises `StopRun` when the whole run must stop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from app.auth import Principal

if TYPE_CHECKING:
    from app.assistants.context import AssistantDeps


class Budget(Protocol):
    credits: int        # spent so far by this item
    max_credits: int    # the most this item may spend


class StopRun(Exception):
    """Raised by an assistant for one person when the run as a whole cannot continue (e.g. the
    external account is out of credits). The item is marked failed and the rest skipped."""


@dataclass(frozen=True)
class AssistantSpec:
    key: str
    label: str
    description: str
    selector_field: str                      # "everyone with this field" bulk selector
    options_model: type[BaseModel]
    # (run_id, item_id, person, options, principal) -> deps for one item
    make_deps: Callable[[int, int, dict, BaseModel, Principal], Awaitable[AssistantDeps]]
    run_item: Callable[[AssistantDeps], Awaitable[dict[str, Any]]]
    item_cap: Callable[[BaseModel], int]     # worst-case credits one item may spend

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "description": self.description,
                "selector_field": self.selector_field,
                "options_schema": self.options_model.model_json_schema()}


REGISTRY: dict[str, AssistantSpec] = {}


def register(spec: AssistantSpec) -> AssistantSpec:
    REGISTRY[spec.key] = spec
    return spec
