"""Agent: decide the normalised value of texts the parser (app.normalize) could not map: a
canonical enum value, a number, a date or a boolean read from the text, or "no value here". The
output validator accepts only values the type admits."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import normalize
from app.agents.common import run_agent

AGENT_NAME = "normalizer"
REQUEST_LIMIT = 3


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
        parsed = normalize.parse(m.value, item.type, ctx.deps.lookups.get(m.field))
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


async def run(items: list[Item], lookups: dict[str, dict[str, str]]) -> Normalised:
    lines = []
    for it in items:
        values = f" values: {', '.join(it.values)}" if it.values else ""
        lines.append(f"- {it.field} ({it.type}{values}): {it.text!r}")
    prompt = "Items:\n" + "\n".join(lines)
    deps = NormalizerDeps({it.field: it for it in items}, lookups)
    return await run_agent(agent, prompt, deps, request_limit=REQUEST_LIMIT)
