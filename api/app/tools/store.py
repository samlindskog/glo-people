"""Tool (level 2): store what a passage says about a person. The storage agent (defined here)
reads the passage, the person's current values and the catalog with its descriptions, and
proposes one write per property: typed values in the type's form, list items appended, restated
values confirmed, contradictions marked, new properties only for recurring kinds of fact. The
tool resolves new properties (redirect near-duplicates, create up to `max_new`, send the rest to
notes or drop them per `overflow`), stages everything on the context's transaction, then runs `tools.normalize` on the same
transaction so flagged values get their typed form: one commit for all of it. The result carries
every change."""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import embedder, fields, people, properties
from app.agents.common import catalog_prompt, run_agent
from app.history import Change
from app.properties import Property
from app.tools import Emit, ToolContext, ToolResult, silent
from app.tools import normalize as normalize_tool
from app.tools.normalize import NormalizeResult

AGENT_NAME = "storage"
REQUEST_LIMIT = 4
OUTPUT_RETRIES = 2        # validation problems are sent back up to twice
MAX_PASSAGE_CHARS = fields.MAX_TEXT_CHARS
LIST_TYPES = ("list", "enum_list")
AUDIT_KEYS = frozenset({"enrichment_passage"})   # records of what was filed: not facts to re-file

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
    evidence: str = Field("", description="The passage words this rests on, copied verbatim")


class NewProperty(BaseModel):
    """A kind of fact the catalog has no home for."""

    key: str = Field(description="lowercase_snake_case key")
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300,
                             description="One sentence: what values this property holds")
    value: Value
    evidence: str = ""


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
    current: dict[str, Any] = field(default_factory=dict)   # the person's current values
    locked: frozenset[str] = frozenset()                   # keys the caller sets itself


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


_TYPOGRAPHIC = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
                              "\u2013": "-", "\u2014": "-", "\u00a0": " ", "\u200b": "",
                              "\u2800": " "})
LONG_QUOTE = 40     # a quote this long is located by its start or end, not matched whole


def _normalise_ws(text: str) -> str:
    """Lower-case, one space between words, typographic quotes and dashes folded to ASCII (models
    copy "I’ve" as "I've"), invisible spacers removed."""
    return " ".join(text.translate(_TYPOGRAPHIC).split()).lower()


def _evidence_ok(evidence: str, flat_passage: str) -> bool:
    """Every fragment of the evidence (the model joins excerpts with ';', '…' or '...') occurs in
    the passage. A long fragment counts when its first or last `LONG_QUOTE` characters do: the
    model copies the start of a long about text and then shortens it, which still locates the
    words the write rests on. Fragments under four characters are ignored."""
    fragments = [f.strip() for f in re.split(r"[;…]|\.\.\.", _normalise_ws(evidence))]

    def found(f: str) -> bool:
        return f in flat_passage or (len(f) >= LONG_QUOTE
                                     and (f[:LONG_QUOTE] in flat_passage
                                          or f[-LONG_QUOTE:] in flat_passage))
    return all(found(f) for f in fragments if len(f) >= 4)


def check(proposal: Proposal, catalog: dict[str, Property], passage: str | None = None,
          current: dict[str, Any] | None = None, locked: frozenset[str] = frozenset()) -> list[str]:
    """Problems with a proposal against the catalog (empty when it is executable). With the
    passage, evidence must occur in it; with `current`, a confirmation needs a current value;
    `locked` keys are the caller's to write (confirm them, or leave them alone)."""
    problems: list[str] = []
    seen: set[str] = set()
    flat = _normalise_ws(passage) if passage is not None else None
    for w in proposal.writes:
        prop = catalog.get(w.key)
        if prop is None:
            problems.append(f"{w.key!r} is not a property; use a listed key or a new property")
            continue
        if w.key in locked:
            problems.append(f"{w.key} is set by the caller from the same data: do not write it "
                            "(confirm it if the passage restates it, otherwise leave it out)")
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
        elif current is not None and current.get(key) in (None, "", []) and key not in locked:
            problems.append(f"{key} has no current value, so the passage cannot confirm it: write "
                            "the value instead")
    return problems


@agent.output_validator
async def _executable(ctx: RunContext[StorageDeps], out: Proposal) -> Proposal:
    problems = check(out, ctx.deps.catalog, ctx.deps.passage, ctx.deps.current, ctx.deps.locked)
    if problems:
        raise ModelRetry("Fix the proposal: " + "; ".join(problems))
    return out


def render_person(person: dict) -> str:
    """The person's current values as the agent reads them: `key: text  [normalised]`."""
    norm = person.get("norm") or {}
    lines = []
    for key, value in person.items():
        if key in fields.SYSTEM_FIELDS or key in ("id", "tier", "name") or key in AUDIT_KEYS:
            continue
        text = "; ".join(map(str, value)) if isinstance(value, list) else str(value)
        text = re.sub(r"\s+", " ", text)[:fields.MAX_TEXT_CHARS]
        extra = f"  [{norm[key]}]" if key in norm else ""
        lines.append(f"{key}: {text}{extra}")
    return "\n".join(lines) or "(no values yet)"


