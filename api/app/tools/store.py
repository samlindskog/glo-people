"""Tool (level 2): store what a passage says about a person. The storage agent (defined here)
reads the passage, the person's current values and the catalog with its descriptions, and
proposes one write per property: typed values in the type's form, list items appended, restated
values confirmed, contradictions marked, new properties only for recurring kinds of fact. The
tool resolves new properties (redirect near-duplicates, create up to `max_new`, send the rest to
notes), writes everything in one `people.update`, then runs `tools.normalize` so flagged values
get their typed form. One shot; the result carries every change."""

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import embedder, fields, people, properties
from app.agents.common import catalog_prompt, run_agent
from app.auth import Principal
from app.history import Change, Source
from app.properties import Property
from app.tools import ToolResult
from app.tools import normalize as normalize_tool
from app.tools.normalize import NormalizeResult

AGENT_NAME = "storage"
REQUEST_LIMIT = 4
OUTPUT_RETRIES = 2        # validation problems are sent back up to twice
MAX_PASSAGE_CHARS = 20_000
MAX_EVIDENCE = 200
LIST_TYPES = ("list", "enum_list")

Value = str | list[str]


class Write(BaseModel):
    """A value for an existing property."""

    key: str = Field(description="An existing property key")
    value: Value = Field(description="Text for text/keyword/url/contact properties; a concise "
                                     "value in the type's form for typed ones ('$2M', 'Seed', "
                                     "'2025-01-15', 'yes'), or the passage's wording when "
                                     "unclear; a list of items for list types")
    action: Literal["set", "append"] = Field(
        "set", description="append (list types only): add these items to the current list")
    conflict: bool = Field(False, description="The passage contradicts the current value")
    evidence: str = Field("", max_length=MAX_EVIDENCE,
                          description="The passage words this rests on, copied verbatim")


class NewProperty(BaseModel):
    """A kind of fact the catalog has no home for."""

    key: str = Field(description="lowercase_snake_case key")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300,
                             description="One sentence: what values this property holds")
    value: Value
    evidence: str = Field("", max_length=MAX_EVIDENCE)


class Proposal(BaseModel):
    writes: list[Write] = Field(default_factory=list, max_length=20)
    new_properties: list[NewProperty] = Field(default_factory=list, max_length=5)
    confirmed: list[str] = Field(default_factory=list, max_length=20,
                                 description="Properties whose current value the passage restates")
    ignored: list[str] = Field(default_factory=list, max_length=10,
                               description="Passage parts not about the person or not storable")


@dataclass
class StorageDeps:
    catalog: dict[str, Property]
    passage: str


agent = Agent(
    name=f"agents.{AGENT_NAME}",
    deps_type=StorageDeps,
    output_type=Proposal,
    retries=OUTPUT_RETRIES,
    instructions="""\
You file what a passage says about one person into their record. You are given the passage, the
person's current values (property: text, with the normalised value in brackets) and the catalog
of properties (key, type, canonical values, how many people have it, what it holds).

Rules:
- Store facts, not sentences. One write per property: merge everything the passage says about
  that property into one value.
- Pick the property by meaning, using the descriptions. For a typed property give the value in
  the form its type expects: a canonical value for enum types ("Seed", "Founder/C-level"), a
  plain amount for numbers ("$2M", "20k"), an ISO date, yes/no for booleans. When the passage is
  unclear, keep its own words; a normaliser cleans those up.
- List properties (list, enum list) take a list of items; use action "append" to add to what is
  already there, "set" only when the passage replaces the list.
- Do not restate what is already there: a property whose current value the passage repeats goes
  to `confirmed`. A different value is a write with `conflict: true`.
- A new property only for a kind of fact that other people could also have and the catalog
  lacks (give a clear key, label and a one-sentence description). One-off remarks, context and
  anything that fits nowhere go to `notes`. Prefer an existing property over a new one.
- `ignored`: greetings, requests, and anything not about this person — copy the passage words
  themselves, do not explain.
- `evidence`: copy the passage words the write rests on, verbatim. Never invent facts.""",
)


def _normalise_ws(text: str) -> str:
    return " ".join(text.split()).lower()


