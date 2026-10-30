"""What a workflow is: its spec, the registry, and the contract with the runner.

A workflow is orchestration over services and agents, run per person by `app.workflows.runner`.
The runner knows nothing about external services: a workflow builds its own dependencies
(`make_deps`), reports spend through `deps.budget.credits`, and raises `StopRun` when the whole
run must stop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from app.auth import Principal

if TYPE_CHECKING:
    from app.workflows.context import WorkflowDeps


class Budget(Protocol):
    credits: int        # spent so far by this item
    max_credits: int    # the most this item may spend


class StopRun(Exception):
    """Raised by a workflow item when the run as a whole cannot continue (e.g. the external
    account is out of credits). The item is marked failed and the remaining items skipped."""


@dataclass(frozen=True)
class WorkflowSpec:
    key: str
    label: str
    description: str
    selector_field: str                      # "everyone with this field" bulk selector
    options_model: type[BaseModel]
    # (run_id, item_id, person, options, principal) -> deps for one item
    make_deps: Callable[[int, int, dict, BaseModel, Principal], Awaitable[WorkflowDeps]]
    run_item: Callable[[WorkflowDeps], Awaitable[dict[str, Any]]]
    item_cap: Callable[[BaseModel], int]     # worst-case credits one item may spend

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "description": self.description,
                "selector_field": self.selector_field,
                "options_schema": self.options_model.model_json_schema()}


REGISTRY: dict[str, WorkflowSpec] = {}


def register(spec: WorkflowSpec) -> WorkflowSpec:
    REGISTRY[spec.key] = spec
    return spec
