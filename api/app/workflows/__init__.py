"""Workflows: per-person orchestration of services and agents, run from the Workflows page.

Each workflow lives in its own subpackage and registers a `WorkflowSpec` (app.workflows.base). The
runner (app.workflows.runner) persists runs and their per-person items in Postgres and executes
them in-process; `WorkflowDeps` (app.workflows.context) is what a workflow's `run_item` receives.
"""

from app.workflows.base import REGISTRY, Budget, StopRun, WorkflowSpec, register

__all__ = ["REGISTRY", "Budget", "StopRun", "WorkflowSpec", "load_all", "register"]


def load_all() -> None:
    """Import every workflow package so it registers itself."""
    from app.workflows import enrich  # noqa: F401
