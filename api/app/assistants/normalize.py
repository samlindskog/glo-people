"""Assistant (level 3): normalise typed properties for many people. Per person it runs the
normalize tool (the parser, then the normaliser agent for what stayed unmapped) and reports what
changed in one line. No external data is fetched; the only cost is the model call for people
with unmapped values."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.assistants.base import AssistantSpec, register
from app.assistants.context import AssistantDeps
from app.auth import Principal
from app.history import Source
from app.tools import normalize as normalize_tool

ASSISTANT = "normalize"


class NormalizeOptions(BaseModel):
    """Nothing to tune: the parser is deterministic and the agent gets one call per person."""


@dataclass
class NoBudget:
    credits: int = 0
    max_credits: int = 0


async def make_deps(run_id: int, item_id: int, person: dict, options: NormalizeOptions,
                    principal: Principal) -> AssistantDeps:
    return AssistantDeps(assistant=ASSISTANT, run_id=run_id, item_id=item_id, person=person,
                         principal=principal, budget=NoBudget(), options=options)


def item_cap(options: NormalizeOptions) -> int:
    return 0


def describe(result: normalize_tool.NormalizeResult) -> str:
    """One line for a human: what the parser and the agent settled."""
    parts = []
    for c in result.changes:
        if c.action == "normalized":
            parts.append(f"{c.key} → {c.value}")
        elif c.action == "flagged":
            parts.append(f"{c.key}: {'no value' if c.flag == 'non_normalizable' else 'not recognised'}")
    for key in result.unmapped:
        parts.append(f"{key}: still unmapped")
    text = "; ".join(parts) if parts else "Everything was already normalised"
    if result.agent_called:
        text += " (agent consulted)"
    return text + "."


async def run_item(deps: AssistantDeps) -> dict[str, Any]:
    result = await normalize_tool.normalize(
        deps.person_id, deps.principal,
        Source("agent", f"{ASSISTANT}/normalizer", f"run:{deps.run_id}/item:{deps.item_id}"))
    if result is None:
        return {"status": "failed", "error": "person not found", "text": "Person not found.",
                "changes": []}
    deps.changes.extend(result.changes)
    return {"text": describe(result), "changes": [c.model_dump() for c in result.changes],
            "agent_called": result.agent_called, "unmapped": result.unmapped}


SPEC = register(AssistantSpec(
    key=ASSISTANT,
    label="Normalise typed properties",
    description=("Re-parses every typed property (number, date, boolean, enum) of each person "
                 "and asks the normaliser agent about the values the parser cannot map, so "
                 "filters on normalised values see them. No external data is fetched; the "
                 "only cost is the model call for people with unmapped values."),
    selector_field="name",
    options_model=NormalizeOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=item_cap,
))
