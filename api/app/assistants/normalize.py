"""Assistant (level 3): normalise and repair records for many people. Per person, in this order:
the normalize tool's free stages (the parser, the normaliser agent for what stayed unmapped);
then, only when the record looks like a person, has a LinkedIn URL and still has no full name,
the enrich ladder on the same item and budget, so the paid profile supplies the name and
everything else it knows; then the repair of identity data (record kind, misfiled values) on the
completed record. The enrich ladder is the only paid part; the run cap bounds it."""

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import Field

from app.assistants.base import AssistantSpec, register
from app.assistants.context import AssistantDeps
from app.assistants.enrich import assistant as enrich_assistant
from app.assistants.enrich import budget as enrich_budget
from app.assistants.enrich.models import EnrichOptions
from app.auth import Principal
from app.config import get_settings
from app.integrations.firecrawl import AsyncFirecrawl, Budget
from app.integrations.profile import is_full_name
from app.tools import normalize as normalize_tool
from app.tools.normalize import NormalizeOptions
from app.urls import linkedin_profile

ASSISTANT = "normalize"
NOT_PEOPLE = {"Company"}        # record kinds the enrich ladder is not for


class NormalizeRunOptions(NormalizeOptions):
    enrich: bool = Field(True, description="After parsing, run the enrich assistant for a record "
                                           "that looks like a person, has a LinkedIn URL and still "
                                           "has no full name: the paid profile fills the name and "
                                           "the rest")
    max_credits: int = Field(1500, ge=0, le=100_000,
                             description="Firecrawl credit cap for the whole run (an enrichment "
                                         "is 3 credits on average, 17 at most)")


def _enrich_options() -> EnrichOptions:
    return EnrichOptions(refresh=True)      # the name is wanted even if a TTL would skip


async def make_deps(run_id: int, item: dict, options: NormalizeRunOptions,
                    principal: Principal | None) -> AssistantDeps:
    if options.enrich:
        budget = enrich_budget.budget_for(_enrich_options())
        client: AsyncFirecrawl | None = AsyncFirecrawl(budget=budget)
    else:
        budget, client = Budget(max_scrapes=0, max_searches=0, max_credits=0), None
    return AssistantDeps(firecrawl=client, assistant=ASSISTANT, run_id=run_id, item_id=item["id"],
                         person=item["person"], label=item.get("label"), principal=principal,
                         budget=budget, options=options)


def item_cap(options: NormalizeRunOptions) -> int:
    return enrich_budget.item_cap(_enrich_options()) if options.enrich else 0


def enriched_recently(deps: AssistantDeps) -> str | None:
    """The date of the last enrich write on the record inside the TTL, or None: an enrichment
    that left the name incomplete is not repeated, the providers would answer the same."""
    ttl = datetime.now(UTC) - timedelta(days=get_settings().enrich_ttl_days)
    latest = None
    for entries in ((deps.person or {}).get("history") or {}).values():
        for e in entries:
            by = (e.get("src") or {}).get("by") or ""
            if by.startswith("enrich/") and e.get("ts") and datetime.fromisoformat(e["ts"]) >= ttl:
                latest = max(latest or e["ts"], e["ts"])
    return latest[:10] if latest else None


def wants_enrich(deps: AssistantDeps, options: NormalizeRunOptions) -> bool:
    """A person-looking record (not a Company) with a LinkedIn profile URL and no full name, not
    enriched within the TTL."""
    if not options.enrich:
        return False
    kind = deps.current("record_kind")
    if isinstance(kind, str) and kind in NOT_PEOPLE:
        return False
    if is_full_name(str(deps.current("name") or "")):
        return False
    if not linkedin_profile(deps.current("linkedin")):
        return False
    if when := enriched_recently(deps):
        deps.skip("enrich", f"enriched {when}; the providers gave no full name then")
        return False
    return True


async def _enrich(deps: AssistantDeps) -> dict[str, Any]:
    """The enrich ladder for this item's person, on this item's budget and event stream; its
    changes and skipped steps join the item's."""
    deps.emit("step", "no full name on the record: enriching it")
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
            "stored", "not_found", "review")
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


FAILED = {"status": "failed", "error": "person not found", "text": "Person not found.",
          "changes": []}


