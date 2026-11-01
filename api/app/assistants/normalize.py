"""Assistant (level 3): normalise and repair records for many people. Per person: when the record
looks like a person, has a LinkedIn URL and no full name that the record itself can settle, the
enrich ladder runs first (same item, same budget), so the paid profile supplies the name and
everything else it knows; then the normalize tool (the parser, the normaliser agent for what
stayed unmapped, and the repair of identity data for records without a record_kind) and one line
on what changed. The enrich ladder is the only paid part; the run cap bounds it."""

from typing import Any

from pydantic import Field

from app.assistants.base import AssistantSpec, register
from app.assistants.context import AssistantDeps
from app.assistants.enrich import assistant as enrich_assistant
from app.assistants.enrich import budget as enrich_budget
from app.assistants.enrich.models import EnrichOptions
from app.auth import Principal
from app.integrations.firecrawl import AsyncFirecrawl, Budget
from app.integrations.fullenrich import MATCH_CREDITS
from app.integrations.profile import is_full_name
from app.tools import normalize as normalize_tool
from app.tools.normalize import NormalizeOptions
from app.urls import linkedin_profile

ASSISTANT = "normalize"
NOT_PEOPLE = {"Company"}        # record kinds the enrich ladder is not for


class NormalizeRunOptions(NormalizeOptions):
    enrich: bool = Field(True, description="Run the enrich assistant first for a record that looks "
                                           "like a person, has a LinkedIn URL and no full name: "
                                           "the paid profile fills the name and the rest")
    max_credits: int = Field(1500, ge=0, le=100_000,
                             description="Firecrawl credit cap for the whole run (an enrichment "
                                         "is 3 credits on average, 17 at most)")


def _enrich_options() -> EnrichOptions:
    return EnrichOptions(refresh=True)      # the name is wanted even if a TTL would skip


async def make_deps(run_id: int, item: dict, options: NormalizeRunOptions,
                    principal: Principal | None) -> AssistantDeps:
    if options.enrich:
        budget = enrich_budget.budget_for(_enrich_options())
    else:
        budget = Budget(max_scrapes=1, max_searches=0, max_credits=MATCH_CREDITS)
    paid = options.enrich or (options.repair and options.provider)
    client = AsyncFirecrawl(budget=budget) if paid else None
    return AssistantDeps(firecrawl=client, assistant=ASSISTANT, run_id=run_id, item_id=item["id"],
                         person=item["person"], label=item.get("label"), principal=principal,
                         budget=budget, options=options)


def item_cap(options: NormalizeRunOptions) -> int:
    if options.enrich:
        return enrich_budget.item_cap(_enrich_options())
    return MATCH_CREDITS if options.repair and options.provider else 0


def wants_enrich(deps: AssistantDeps, options: NormalizeRunOptions) -> bool:
    """A person-looking record (not a Company) with a LinkedIn profile URL and no full name."""
    if not options.enrich:
        return False
    kind = deps.current("record_kind")
    if isinstance(kind, str) and kind in NOT_PEOPLE:
        return False
    if is_full_name(str(deps.current("name") or "")):
        return False
    return bool(linkedin_profile(deps.current("linkedin")))


async def _enrich_first(deps: AssistantDeps) -> dict[str, Any]:
    """The enrich ladder for this item's person, on this item's budget and event stream; its
    changes and skipped steps join the item's."""
    deps.emit("step", "no full name on the record: enriching it first")
    edeps = AssistantDeps(firecrawl=deps.firecrawl, assistant=enrich_assistant.ASSISTANT,
                          run_id=deps.run_id, item_id=deps.item_id, person=deps.person,
                          label=deps.label, principal=deps.principal, budget=deps.budget,
                          options=_enrich_options(), event_sink=deps.event_sink)
    res = await enrich_assistant.run_item(edeps)
    deps.person = edeps.person
    deps.changes.extend(edeps.changes)
    deps.skipped.extend({"step": f"enrich/{s['step']}", "reason": s["reason"]}
                        for s in edeps.skipped)
    keep = ("text", "status", "linkedin", "linkedin_provider", "providers", "company", "storage",
            "stored", "not_found")
    return {k: res.get(k) for k in keep if k in res}


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
    options: NormalizeRunOptions = deps.options
    enriched = await _enrich_first(deps) if wants_enrich(deps, options) else None
    opts = options
    if deps.current("record_kind") == "Unidentifiable" \
            and is_full_name(str(deps.current("name") or "")):
        # marked while the name was incomplete (by an earlier run or before this item's
        # enrichment); the name is complete now, so the kind is judged afresh
        opts = options.model_copy(update={"force": True})
    result = await deps.tool(normalize_tool.normalize, normalize_tool.AGENT_NAME, opts=opts)
    if result is None:
        return {"status": "failed", "error": "person not found", "text": "Person not found.",
                "changes": []}
    assert isinstance(result, normalize_tool.NormalizeResult)
    text = describe(result)
    if enriched:
        text = f"Enriched first: {(enriched.get('text') or '').rstrip('.')}. {text}"
    return {"text": text, "changes": [c.model_dump() for c in deps.changes],
            "skipped": deps.skipped, "enrich": enriched,
            "agent_called": result.agent_called, "unmapped": result.unmapped,
            "merged": result.merged, "allocated": result.allocated, "rejected": result.rejected,
            "repair": result.repair.model_dump(exclude={"changes"}) if result.repair else None}


