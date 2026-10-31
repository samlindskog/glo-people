"""Tool (level 2): bring one person's normalised values up to date. The parser runs first (free,
through the people store); whatever it left `unmapped` goes to the normaliser agent in one call,
and the agent's decisions are written with the caller's source so the parser never redoes them.
Returns the changes (normalized / flagged) and what is still unmapped."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import fields, history, people, properties
from app import normalize as parser
from app.agents.common import run_agent
from app.auth import Principal
from app.history import Source
from app.normalize import NON_NORMALIZABLE, Parsed
from app.tools import ToolResult

AGENT_NAME = "normalizer"
REQUEST_LIMIT = 3


class NormalizeResult(ToolResult):
    agent_called: bool = False
    unmapped: list[str] = Field(default_factory=list,
                                description="Typed properties still without a value afterwards")


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
        None, description="The normalised value: a canonical value (or list of them), a number, "
                          "an ISO date or true/false; null when the text gives no value")
    non_normalizable: bool = Field(
        False, description="True when the text says there is no value or cannot be read as one "
                           "of this type (then value is null)")
    note: str = Field("", max_length=200, description="Why, in a few words")


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
types, its canonical values. Give the normalised value the text expresses: for enum types one of
the canonical values (for an enum list, every canonical value the text covers), for numbers a
plain number (ranges: the lower bound; "pre-revenue": 0), for dates an ISO date, for booleans
true/false. When the text clearly says there is no value, or is about something else entirely,
mark it non_normalizable. Never guess a canonical value that the text does not support, and never
invent a value not in the list; one answer per item, in the same order.""",
)


@agent.output_validator
async def _admissible(ctx: RunContext[NormalizerDeps], out: Normalised) -> Normalised:
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
        parsed = parser.parse(m.value, item.type, ctx.deps.lookups.get(m.field))
        if parsed.value is None:
            hint = f"; canonical values: {item.values}" if item.values else ""
            problems.append(f"{m.field}: {m.value!r} is not a valid {item.type} value{hint}")
            continue
        fixed.append(Mapping(field=m.field, value=_jsonable(parsed.value), note=m.note))
    if problems:
        raise ModelRetry("; ".join(problems))
    return Normalised(items=fixed)


def _jsonable(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


async def _decide(items: list[Item], lookups: dict[str, dict[str, str]]) -> Normalised:
    lines = []
    for it in items:
        values = f" values: {', '.join(it.values)}" if it.values else ""
        lines.append(f"- {it.field} ({it.type}{values}): {it.text!r}")
    prompt = "Items:\n" + "\n".join(lines)
    deps = NormalizerDeps({it.field: it for it in items}, lookups)
    return await run_agent(agent, prompt, deps, request_limit=REQUEST_LIMIT)


# --- the tool -----------------------------------------------------------------------------------------

async def normalize(person_id: str, principal: Principal | None,
                    source: Source) -> NormalizeResult | None:
    """Parser first, then the agent for what stayed unmapped. None if the person does not exist."""
    docs = await people.fetch([person_id])
    if not docs:
        return None
    doc = docs[0]
    result = NormalizeResult(person_id=person_id)
    parsed = await people.pending_normalization(doc)
    if parsed:
        write = await people.update_norm(principal, person_id, parsed, people.NORMALIZE_SRC)
        if write is not None:
            result.changes += write.changes
        doc = (await people.fetch([person_id]))[0]

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
        return result

    result.agent_called = True
    decided_by_agent = await _decide(items, lookups)
    decided: dict[str, Parsed] = {}
    for m in decided_by_agent.items:
        item = next((i for i in items if i.field == m.field), None)
        if item is None:
            continue
        if m.non_normalizable or m.value is None:
            decided[m.field] = Parsed(None, NON_NORMALIZABLE)
        else:
            decided[m.field] = parser.parse(m.value, item.type, lookups.get(m.field))
    result.unmapped = sorted(i.field for i in items if i.field not in decided)
    if decided:
        write = await people.update_norm(principal, person_id, decided, source)
        if write is not None:
            result.changes += write.changes
    return result