async def _propose(passage: str, person: dict, catalog: list[Property],
                   emit: Emit = silent, locked: frozenset[str] = frozenset()) -> Proposal:
    passage = passage.strip()[:MAX_PASSAGE_CHARS]
    note = (f"\n\nNot yours to write (set by the caller from the same data; confirm or ignore): "
            f"{', '.join(sorted(locked))}" if locked else "")
    prompt = (f"Person: {person.get('name') or 'unnamed'}\n\n"
              f"Current values:\n{render_person(person)}\n\n"
              f"Passage:\n{passage}\n\n"
              f"Properties:\n{catalog_prompt(catalog)}{note}")
    deps = StorageDeps({p.key: p for p in catalog}, passage,
                       {k: v for k, v in person.items() if k not in fields.SYSTEM_FIELDS}, locked)
    return await run_agent(agent, prompt, deps, request_limit=REQUEST_LIMIT, on_event=emit)


# --- the tool -----------------------------------------------------------------------------------------

NOTES = "notes"


class StoreOptions(BaseModel):
    """Caps on new properties, and what happens to facts beyond them."""

    max_new: int = Field(2, ge=0, le=5, description="New properties one passage may create; "
                                                    "the rest of the new facts go to notes")
    near_duplicate: float = Field(0.8, ge=0, le=1,
                                  description="A proposed new property whose name is at least "
                                              "this similar to an existing one is redirected to it")
    overflow: Literal["notes", "ignore"] = Field(
        "notes", description="Where facts beyond max_new go: filed under notes as 'Label: value', "
                             "or dropped and reported (for provider passages)")
    locked: list[str] = Field(default_factory=list,
                              description="Properties the caller writes itself from the same data "
                                          "(identity, employer); the agent may confirm them but "
                                          "not write them")


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
    dropped: list[str] = Field(default_factory=list,
                               description="Facts dropped because max_new was reached (overflow ignore)")
    evidence: dict[str, str] = Field(default_factory=dict,
                                     description="Property → the passage words its write rests on")
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


async def store(ctx: ToolContext, passage: str, opts: StoreOptions | None = None) -> StoreResult:
    """File a passage into the person's properties: staged on the context's transaction, then
    the normalize tool (without repair: storage is not the moment to re-judge identity)."""
    opts = opts or StoreOptions()
    person = ctx.tx.public()
    ctx.emit("step", f"reading {len(passage)} characters about {person.get('name') or 'the record'}")
    catalog = await properties.catalog(await people.coverage())
    cat = {p.key: p for p in catalog}
    proposal = await _propose(passage, person, catalog, ctx.emit, frozenset(opts.locked))

    result = StoreResult(person_id=ctx.person_id, confirmed=list(proposal.confirmed),
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
        if w.evidence:
            result.evidence[w.key] = w.evidence

    if proposal.new_properties:
        vectors = await embedder.embed_texts([f"{n.label}: {n.description}"
                                              for n in proposal.new_properties])
        nearest = await properties.nearest(vectors, k=1)
        for n, near in zip(proposal.new_properties, nearest, strict=True):
            best = near[0] if near else None
            if best is not None and best[1] >= opts.near_duplicate:
                result.redirected[n.key] = best[0]
                put(best[0], n.value, append=True)
                if n.evidence:
                    result.evidence[best[0]] = n.evidence
            elif len(result.created_properties) < opts.max_new:
                await properties.ensure([n.key], "agent", descriptions={n.key: n.description})
                cat[n.key] = await properties.get(n.key)  # type: ignore[assignment]
                result.created_properties.append(n.key)
                ctx.tx.record_catalog(Change(key=n.key, action="property_created",
                                             value=n.description), tag="store")
                ctx.emit("note", f"new property {n.key}: {n.description}", key=n.key)
                put(n.key, n.value, append=False)
                if n.evidence:
                    result.evidence[n.key] = n.evidence
            elif opts.overflow == "ignore":
                result.dropped.append(n.label)
                ctx.emit("note", f"dropped {n.label!r}: no property for it and none may be created",
                         label=n.label)
            else:
                result.to_notes.append(n.label)
                put(NOTES, f"{n.label}: {_as_text(n.value)}", append=False)

    if values:
        for c in await ctx.tx.set_values(values, ctx.source, appended=appended, tag="store"):
            if c.action in ("set", "append", "removed"):
                ctx.emit("write", f"{c.key}: {c.action}", key=c.key, action=c.action)
    ctx.emit("step", f"{len(values)} propert{'y' if len(values) == 1 else 'ies'} staged, "
                     f"{len(result.confirmed)} confirmed, {len(result.ignored)} ignored")
    result.normalization = await normalize_tool.normalize(
        ctx.child(normalize_tool.AGENT_NAME), normalize_tool.NormalizeOptions(repair=False))
    result.changes = ctx.tx.changes("store", "normalize", "repair")
    return result
