"""Tool (level 2): plan a search from a question. The planner agent reads the property catalog and
turns a question into literal filters on typed properties and passages for embeddable ones; the
output validator keeps every plan executable (known properties, operators allowed for the type,
canonical enum values). The result is an ordinary `SearchRequest` a program runs with
`tools.search.run_search` — asking for N people is a limit, not a filter."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import people, properties
from app.agents.common import catalog_prompt, run_agent
from app.properties import Property
from app.tools import Emit, silent
from app.tools.search import (
    EVERY,
    FieldFilter,
    FieldQuery,
    SearchRequest,
    SemanticQuery,
    filter_to_mongo,
)

AGENT_NAME = "planner"
REQUEST_LIMIT = 4
COVERAGE_MAX_AGE_S = 60.0     # the counts in the planner's catalog may be this old
OUTPUT_RETRIES = 2        # validation problems are sent back up to twice
_sem_defaults = SemanticQuery.model_fields


class Tuning(BaseModel):
    """How strictly a planned search is run; the planner decides what to search."""

    k: int = Field(12, ge=1, le=50, description="People to return")
    mode: Literal["avg", "max"] = Field(_sem_defaults["mode"].default,
                                        description="How a person's property scores combine")
    threshold: float = Field(_sem_defaults["threshold"].default, ge=-1, le=1,
                             description="Minimum aggregate similarity for a semantic hit")


class SearchPlan(BaseModel):
    """The agent's output: the agent-authored part of a SearchRequest."""

    semantic: list[FieldQuery] = Field(
        default_factory=list, max_length=6,
        description="Passages compared by meaning, each with the embeddable property it should "
                    "be compared with (bio, one_liner, company_description, role, company, "
                    "enrichment_passage…) or '*' for every embeddable property. Write the "
                    "passage as the text you expect to find, not as a question.")
    filters: list[FieldFilter] = Field(
        default_factory=list, max_length=6,
        description="Literal conditions on typed or text properties (AND). Use canonical enum "
                    "values, numbers for number properties, ISO dates for dates.")
    rationale: str = Field("", description="One sentence on the choice")


class PlanResult(BaseModel):
    request: SearchRequest = Field(description="Executable with tools.search.run_search")
    rationale: str = ""


@dataclass
class PlannerDeps:
    catalog: dict[str, Property]
    lookups: dict[str, dict[str, str]] = field(default_factory=dict)


agent = Agent(
    name=f"agents.{AGENT_NAME}",
    deps_type=PlannerDeps,
    output_type=SearchPlan,
    retries=OUTPUT_RETRIES,
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
  are tags). Short values are compared by meaning too: role for what someone does ("head of
  partnerships" finds "BD lead"), company, industry, location — these are the properties most
  people have. enrichment_passage is the whole provider profile in one text: use it for a
  profile-wide description (background, employers, schools, skills together). The field "*"
  compares one passage with every embeddable property; pair it with mode max. A passage is
  compared by meaning and drops everyone below a similarity cut-off, so do not add one when a
  filter already answers the question ("name three founders" is a seniority filter, not a
  passage).
- Never invent properties or values. A request for N people is not a filter. Give at least one
  filter or passage; keep the plan small.""",
)


@agent.output_validator
async def _executable(ctx: RunContext[PlannerDeps], plan: SearchPlan) -> SearchPlan:
    cat = ctx.deps.catalog
    problems: list[str] = []
    seen: set[str] = set()
    for q in plan.semantic:
        if q.field == EVERY:
            if q.field in seen:
                problems.append("'*' appears twice; merge the passages")
            seen.add(q.field)
            continue
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


async def plan_search(question: str, tier: str | None = None,
                      filters: list[FieldFilter] = (), tuning: Tuning | None = None,
                      emit: Emit = silent) -> PlanResult:
    """The executable search for a question: the agent's plan folded into a `SearchRequest`
    together with the caller's extra filters and tuning."""
    tuning = tuning or Tuning()
    catalog = await properties.catalog(await people.coverage(max_age_s=COVERAGE_MAX_AGE_S))
    cat = {p.key: p for p in catalog}
    lookups = {k: await properties.enum_lookup(k) for k, p in cat.items()
               if p.type in ("enum", "enum_list")}
    prompt = (f"Question: {question}\nTier: {tier or 'any'}\n\n"
              f"Properties:\n{catalog_prompt(catalog)}")
    plan: SearchPlan = await run_agent(agent, prompt, PlannerDeps(cat, lookups),
                                       request_limit=REQUEST_LIMIT, on_event=emit)
    semantic = SemanticQuery(queries=plan.semantic, mode=tuning.mode,
                             threshold=tuning.threshold) if plan.semantic else None
    request = SearchRequest(tier=tier, filters=[*filters, *plan.filters], semantic=semantic,
                            limit=tuning.k)
    return PlanResult(request=request, rationale=plan.rationale)
