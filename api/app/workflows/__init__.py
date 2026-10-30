"""Workflows: pydantic-ai agents that enrich people records, run from the Workflows page.

Each workflow lives in its own subpackage and registers a `WorkflowSpec` here. The runner
(app.workflows.runner) persists runs and their per-person items in Postgres and executes them
in-process; `WorkflowDeps` (app.workflows.context) is what a workflow's `run_item` receives.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.workflows.context import WorkflowDeps
from app.workflows.firecrawl import Budget


@dataclass(frozen=True)
class WorkflowSpec:
    key: str
    label: str
    description: str
    selector_field: str                      # "everyone with this field" bulk selector
    options_model: type[BaseModel]
    run_item: Callable[[WorkflowDeps], Awaitable[dict[str, Any]]]
    budget_for: Callable[[BaseModel], Budget]

    def public(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "description": self.description,
                "selector_field": self.selector_field,
                "options_schema": self.options_model.model_json_schema()}


REGISTRY: dict[str, WorkflowSpec] = {}


def register(spec: WorkflowSpec) -> WorkflowSpec:
    REGISTRY[spec.key] = spec
    return spec


def load_all() -> None:
    """Import every workflow package so it registers itself."""
    from app.workflows import enrich  # noqa: F401
