"""Tool (level 2): bring one person's normalised values up to date. The parser runs first (free,
through the people store); whatever it left `unmapped` goes to the normaliser agent in one call,
with the person's context (bio, company, role, sectors) so it can judge. For enum properties the
agent may merge a text into an existing canonical value (recorded as an alias, so the parser
handles it next time), allocate a new canonical value when the text names a category the list
lacks and it fits the person (capped per call), or drop it as non-normalisable when it is not a
value of that property at all. Then, for a record that has no `record_kind` yet (or on `force`),
the repair tool fixes broken identity data and classifies the record. Returns the changes
(normalized / flagged / enum_value_added, plus the repair's), the merges and allocations, what was
rejected by the caps, what is still unmapped, and the repair result."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import fields, history, people, properties
from app import normalize as parser
from app.agents.common import run_agent
from app.history import Change
from app.normalize import NON_NORMALIZABLE, Parsed
from app.tools import Emit, ToolContext, ToolResult, silent
from app.tools import repair as repair_tool
from app.tools.repair import RepairOptions, RepairResult

AGENT_NAME = "normalizer"
REQUEST_LIMIT = 3
ENUM_TYPES = ("enum", "enum_list")
MAX_VALUE_CHARS = 40
CONTEXT_KEYS = ("company", "role", "one_liner", "bio", "sectors", "company_description")


class NormalizeOptions(BaseModel):
    """Whether the agents may extend enum properties, and whether the record is repaired."""

    allocate: bool = Field(True, description="Let the agents add canonical values to enum "
                                             "properties when a text names a category the list "
                                             "lacks (one per property per call)")
    max_new_values: int = Field(3, ge=0, le=10, description="New enum values per call, in total")
    repair: bool = Field(True, description="Repair identity data and set record_kind for records "
                                           "that have none yet")
    provider: bool = Field(True, description="During repair, ask FullEnrich (5 credits) for the "
                                             "full name when the record has a LinkedIn URL and "
                                             "no full name")
    force: bool = Field(False, description="Repair even when record_kind is already set")


class NormalizeResult(ToolResult):
    agent_called: bool = False
    unmapped: list[str] = Field(default_factory=list,
                                description="Typed properties still without a value afterwards")
    merged: dict[str, str] = Field(default_factory=dict,
                                   description="Property → canonical value its text was merged "
                                               "into (the text became an alias)")
    allocated: dict[str, list[str]] = Field(default_factory=dict,
                                            description="Property → new canonical values")
    rejected: dict[str, list[str]] = Field(default_factory=dict,
                                           description="Property → values the agent proposed "
                                                       "that the caps did not allow")
    repair: RepairResult | None = Field(None, description="The identity repair, when it ran")


# --- the normaliser agent ---------------------------------------------------------------------------

class Item(BaseModel):
    """One text to normalise."""

    field: str
    text: str
    type: str                                   # number | boolean | date | enum | enum_list
    values: list[str] = Field(default_factory=list, description="Canonical values (enum types)")


class Mapping(BaseModel):
    field: str
    value: str | int | float | bool | list[str] | None = Field(
        None, description="The normalised value: a canonical value (or list of them) — or, for an "
                          "enum property, a new canonical value when the text names a category "
                          "the list lacks; a number; an ISO date; true/false. Null when the text "
                          "gives no value of this property.")
    non_normalizable: bool = Field(
        False, description="True when the text says there is no value, or is not a value of this "
                           "property at all (e.g. an industry in a stage field); then value is null")
    note: str = Field("", description="Why, in a few words")


class Normalised(BaseModel):
    items: list[Mapping]


@dataclass
class NormalizerDeps:
    items: dict[str, Item]
    lookups: dict[str, dict[str, str]]


agent = Agent(
    name=f"agents.{AGENT_NAME}",
    deps_type=NormalizerDeps,
    output_type=Normalised,
    instructions="""\
Each item is a text someone entered for a typed property, with the property's type and, for enum
types, its canonical values. You also see who the person is (company, role, bio, sectors).

