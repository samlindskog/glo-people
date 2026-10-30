"""Agent: break a passage about a person into tagged statements, each with the existing property
it belongs to when one fits (the catalog is shown). The storage operation decides where each
statement lands (app.agents.store)."""

from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import fields
from app.agents.common import catalog_prompt, run_agent
from app.properties import Property

AGENT_NAME = "extract"
REQUEST_LIMIT = 3
MAX_PASSAGE_CHARS = 20_000


class Item(BaseModel):
    tag: str = Field(min_length=1, max_length=60,
                     description="Two to four plain words naming what the statement is about, "
                                 "as a property name would (e.g. 'current company', 'hiring "
                                 "needs', 'favourite tool')")
    text: str = Field(min_length=1, max_length=1000,
                      description="The statement itself, neutral and self-contained; keep the "
                                  "passage's own words and figures")
    existing_key: str | None = Field(
        None, description="The key of an existing property this statement is a value of, when "
                          "one clearly fits; otherwise null")


class Extraction(BaseModel):
    items: list[Item] = Field(default_factory=list, max_length=8)


@dataclass
class ExtractDeps:
    keys: set[str]


agent = Agent(
    name=f"agents.{AGENT_NAME}",
    deps_type=ExtractDeps,
    output_type=Extraction,
    instructions="""\
You are given a passage about one person (notes from a call, a message, a profile) and the
catalog of properties a person record can have. Split the passage into the distinct things it
says about the person: between one and five statements, each about one topic. For each statement
give a short tag (two to four words, like a property name), the statement in neutral wording with
the passage's own facts and figures, and, when an existing property clearly holds this kind of
information, that property's key (for example revenue figures belong to mrr, the employer to
company, what they look for to needs_stated, a short description to bio). Leave existing_key null
when nothing fits; do not force a match. Skip greetings and anything not about the person.""",
)


@agent.output_validator
async def _known_keys(ctx: RunContext[ExtractDeps], out: Extraction) -> Extraction:
    problems = []
    for it in out.items:
        if it.existing_key is not None and it.existing_key not in ctx.deps.keys:
            problems.append(f"{it.existing_key!r} is not a property; use null or a listed key")
        if not fields.fold(it.tag):
            problems.append(f"tag {it.tag!r} has no words")
    if problems:
        raise ModelRetry("; ".join(problems))
    return out


async def extract(passage: str, about: str, catalog: list[Property]) -> Extraction:
    prompt = (f"Person: {about}\n\nPassage:\n{passage.strip()[:MAX_PASSAGE_CHARS]}\n\n"
              f"Properties:\n{catalog_prompt(catalog)}")
    return await run_agent(agent, prompt, ExtractDeps({p.key for p in catalog}),
                           request_limit=REQUEST_LIMIT)