def _evidence_ok(evidence: str, flat_passage: str) -> bool:
    """Every fragment of the evidence (the model joins excerpts with ';', '…' or '...') occurs in
    the passage; fragments under four characters are ignored."""
    fragments = [f.strip() for f in re.split(r"[;…]|\.\.\.", _normalise_ws(evidence))]
    return all(f in flat_passage for f in fragments if len(f) >= 4)


def check(proposal: Proposal, catalog: dict[str, Property],
          passage: str | None = None) -> list[str]:
    """Problems with a proposal against the catalog (empty when it is executable). With the
    passage, evidence must occur in it."""
    problems: list[str] = []
    seen: set[str] = set()
    flat = _normalise_ws(passage) if passage is not None else None
    for w in proposal.writes:
        prop = catalog.get(w.key)
        if prop is None:
            problems.append(f"{w.key!r} is not a property; use a listed key or a new property")
            continue
        if w.key in seen:
            problems.append(f"{w.key!r} has two writes; merge them into one")
        seen.add(w.key)
        if w.action == "append" and prop.type not in LIST_TYPES:
            problems.append(f"{w.key} is {prop.type}: use action 'set', 'append' is for lists")
        if isinstance(w.value, list):
            if not w.value or not all(isinstance(v, str) and v.strip() for v in w.value):
                problems.append(f"{w.key}: a list value needs non-empty items")
        elif not w.value.strip():
            problems.append(f"{w.key}: empty value")
        if flat is not None and w.evidence and not _evidence_ok(w.evidence, flat):
            problems.append(f"{w.key}: evidence must be copied verbatim from the passage")
    for n in proposal.new_properties:
        if not fields.valid_key(n.key):
            problems.append(f"new property {n.key!r}: key must be lowercase letters, digits and "
                            "underscores, not a reserved name")
        elif n.key in catalog:
            problems.append(f"new property {n.key!r} already exists: write to it instead")
        elif n.key in seen:
            problems.append(f"new property {n.key!r} is proposed twice")
        seen.add(n.key)
        if (isinstance(n.value, list) and not n.value) or (isinstance(n.value, str)
                                                            and not n.value.strip()):
            problems.append(f"new property {n.key}: empty value")
        if flat is not None and n.evidence and not _evidence_ok(n.evidence, flat):
            problems.append(f"new property {n.key}: evidence must be copied verbatim")
    for key in proposal.confirmed:
        if key not in catalog:
            problems.append(f"confirmed {key!r} is not a property")
    return problems


@agent.output_validator
async def _executable(ctx: RunContext[StorageDeps], out: Proposal) -> Proposal:
    problems = check(out, ctx.deps.catalog, ctx.deps.passage)
    if problems:
        raise ModelRetry("Fix the proposal: " + "; ".join(problems))
    return out


def render_person(person: dict) -> str:
    """The person's current values as the agent reads them: `key: text  [normalised]`."""
    norm = person.get("norm") or {}
    lines = []
    for key, value in person.items():
        if key in fields.SYSTEM_FIELDS or key in ("id", "tier", "name"):
            continue
        text = "; ".join(map(str, value)) if isinstance(value, list) else str(value)
        text = re.sub(r"\s+", " ", text)[:300]
        extra = f"  [{norm[key]}]" if key in norm else ""
        lines.append(f"{key}: {text}{extra}")
    return "\n".join(lines) or "(no values yet)"


async def _propose(passage: str, person: dict, catalog: list[Property]) -> Proposal:
    passage = passage.strip()[:MAX_PASSAGE_CHARS]
    prompt = (f"Person: {person.get('name') or 'unnamed'}\n\n"
              f"Current values:\n{render_person(person)}\n\n"
              f"Passage:\n{passage}\n\n"
              f"Properties:\n{catalog_prompt(catalog)}")
    deps = StorageDeps({p.key: p for p in catalog}, passage)
    return await run_agent(agent, prompt, deps, request_limit=REQUEST_LIMIT)


# --- the tool -----------------------------------------------------------------------------------------

NOTES = "notes"


