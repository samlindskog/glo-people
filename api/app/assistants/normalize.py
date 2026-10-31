"""Assistant (level 3): normalise and repair records for many people. Per person it runs the
normalize tool (the parser, the normaliser agent for what stayed unmapped, and the repair of
identity data for records without a record_kind) and reports what changed in one line. The only
paid calls are FullEnrich name lookups (5 credits) for Persons with a LinkedIn URL and no full
name; the run cap bounds them."""

from typing import Any

from pydantic import Field

from app.assistants.base import AssistantSpec, register
from app.assistants.context import AssistantDeps
from app.auth import Principal
from app.integrations.firecrawl import AsyncFirecrawl, Budget
from app.integrations.fullenrich import MATCH_CREDITS
from app.tools import normalize as normalize_tool
from app.tools.normalize import NormalizeOptions

ASSISTANT = "normalize"


class NormalizeRunOptions(NormalizeOptions):
    max_credits: int = Field(1500, ge=0, le=100_000,
                             description="Firecrawl credit cap for the whole run (one FullEnrich "
                                         "name lookup is 5)")


async def make_deps(run_id: int, item_id: int, person: dict, options: NormalizeRunOptions,
                    principal: Principal) -> AssistantDeps:
    budget = Budget(max_scrapes=1, max_searches=0, max_credits=MATCH_CREDITS)
    client = AsyncFirecrawl(budget=budget) if options.repair and options.provider else None
    return AssistantDeps(firecrawl=client, assistant=ASSISTANT, run_id=run_id, item_id=item_id,
                         person=person, principal=principal, budget=budget, options=options)


def item_cap(options: NormalizeRunOptions) -> int:
    return MATCH_CREDITS if options.repair and options.provider else 0


def describe(result: normalize_tool.NormalizeResult) -> str:
    """One line for a human: what the parser, the agent and the repair settled."""
    parts = []
    for c in result.changes:
        if c.key == "record_kind" and c.action != "enum_value_added":
            continue                                   # summarised as "record kind: …" below
        if c.action == "normalized":
            parts.append(f"{c.key} → {c.value}")
        elif c.action == "flagged":
            parts.append(f"{c.key}: {'no value' if c.flag == 'non_normalizable' else 'not recognised'}")
        elif c.action == "enum_value_added":
            parts.append(f"{c.key}: new value {c.value!r}")
        elif c.action == "set" and c.key == "name":
            parts.append(f"name → {c.value}")
        elif c.action == "removed":
            parts.append(f"{c.key} removed")
    for key, values in result.rejected.items():
        parts.append(f"{key}: {', '.join(repr(v) for v in values)} not added (cap)")
    for key in result.unmapped:
        parts.append(f"{key}: still unmapped")
    if result.repair and result.repair.kind:
        parts.append(f"record kind: {result.repair.kind}")
    text = "; ".join(parts) if parts else "Everything was already normalised"
    if result.agent_called or (result.repair and result.repair.agent_called):
        text += " (agent consulted)"
    return text + "."


async def run_item(deps: AssistantDeps) -> dict[str, Any]:
    result = await deps.tool(normalize_tool.normalize, normalize_tool.AGENT_NAME, opts=deps.options)
    if result is None:
        return {"status": "failed", "error": "person not found", "text": "Person not found.",
                "changes": []}
    assert isinstance(result, normalize_tool.NormalizeResult)
    return {"text": describe(result), "changes": [c.model_dump() for c in result.changes],
            "agent_called": result.agent_called, "unmapped": result.unmapped,
            "merged": result.merged, "allocated": result.allocated, "rejected": result.rejected,
            "repair": result.repair.model_dump(exclude={"changes"}) if result.repair else None}


SPEC = register(AssistantSpec(
    key=ASSISTANT,
    label="Normalise and repair records",
    description=("Re-parses every typed property (number, date, boolean, enum) of each person, "
                 "asks the normaliser agent about the values the parser cannot map, and for "
                 "records without a record kind repairs broken identity data (names from the "
                 "record's own facts, or from the FullEnrich profile behind the LinkedIn URL for "
                 "5 credits) and classifies the record as Person, Company or Unidentifiable."),
    selector_field="name",
    options_model=NormalizeRunOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=item_cap,
))
