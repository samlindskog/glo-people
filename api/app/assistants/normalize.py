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


async def make_deps(run_id: int, item: dict, options: NormalizeRunOptions,
                    principal: Principal | None) -> AssistantDeps:
    budget = Budget(max_scrapes=1, max_searches=0, max_credits=MATCH_CREDITS)
    client = AsyncFirecrawl(budget=budget) if options.repair and options.provider else None
    return AssistantDeps(firecrawl=client, assistant=ASSISTANT, run_id=run_id, item_id=item["id"],
                         person=item["person"], label=item.get("label"), principal=principal,
                         budget=budget, options=options)


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


DETAILS = """\
**What it does.** Brings one record's typed values up to date and, once per record, repairs its identity data and classifies it. Three stages, one write: the parser, the normaliser agent for what the parser cannot read, and the repair tool. Everything is staged on the record's transaction and lands in a single compare-and-set write; stopping before that writes nothing.

**Starting it.** One person, or everyone with a property (the Properties page's Normalise buttons do that per property). Options: `allocate` (true: agents may add canonical values to enum properties), `max_new_values` (3 new enum values per record per call), `repair` (true), `provider` (true: ask FullEnrich for a missing full name), `force` (false: repair even when the record kind is set), `max_credits` (1500 for the run). Each item reserves 5 credits while it runs; the run pauses at the cap and Resume with a higher cap continues it.

**Stage 1, the parser (free).** For every typed property (number, date, boolean, enum, enum list) whose current text has not been normalised yet, the deterministic parser gives the typed value, or flags the text: `unmapped` when it cannot read it, `non_normalizable` when it says there is no value. Enum texts match canonical values and their aliases case-insensitively. Decisions an agent made earlier for the same text are kept.

**Stage 2, the normaliser agent (one model call, only when something stayed unmapped).** It sees each unmapped text with the property's type and canonical values, and who the person is (name, company, role, one-liner, bio, sectors, company description). Per item it merges the text into an existing value (the text becomes an alias, so the parser handles it next time), allocates a new canonical value when the text names a category the list lacks and fits the person (enum types only, within `max_new_values`, one new value per property per call), or drops it as non-normalisable. A validator fixes spellings and rejects unusable answers; what the caps refuse is reported as rejected.

**Stage 3, repair (when the record has no record kind yet, or with `force`).** Mechanical fixes first: mojibake re-decoded, an all-lowercase name title-cased (never a handle, email or URL). Then the repair agent judges the record from its own facts (name history, LinkedIn slug, emails, website, company, role, headline, bio, intro, notes, source file): the kind (Person, Company, Unidentifiable, or a new kind it allocates), a first-and-last name only when the current one is broken and the evidence gives it verbatim, and values filed under the wrong property (moved to the property it names, usually notes, and tombstoned where they were; at most 3). A Person still without a full name whose record has a LinkedIn URL triggers one FullEnrich lookup (5 credits, a miss is free): the profile's name is taken, and because the lookup was paid for its headline, GitHub, location and company are written too. A Person whose name stays partial is marked Unidentifiable.

**The write.** One commit carries the norm entries, any new values and the record kind, each history entry attributed to its author: the parser as system/normalize, the agent as normalize/normalizer with the run and item reference, the repair agent as repair, mechanical fixes as system/repair, provider data as repair/fullenrich.

**Cost profile.** Model calls only for records with unmapped texts or without a record kind; Firecrawl credits only for Persons lacking a full name with a LinkedIn URL. A second run over everyone is nearly free: the parser confirms, the agents are not consulted, repair is skipped.

**Result.** One line per person ("stage → Seed; region: new value 'Lisbon'; name → Pooja Nagpal; record kind: Person (agent consulted)"), every change, the merges, allocations and rejections, what is still unmapped, and the repair's kind, name source, signals, mechanical fixes, provider use and credits.
"""

SPEC = register(AssistantSpec(
    key=ASSISTANT,
    label="Normalise and repair records",
    description=("Re-parses every typed property (number, date, boolean, enum) of each person, "
                 "asks the normaliser agent about the values the parser cannot map, and for "
                 "records without a record kind repairs broken identity data (names from the "
                 "record's own facts, or from the FullEnrich profile behind the LinkedIn URL for "
                 "5 credits) and classifies the record as Person, Company or Unidentifiable."),
    selector_field="name",
    details=DETAILS,
    options_model=NormalizeRunOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=item_cap,
))