class StoreOptions(BaseModel):
    """Caps on new properties."""

    max_new: int = Field(2, ge=0, le=5, description="New properties one passage may create; "
                                                    "the rest of the new facts go to notes")
    near_duplicate: float = Field(0.8, ge=0, le=1,
                                  description="A proposed new property whose name is at least "
                                              "this similar to an existing one is redirected to it")


class StoreResult(ToolResult):
    confirmed: list[str] = Field(default_factory=list,
                                 description="Properties whose current value the passage restated")
    ignored: list[str] = Field(default_factory=list,
                               description="Passage parts not about the person or not storable")
    conflicts: list[str] = Field(default_factory=list,
                                 description="Properties whose new value contradicts the old one")
    created_properties: list[str] = Field(default_factory=list)
    redirected: dict[str, str] = Field(default_factory=dict,
                                       description="Proposed property → existing one used instead")
    to_notes: list[str] = Field(default_factory=list,
                                description="Facts filed under notes because max_new was reached")
    normalization: NormalizeResult | None = Field(None, description="The clean-up pass")


def _merge_list(current: Any, items: list[str]) -> list[str]:
    base = list(current) if isinstance(current, list) else ([current] if current else [])
    seen = {fields.fold(str(v)) for v in base}
    for item in items:
        if fields.fold(item) not in seen:
            base.append(item)
            seen.add(fields.fold(item))
    return base


def _as_text(value: Value) -> str:
    return "; ".join(value) if isinstance(value, list) else value


async def store(person_id: str, passage: str, opts: StoreOptions, principal: Principal,
                source: Source) -> StoreResult | None:
    """File a passage into the person's properties. None if the person does not exist."""
    person = await people.get(person_id)
    if person is None:
        return None
    catalog = await properties.catalog(await people.coverage())
    cat = {p.key: p for p in catalog}
    proposal = await _propose(passage, person, catalog)

    result = StoreResult(person_id=person_id, confirmed=list(proposal.confirmed),
                         ignored=list(proposal.ignored),
                         conflicts=[w.key for w in proposal.writes if w.conflict])
    values: dict[str, Any] = {}
    appended: set[str] = set()

    def put(key: str, value: Value, append: bool) -> None:
        prop = cat.get(key)
        if prop is not None and prop.type in LIST_TYPES:
            incoming = value if isinstance(value, list) else [value]
            if append or key in values:
                values[key] = _merge_list(values.get(key, person.get(key)), incoming)
                appended.add(key)
            else:
                values[key] = incoming
        else:
            text = _as_text(value)
            values[key] = f"{values[key]}\n{text}" if key in values else text

    for w in proposal.writes:
        put(w.key, w.value, w.action == "append")

    if proposal.new_properties:
        vectors = await embedder.embed_texts([f"{n.label}: {n.description}"
                                              for n in proposal.new_properties])
        nearest = await properties.nearest(vectors, k=1)
        for n, near in zip(proposal.new_properties, nearest, strict=True):
            best = near[0] if near else None
            if best is not None and best[1] >= opts.near_duplicate:
                result.redirected[n.key] = best[0]
                put(best[0], n.value, append=True)
            elif len(result.created_properties) < opts.max_new:
                await properties.ensure([n.key], "agent", descriptions={n.key: n.description})
                cat[n.key] = await properties.get(n.key)  # type: ignore[assignment]
                result.created_properties.append(n.key)
                result.changes.append(Change(key=n.key, action="property_created",
                                             value=n.description))
                put(n.key, n.value, append=False)
            else:
                result.to_notes.append(n.label)
                put(NOTES, f"{n.label}: {_as_text(n.value)}", append=False)

    if values:
        write = await people.update(principal, person_id, values, source=source)
        if write is not None:
            for c in write.changes:
                if c.action == "set" and c.key in appended:
                    c = c.model_copy(update={"action": "append"})
                result.changes.append(c)
    result.normalization = await normalize_tool.normalize(
        person_id, principal, Source("agent", "normalizer", source.ref or "store"),
        normalize_tool.NormalizeOptions(repair=False))   # storage is not the moment to re-judge identity
    return result