Give the normalised value the text expresses: for numbers a plain number (ranges: the lower
bound; "pre-revenue": 0), for dates an ISO date, for booleans true/false. For enum properties:
- Merge: when the text means the same as a canonical value, or is a variant or intensified form
  of one ("seed round closed" → "Seed"; "Yes-future events" → "Yes, future events"; "super warm"
  → "Warm"), answer that value in its exact spelling.
- Allocate: when the text clearly names a category of this property that the list lacks and it
  fits the person (a city not yet listed for region; a real sector; a stage the list does not
  have), answer a new canonical value: short, in the style of the existing values, one category
  only. Never allocate a variant of an existing value, a free-form remark, or a value that
  belongs to another property.
- Drop: when the text is not a value of this property for this person (an industry list in the
  stage field, a note, "n/a", something about someone else), mark non_normalizable.
For an enum list, answer every canonical value the text covers (merging or allocating per item).
Never invent a value the text does not support; one answer per item, in the same order.""",
)


def _clean_value(text: str) -> str:
    return " ".join(str(text).split())


@agent.output_validator
async def _admissible(ctx: RunContext[NormalizerDeps], out: Normalised) -> Normalised:
    """Non-enum values must parse; enum values must be canonical, or well-formed new values (the
    tool decides whether allocation is allowed). Canonical spellings are fixed here."""
    problems = []
    fixed = []
    for m in out.items:
        item = ctx.deps.items.get(m.field)
        if item is None:
            problems.append(f"{m.field!r} was not asked about")
            continue
        if m.non_normalizable or m.value is None:
            fixed.append(Mapping(field=m.field, value=None, non_normalizable=True, note=m.note))
            continue
        if item.type in ENUM_TYPES:
            raw = m.value if isinstance(m.value, list) else [m.value]
            lookup = ctx.deps.lookups.get(m.field, {})
            values = []
            for v in raw:
                v = _clean_value(v)
                canon = lookup.get(fields.fold(v))
                if canon:
                    values.append(canon)
                elif not v or len(v) > MAX_VALUE_CHARS or not fields.fold(v):
                    problems.append(f"{m.field}: {v!r} is not a usable value (1–{MAX_VALUE_CHARS} "
                                    "characters, words)")
                else:
                    values.append(v)
            if item.type == "enum" and len(values) != 1:
                problems.append(f"{m.field}: exactly one value for an enum property")
                continue
            fixed.append(Mapping(field=m.field, value=values if item.type == "enum_list"
                                 else values[0], note=m.note))
            continue
        parsed = parser.parse(m.value, item.type, None)
        if parsed.value is None:
            problems.append(f"{m.field}: {m.value!r} is not a valid {item.type} value")
            continue
        fixed.append(Mapping(field=m.field, value=_jsonable(parsed.value), note=m.note))
    if problems:
        raise ModelRetry("; ".join(problems))
    return Normalised(items=fixed)


def _jsonable(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def person_context(doc: dict) -> str:
    """Who the person is, for the agent's judgement: a few short properties."""
    lines = []
    for key in ("name", *CONTEXT_KEYS):
        value = history.current(doc.get(key))
        if value not in (None, "", []):
            text = fields.text_of(value)
            lines.append(f"{key}: {text[:fields.MAX_TEXT_CHARS]}")
    return "\n".join(lines) or "(nothing else known)"


async def _decide(items: list[Item], lookups: dict[str, dict[str, str]], context: str,
                  emit: Emit = silent) -> Normalised:
    lines = []
    for it in items:
        values = f" values: {', '.join(it.values)}" if it.values else ""
        lines.append(f"- {it.field} ({it.type}{values}): {it.text!r}")
    prompt = f"Person:\n{context}\n\nItems:\n" + "\n".join(lines)
    deps = NormalizerDeps({it.field: it for it in items}, lookups)
    return await run_agent(agent, prompt, deps, request_limit=REQUEST_LIMIT, on_event=emit)


# --- the tool -----------------------------------------------------------------------------------------