DETAILS = """\
**What it does.** Brings one record's typed values up to date and, once per record, repairs its identity data and classifies it. When the record looks like a person, has a LinkedIn URL and no first-and-last name, the enrich assistant runs first on the same item and budget, so the paid profile supplies the name along with everything else it knows (see the enrich assistant's spec). Then three stages, one write: the parser, the normaliser agent for what the parser cannot read, and the repair tool. Everything is staged on the record's transaction and lands in a single compare-and-set write; stopping before that writes nothing.

**Starting it.** One person, or everyone with a property (the Properties page's Normalise buttons do that per property). Options: `enrich` (true: enrich first a person-looking record with a LinkedIn URL and no full name), `allocate` (true: agents may add canonical values to enum properties), `max_new_values` (3 new enum values per record per call), `repair` (true), `provider` (false: let the repair tool itself ask FullEnrich for a missing full name, 5 credits; the enrichment covers this), `force` (false: repair even when the record kind is set), `max_credits` (1500 for the run). Each item reserves the enrichment's worst case while it runs; the run pauses at the cap and Resume with a higher cap continues it.

**Stage 1, the parser (free).** For every typed property (number, date, boolean, enum, enum list) whose current text has not been normalised yet, the deterministic parser gives the typed value, or flags the text: `unmapped` when it cannot read it, `non_normalizable` when it says there is no value. Enum texts match canonical values and their aliases case-insensitively. Decisions an agent made earlier for the same text are kept.

**Stage 2, the normaliser agent (one model call, only when something stayed unmapped).** It sees each unmapped text with the property's type and canonical values, and who the person is (name, company, role, one-liner, bio, sectors, company description). Per item it merges the text into an existing value (the text becomes an alias, so the parser handles it next time), allocates a new canonical value when the text names a category the list lacks and fits the person (enum types only, within `max_new_values`, one new value per property per call), or drops it as non-normalisable. A validator fixes spellings and rejects unusable answers; what the caps refuse is reported as rejected.

**Stage 3, repair (when the record has no record kind yet, or with `force`).** Mechanical fixes first: mojibake re-decoded, an all-lowercase name title-cased (never a handle, email or URL). Then the repair agent judges the record from its own facts (name history, LinkedIn slug, emails, website, company, role, headline, bio, intro, notes, source file): the kind (Person, Company, Unidentifiable, or a new kind it allocates), a first-and-last name only when the current one is broken and the evidence gives it verbatim, and values filed under the wrong property (moved to the property it names, usually notes, and tombstoned where they were; at most 3). A Person whose name stays partial after the enrichment is marked Unidentifiable; a record the earlier runs marked Unidentifiable is re-judged when the enrichment completes its name. (With `provider`, the repair tool itself asks FullEnrich for the name instead, 5 credits.)

**The write.** One commit carries the norm entries, any new values and the record kind, each history entry attributed to its author: the parser as system/normalize, the agent as normalize/normalizer with the run and item reference, the repair agent as repair, mechanical fixes as system/repair, provider data as repair/fullenrich.

**Cost profile.** Model calls only for records with unmapped texts or without a record kind, plus the enrichment's storage call; Firecrawl credits only for the enrichment of persons lacking a full name with a LinkedIn URL (about 3 credits each). A second run over everyone is nearly free: the parser confirms, the agents are not consulted, repair is skipped.

**Result.** One line per person ("Enriched first: LinkedIn profile matched; …; wrote name, about, … stage → Seed; record kind: Person (agent consulted)"), every change (the enrichment's and the normalisation's), the skipped steps, the enrichment's outcome, the merges, allocations and rejections, what is still unmapped, and the repair's kind, name source, signals, mechanical fixes and credits.
"""

SPEC = register(AssistantSpec(
    key=ASSISTANT,
    label="Normalise and repair records",
    description=("Re-parses every typed property (number, date, boolean, enum) of each person, "
                 "asks the normaliser agent about the values the parser cannot map, enriches "
                 "first a person-looking record that has a LinkedIn URL but no full name (the "
                 "profile supplies the name and the rest), and for records without a record kind "
                 "repairs broken identity data from the record's own facts and classifies the "
                 "record as Person, Company or Unidentifiable."),
    selector_field="name",
    details=DETAILS,
    options_model=NormalizeRunOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=item_cap,
))
