"""Agent: turn a passage about a person into writes against their properties.

It sees the passage, the person's current values and the whole property catalog, and proposes
one write per property (a value in the form the property's type expects, or the text as written
when unclear — the normaliser cleans that up), new properties only for recurring kinds of fact,
and says what it confirmed or ignored. `check` validates a proposal against the catalog and is
shared with the apply path, so a program-edited proposal meets the same bar."""

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import fields
from app.agents.common import catalog_prompt, run_agent
from app.properties import Property

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
- `ignored`: greetings, requests, and anything not about this person.
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


async def propose(passage: str, person: dict, catalog: list[Property]) -> Proposal:
    passage = passage.strip()[:MAX_PASSAGE_CHARS]
    prompt = (f"Person: {person.get('name') or 'unnamed'}\n\n"
              f"Current values:\n{render_person(person)}\n\n"
              f"Passage:\n{passage}\n\n"
              f"Properties:\n{catalog_prompt(catalog)}")
    deps = StorageDeps({p.key: p for p in catalog}, passage)
    return await run_agent(agent, prompt, deps, request_limit=REQUEST_LIMIT)