async def normalize(ctx: ToolContext, opts: NormalizeOptions | None = None) -> NormalizeResult:
    """Parser first, then the agent for what stayed unmapped, then the repair of the record; all
    staged on the context's transaction."""
    opts = opts or NormalizeOptions()
    result = NormalizeResult(person_id=ctx.person_id)
    await _values(ctx, opts, result)
    doc = ctx.tx.view
    if opts.repair and (opts.force or history.current(doc.get(repair_tool.KIND)) is None):
        result.repair = await repair_tool.repair(
            ctx.child(repair_tool.AGENT_NAME),
            RepairOptions(provider=opts.provider, allocate=opts.allocate))
    result.changes = ctx.tx.changes("normalize", "repair")
    return result


async def _values(ctx: ToolContext, opts: NormalizeOptions, result: NormalizeResult) -> None:
    """The typed-value stages: the parser, then the agent."""
    doc = ctx.tx.view
    parsed = await people.pending_normalization(doc)
    if parsed:
        changes = await ctx.tx.set_norm(parsed, people.NORMALIZE_SRC, tag="normalize")
        ctx.emit("step", f"parser: {len(parsed)} typed value{'s' if len(parsed) != 1 else ''} "
                         f"re-read, {len(changes)} changed", keys=sorted(parsed))
        doc = ctx.tx.view

    items: list[Item] = []
    lookups: dict[str, dict[str, str]] = {}
    for key, prop in doc.items():
        if key in fields.SYSTEM_FIELDS or not history.is_prop(prop):
            continue
        value = history.current(prop)
        if value is None or history.norm_flag(prop) != "unmapped":
            continue
        sp = properties.spec(key)
        if sp is None:
            continue
        values = await properties.enum_values(key)
        if values:
            lookups[key] = await properties.enum_lookup(key)
        items.append(Item(field=key, text=fields.text_of(value), type=sp.type, values=values))
    if not items:
        return

    result.agent_called = True
    ctx.emit("step", "asking the normaliser about " + ", ".join(i.field for i in items),
             keys=[i.field for i in items])
    decided_by_agent = await _decide(items, lookups, person_context(doc), ctx.emit)
    decided: dict[str, Parsed] = {}
    allocated_total = 0
    for m in decided_by_agent.items:
        item = next((i for i in items if i.field == m.field), None)
        if item is None:
            continue
        if m.non_normalizable or m.value is None:
            decided[m.field] = Parsed(None, NON_NORMALIZABLE)
            continue
        if item.type not in ENUM_TYPES:
            decided[m.field] = parser.parse(m.value, item.type, None)
            continue
        proposed = m.value if isinstance(m.value, list) else [m.value]
        accepted: list[str] = []
        for value in proposed:
            may_allocate = opts.allocate and allocated_total < opts.max_new_values \
                and m.field not in result.allocated
            canon, allocated = await properties.resolve_enum_value(
                m.field, value, may_allocate,
                src=f"agent:{AGENT_NAME} {ctx.source.ref or ''}".strip())
            if canon is None:
                result.rejected.setdefault(m.field, []).append(value)
                ctx.emit("note", f"{m.field}: {value!r} not added (cap)", key=m.field)
                continue
            accepted.append(canon)
            if allocated:
                allocated_total += 1
                result.allocated.setdefault(m.field, []).append(canon)
                ctx.tx.record_catalog(Change(key=m.field, action="enum_value_added", value=canon),
                                      tag="normalize")
                ctx.emit("note", f"{m.field}: new value {canon!r}", key=m.field, value=canon)
            if item.type == "enum" and await properties.add_alias(m.field, canon, item.text):
                result.merged[m.field] = canon
        if accepted:
            decided[m.field] = Parsed(accepted if item.type == "enum_list" else accepted[0])
    result.unmapped = sorted(i.field for i in items if i.field not in decided)
    if decided:
        changes = await ctx.tx.set_norm(decided, ctx.source, tag="normalize")
        for c in changes:
            if c.action == "normalized":
                ctx.emit("write", f"{c.key} → {c.value}", key=c.key, value=c.value)
            elif c.action == "flagged":
                ctx.emit("write", f"{c.key}: {c.flag}", key=c.key, flag=c.flag)
