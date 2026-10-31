"""The normalise workflow: one normalise-person operation per item, no credits involved."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.auth import Principal
from app.history import Source
from app.normalize import person as normalize_person
from app.workflows.context import WorkflowDeps

WORKFLOW = "normalize"


class NormalizeOptions(BaseModel):
    """Nothing to tune: the parser is deterministic and the agent gets one call per person."""


@dataclass
class NoBudget:
    credits: int = 0
    max_credits: int = 0


async def make_deps(run_id: int, item_id: int, person: dict, options: NormalizeOptions,
                    principal: Principal) -> WorkflowDeps:
    return WorkflowDeps(workflow=WORKFLOW, run_id=run_id, item_id=item_id, person=person,
                        principal=principal, budget=NoBudget(), options=options)


def item_cap(options: NormalizeOptions) -> int:
    return 0


async def run_item(deps: WorkflowDeps) -> dict[str, Any]:
    report = await normalize_person.run(deps.person_id, deps.principal,
                                        Source("agent", f"{WORKFLOW}/normalizer",
                                               f"run:{deps.run_id}/item:{deps.item_id}"))
    if report is None:
        return {"status": "failed", "error": "person not found"}
    return {**report.model_dump(), "written": {**report.mapped,
                                               **dict.fromkeys(report.non_normalizable, None)}}
