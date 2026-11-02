"""Assistant (level 3): the same edit on many people. The bulk write behind "mark everyone from
that upload as plus two", "set region Europe for these forty people" or "remove the bogus phone on
the ones I just listed": each item writes the given values (and tier) through the record tool,
attributed to whoever started the run, then closes with the shared review when something changed.
Selected like any people assistant: ids, the people of another run (`from_run`), or a filter."""

from typing import Any

from pydantic import BaseModel, Field

from app import fields
from app.assistants.base import AssistantSpec, register
from app.assistants.context import AssistantDeps
from app.assistants.review import review
from app.auth import Principal
from app.history import Source
from app.integrations.firecrawl import Budget
from app.tools import record

ASSISTANT = "update"


class UpdateOptions(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict,
                                   description="Property key → value to write on every person; "
                                               "null removes the value")
    append: list[str] = Field(default_factory=list,
                              description="Keys among `values` merged into the current value "
                                          "(list items added, a text line added) instead of "
                                          "replacing it")
    tier: fields.Tier | None = Field(None, description="Move every person to this tier")
    review: bool = Field(True, description="Close each person with the review (normaliser and "
                                           "repair) when something changed")
    dry_run: bool = Field(False, description="Report what would be written, write nothing")


async def make_deps(run_id: int, item: dict, options: UpdateOptions,
                    principal: Principal | None) -> AssistantDeps:
    return AssistantDeps(assistant=ASSISTANT, run_id=run_id, item_id=item["id"],
                         person=item.get("person"), payload=item["payload"],
                         label=item.get("label"), principal=principal,
                         budget=Budget(max_scrapes=0, max_searches=0, max_credits=0),
                         options=options)


def _changes(options: UpdateOptions, person: dict[str, Any]) -> dict[str, Any]:
    """The values that differ from the person's current ones (a dry run's answer)."""
    out = {}
    for key, value in options.values.items():
        current = person.get(key)
        if key in options.append and isinstance(current, list) and isinstance(value, list):
            if any(fields.fold(str(v)) not in {fields.fold(str(c)) for c in current} for v in value):
                out[key] = value
        elif value is None:
            if current not in (None, "", []):
                out[key] = None
        elif value != current:
            out[key] = value
    return out


async def run_item(deps: AssistantDeps) -> dict[str, Any]:
    options: UpdateOptions = deps.options
    assert deps.person is not None
    if not options.values and options.tier is None:
        return {"status": "skipped", "text": "Nothing to write: no values and no tier.",
                "changes": [], "skipped": deps.skipped}
    would = _changes(options, deps.person)
    tier_change = options.tier if options.tier and options.tier != deps.person.get("tier") else None
    if options.dry_run:
        parts = [f"would set {', '.join(sorted(would))}" if would else "nothing to set"]
        if tier_change:
            parts.append(f"would move to {tier_change}")
        return {"dry_run": True, "would": {"values": would, "tier": tier_change},
                "text": fields.sentence("; ".join(parts)) + ".", "changes": [], "skipped": deps.skipped}
    source = Source.for_rest(deps.principal) if deps.principal is not None else None
    res = await deps.tool(record.write, "record", source=source, values=options.values,
                          tier=options.tier, append=options.append)
    if res is None:
        return {"status": "failed", "error": "person not found", "text": "Person not found.",
                "changes": [], "skipped": deps.skipped}
    written = sorted({c.key for c in res.changes if c.action in ("set", "append", "removed")})
    kept = sorted({c.key for c in res.changes if c.action == "kept"})
    rev = await review(deps) if options.review and any(k != "tier" for k in written) else None
    parts = [f"wrote {', '.join(written)}" if written else "nothing new"]
    if kept:
        parts.append(f"recorded but not shown: {', '.join(kept)}")
    if rev and rev.get("kind"):
        parts.append(f"record kind {rev['kind']}")
    return {"written": written, "kept": kept, "review": rev,
            "changes": [c.model_dump() for c in deps.changes], "skipped": deps.skipped,
            "text": fields.sentence("; ".join(parts)) + "."}


DETAILS = """\
**What it does.** Writes the same values (and, optionally, a tier) on every selected person through the record tool, as if each had been edited by the person who started the run, and closes each with the shared review when something changed. It is the bulk edit behind "mark everyone from that upload as plus two" or "set region Europe for these people".

**Starting it.** Select people like any people assistant: a list of ids, the people another run touched (`from_run`, e.g. the merge run of an upload), or everyone matching filters (`where`, with an optional tier). Options: `values` (property → value; null removes), `append` (keys merged into the current value instead of replacing it), `tier`, `review` (true), `dry_run` (false: report what would change, write nothing). Free apart from the review's model calls.

**Writing.** Values are appended to each property's history under the starter's source (a UI or API write), so they rank as a person's own edit; a value equal to the current one appends nothing; a value that does not outrank the one shown is recorded and reported as kept. The tier changes only when it differs.

**Result.** Per person: the keys written, the keys recorded but not shown, the review, every change; a dry run lists the values and the tier that would change.
"""

SPEC = register(AssistantSpec(
    key=ASSISTANT,
    label="Update many people",
    description=("Writes the same values or tier on every selected person (ids, the people of "
                 "another run, or a filter) through the record tool and closes each with the "
                 "review; dry run reports what would change."),
    options_model=UpdateOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=lambda options: 0,
    details=DETAILS,
))