async def run_item(deps: AssistantDeps) -> dict[str, Any]:
    options: NormalizeRunOptions = deps.options
    # 1. the free stages: the parser and the normaliser agent
    first = await deps.tool(normalize_tool.normalize, normalize_tool.AGENT_NAME,
                            opts=options.model_copy(update={"repair": False}))
    if first is None:
        return dict(FAILED)
    assert isinstance(first, normalize_tool.NormalizeResult)
    # 2. enrichment, only when the record still cannot settle its own name
    enriched = await _enrich(deps) if wants_enrich(deps, options) else None
    # 3. repair on the completed record (the parser only confirms on this pass); an enrichment
    #    ends with its own review of the record, which stands in for this stage
    result = first
    review = (enriched or {}).get("review") or {}
    repair_from_review = ({"kind": review["kind"], "name_source": review.get("name_source"),
                           "agent_called": True, "note": review.get("note", ""),
                           "mechanical": review.get("mechanical", []),
                           "rejected": review.get("rejected", []), "signals": [], "confidence": None}
                          if review.get("kind") else None)
    if options.repair and repair_from_review is None:
        # an Unidentifiable mark is re-judged every time: the rule is common sense now, and the
        # record may have gained a name or evidence since
        force = options.force or deps.current("record_kind") == "Unidentifiable"
        second = await deps.tool(normalize_tool.normalize, normalize_tool.AGENT_NAME,
                                 opts=options.model_copy(update={"force": force}))
        if second is None:
            return dict(FAILED)
        assert isinstance(second, normalize_tool.NormalizeResult)
        result = second.model_copy(update={
            "changes": first.changes + second.changes,
            "agent_called": first.agent_called or second.agent_called,
            "merged": {**first.merged, **second.merged},
            "allocated": {**first.allocated, **second.allocated},
            "rejected": {**first.rejected, **second.rejected}})
    text = describe(result)
    if enriched:
        text += f" Enriched: {enriched.get('text') or ''}"
    return {"text": text, "changes": [c.model_dump() for c in deps.changes],
            "skipped": deps.skipped, "enrich": enriched,
            "agent_called": result.agent_called, "unmapped": result.unmapped,
            "merged": result.merged, "allocated": result.allocated, "rejected": result.rejected,
            "repair": (repair_from_review if repair_from_review is not None else
                       result.repair.model_dump(exclude={"changes"}) if result.repair else None)}


DETAILS = """\
**What it does.** Brings one record's typed values up to date and, once per record, repairs its identity data and classifies it. In order: the free stages (the parser, then the normaliser agent for what the parser cannot read); then, only when the record looks like a person, has a LinkedIn URL and still has no first-and-last name, the enrich assistant on the same item and budget, so the paid profile supplies the name along with everything else it knows (see the enrich assistant's spec); then the repair tool on the completed record. Each stage lands in its own compare-and-set write; stopping mid-way loses nothing already written.

**Starting it.** One person, or everyone with a property (the Properties page's Normalise buttons do that per property). Options: `enrich` (true: enrich a person-looking record with a LinkedIn URL whose name is still incomplete after parsing), `allocate` (true: agents may add canonical values to enum properties), `max_new_values` (3 new enum values per record per call), `repair` (true), `force` (false: repair even when the record kind is set), `max_credits` (1500 for the run). Each item reserves the enrichment's worst case while it runs; the run pauses at the cap and Resume with a higher cap continues it.

**Stage 1, the parser (free).** For every typed property (number, date, boolean, enum, enum list) whose current text has not been normalised yet, the deterministic parser gives the typed value, or flags the text: `unmapped` when it cannot read it, `non_normalizable` when it says there is no value. Enum texts match canonical values and their aliases case-insensitively. Decisions an agent made earlier for the same text are kept.

**Stage 2, the normaliser agent (one model call, only when something stayed unmapped).** It sees each unmapped text with the property's type and canonical values, and who the person is (name, company, role, one-liner, bio, sectors, company description). Per item it merges the text into an existing value (the text becomes an alias, so the parser handles it next time), allocates a new canonical value when the text names a category the list lacks and fits the person (enum types only, within `max_new_values`, one new value per property per call), or drops it as non-normalisable. A validator fixes spellings and rejects unusable answers; what the caps refuse is reported as rejected.

**Stage 3, repair (when the record has no record kind yet, or with `force`).** Mechanical fixes first: mojibake re-decoded, an all-lowercase name title-cased (never a handle, email or URL). Then the repair agent judges the record from its own facts (name history, LinkedIn slug, emails, website, company, role, headline, bio, intro, notes, source file): the kind (Person, Company, Unidentifiable, or a new kind it allocates), a first-and-last name only when the current one is broken and the evidence gives it verbatim, and values filed under the wrong property (moved to the property it names, usually notes, and tombstoned where they were; at most 3). The repair tool never calls a data provider. The kind is common sense: a human name, even a first name alone, is a Person; Company for an organisation recorded as a person; Unidentifiable only when nothing on the record names a human or an organisation. A record marked Unidentifiable is re-judged, free, on every run. When the enrichment ran, its closing review stands in for this stage; a record enriched within the last 90 days without gaining a full name is not enriched again.

**The write.** One commit carries the norm entries, any new values and the record kind, each history entry attributed to its author: the parser as system/normalize, the agent as normalize/normalizer with the run and item reference, the repair agent as repair, mechanical fixes as system/repair, the enrichment's writes as enrich/<step>.

**Cost profile.** Model calls only for records with unmapped texts or without a record kind, plus the enrichment's storage call; Firecrawl credits only for the enrichment of persons lacking a full name with a LinkedIn URL (about 3 credits each). A second run over everyone is nearly free: the parser confirms, the agents are not consulted, repair is skipped.

**Result.** One line per person ("stage → Seed; record kind: Person (agent consulted). Enriched: LinkedIn profile matched; …; wrote name, about, …"), every change (the enrichment's and the normalisation's), the skipped steps, the enrichment's outcome, the merges, allocations and rejections, what is still unmapped, and the repair's kind, name source, signals, mechanical fixes and credits.
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
