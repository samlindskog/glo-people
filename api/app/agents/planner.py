"""Agent: turn a question about people into a search plan over the property catalog.

The plan is the agent-authored subset of `search.SearchRequest`: a passage per embeddable
property to compare by meaning, and literal filters on typed or text properties. The output
validator makes every plan executable (known properties, operators allowed for the type,
canonical enum values) by sending problems back to the model."""

from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import properties
from app.agents.common import catalog_prompt, run_agent
from app.properties import Property
from app.search.engine import FieldFilter, FieldQuery, filter_to_mongo

AGENT_NAME = "planner"
REQUEST_LIMIT = 3


class SearchPlan(BaseModel):
    """How to find the people a question is about."""

    semantic: list[FieldQuery] = Field(
        default_factory=list, max_length=6,
        description="Passages compared by meaning, each with the embeddable property it should "
                    "be compared with (bio, one_liner, company_description, notes, sectors…). "
                    "Write the passage as the text you expect to find, not as a question.")
    filters: list[FieldFilter] = Field(
        default_factory=list, max_length=6,
        description="Literal conditions on typed or text properties (AND). Use canonical enum "
                    "values, numbers for number properties, ISO dates for dates.")
    rationale: str = Field("", max_length=300, description="One sentence on the choice")


@dataclass
class PlannerDeps:
    catalog: dict[str, Property]
    lookups: dict[str, dict[str, str]] = field(default_factory=dict)


agent = Agent(
    name=f"agents.{AGENT_NAME}",
    deps_type=PlannerDeps,
    output_type=SearchPlan,
    instructions="""\
You plan a search over a database of people (founders, investors, operators). You are given a
question and the catalog of properties each person may have, with how many people have each.
Decide which properties to search and how:

- First look for typed properties that encode what the question asks for: a role or seniority
  (seniority, role), a place (region, location), a stage, a sector or community, an amount
  (mrr, raised_funds), a date. When one does, write a literal filter on it: enum filters use one
  of the canonical values listed; number filters take numbers (10000, not "10k"); dates are ISO
  (YYYY-MM-DD); keyword/url/contact properties support contains / starts_with / regex / eq / ne
  / in / exists. Prefer the property most people have.
- Add a passage only for the part of the question that no typed property covers (a topic, a
  product, a need, a background): a few words to one sentence phrased like the text you expect
  to find, on the one or two embeddable properties most likely to hold it (bio and one_liner
  describe the person, company_description the company, notes are internal remarks, sectors
  are tags). A passage is compared by meaning and drops everyone below a similarity cut-off, so
  do not add one when a filter already answers the question ("name three founders" is a
  seniority filter, not a passage).
- Never invent properties or values. A request for N people is not a filter. Give at least one
  filter or passage; keep the plan small.""",
)


@agent.output_validator
async def _executable(ctx: RunContext[PlannerDeps], plan: SearchPlan) -> SearchPlan:
    cat = ctx.deps.catalog
    problems: list[str] = []
    seen: set[str] = set()
    for q in plan.semantic:
        prop = cat.get(q.field)
        if prop is None:
            problems.append(f"{q.field!r} is not a property")
        elif not prop.embeddable:
            problems.append(f"{q.field!r} is {prop.type}, not embeddable: use a filter on it")
        elif q.field in seen:
            problems.append(f"{q.field!r} has two passages; merge them")
        seen.add(q.field)
    for f in plan.filters:
        prop = cat.get(f.field)
        if prop is None:
            problems.append(f"{f.field!r} is not a property")
            continue
        try:
            filter_to_mongo(f, prop, ctx.deps.lookups.get(f.field))
        except ValueError as e:
            problems.append(str(e))
    if not plan.semantic and not plan.filters:
        problems.append("the plan is empty: give at least one passage or one filter")
    if problems:
        raise ModelRetry("Fix the plan: " + "; ".join(problems))
    return plan


async def plan(question: str, tier: str | None, catalog: list[Property]) -> SearchPlan:
    cat = {p.key: p for p in catalog}
    lookups = {k: await properties.enum_lookup(k) for k, p in cat.items()
               if p.type in ("enum", "enum_list")}
    prompt = (f"Question: {question}\nTier: {tier or 'any'}\n\n"
              f"Properties:\n{catalog_prompt(catalog)}")
    return await run_agent(agent, prompt, PlannerDeps(cat, lookups), request_limit=REQUEST_LIMIT)
