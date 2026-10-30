"""Agent: answer a question from the people a search returned, citing where each claim comes
from (person and property). It sees only the documents it is given."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app.agents.common import run_agent

AGENT_NAME = "answer"
REQUEST_LIMIT = 3
MAX_CONTEXT_CHARS = 60_000


class Citation(BaseModel):
    person_id: str
    field: str = Field(description="The property the cited value comes from")
    value: str = Field(max_length=300, description="The value (or an excerpt) relied on")


class Answer(BaseModel):
    text: str = Field(description="The answer for a human reader: two to eight plain sentences "
                                  "(or a short list), naming people by name. Never JSON.")
    citations: list[Citation] = Field(default_factory=list, max_length=30,
                                      description="One per person named: their person_id, the "
                                                  "property relied on, and its value")


@dataclass
class AnswerDeps:
    ids: set[str]


agent = Agent(
    name=f"agents.{AGENT_NAME}",
    deps_type=AnswerDeps,
    output_type=Answer,
    instructions="""\
You answer a question about people from a database, using only the records given below the
question. Each record is one person: a header with name and id, then the properties the search
looked at, as `property: text` (with the normalised value in brackets where there is one) and the
similarity score when a passage was compared.

Write `text` as prose for a human reader: say who matches the question and why, naming people
(two to eight sentences, or a short list when several people match). If nobody fits, say so
plainly. Never restate the records, never answer in JSON, never add facts that are not in them.
For every person you name add a citation with their id and the property that supports it.""",
)


@agent.output_validator
async def _cited(ctx: RunContext[AnswerDeps], out: Answer) -> Answer:
    bad = [c.person_id for c in out.citations if c.person_id not in ctx.deps.ids]
    if bad:
        raise ModelRetry(f"Citations must use the person ids given; unknown: {bad}")
    return out


def render(context: dict[str, Any]) -> str:
    """One hit as the model reads it (see app.agents.ask._context for the keys)."""
    head = f"### {context.get('name') or 'unnamed'} (id {context['id']}"
    if context.get("score") is not None:
        head += f"; similarity {context['score']:.2f}"
    lines = [head + ")"]
    for key, value in context.items():
        if key in ("id", "name", "tier", "score", "field_scores") or key.endswith("_normalised"):
            continue
        norm = context.get(f"{key}_normalised")
        text = "; ".join(map(str, value)) if isinstance(value, list) else str(value)
        lines.append(f"{key}: {text}" + (f"  [{norm}]" if norm is not None else ""))
    return "\n".join(lines)


async def answer(question: str, contexts: list[dict[str, Any]]) -> Answer:
    body = "\n\n".join(render(c) for c in contexts)[:MAX_CONTEXT_CHARS]
    prompt = f"Question: {question}\n\nPeople:\n{body}"
    deps = AnswerDeps(ids={str(c.get("id")) for c in contexts})
    return await run_agent(agent, prompt, deps, request_limit=REQUEST_LIMIT)
