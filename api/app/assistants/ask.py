"""Assistant (level 3): answer a question about people. The plan tool turns the question into a
search, the search tool runs it, and the answer agent (defined here) writes the answer from the
hits with citations. Nothing is written; the executed search is saved like any other so it can be
re-run from the history. Tuning is per call."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

from app import fields
from app.agents.common import run_agent
from app.assistants import AssistantResult
from app.auth import Principal
from app.tools import plan as plan_tool
from app.tools import search as search_tool
from app.tools.plan import Tuning
from app.tools.search import FieldFilter, SearchHit, SearchRequest

AGENT_NAME = "answer"
REQUEST_LIMIT = 3
MAX_CONTEXT_CHARS = 200_000   # all hits together; one value may be fields.MAX_TEXT_CHARS


class Citation(BaseModel):
    person_id: str
    field: str = Field(description="The property the cited value comes from")
    value: str = Field(description="The value (or an excerpt) relied on")


class AskResult(AssistantResult):
    citations: list[Citation] = Field(default_factory=list)
    request: SearchRequest = Field(description="The search that was run; re-runnable as is")
    rationale: str = ""
    total: int = 0
    results: list[SearchHit] = Field(default_factory=list)
    search_id: int | None = None


# --- the answer agent -----------------------------------------------------------------------------------

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


def _context(hit: SearchHit, keys: list[str]) -> dict[str, Any]:
    """What the answerer sees of one hit: identity, score, and the searched properties' text and
    normalised values."""
    person = hit.person
    out: dict[str, Any] = {"id": hit.id, "name": person.get("name"), "tier": hit.tier}
    if hit.score is not None:
        out["score"] = hit.score
    norm = person.get("norm") or {}
    for key in keys:
        if key in person:
            value = person[key]
            out[key] = value[:fields.MAX_TEXT_CHARS] if isinstance(value, str) else value
        if key in norm:
            out[f"{key}_normalised"] = norm[key]
    return out


def render(context: dict[str, Any]) -> str:
    """One hit as the model reads it."""
    head = f"### {context.get('name') or 'unnamed'} (id {context['id']}"
    if context.get("score") is not None:
        head += f"; similarity {context['score']:.2f}"
    lines = [head + ")"]
    for key, value in context.items():
        if key in ("id", "name", "tier", "score") or key.endswith("_normalised"):
            continue
        norm = context.get(f"{key}_normalised")
        text = "; ".join(map(str, value)) if isinstance(value, list) else str(value)
        lines.append(f"{key}: {text}" + (f"  [{norm}]" if norm is not None else ""))
    return "\n".join(lines)


async def _answer(question: str, contexts: list[dict[str, Any]]) -> Answer:
    body = "\n\n".join(render(c) for c in contexts)[:MAX_CONTEXT_CHARS]
    prompt = f"Question: {question}\n\nPeople:\n{body}"
    deps = AnswerDeps(ids={str(c.get("id")) for c in contexts})
    return await run_agent(agent, prompt, deps, request_limit=REQUEST_LIMIT)


# --- the assistant --------------------------------------------------------------------------------------

async def ask(question: str, tuning: Tuning, principal: Principal, tier: str | None = None,
              filters: list[FieldFilter] = (), source: str = "ui") -> AskResult:
    planned = await plan_tool.plan_search(question, tier, filters, tuning)
    req = planned.request
    resp = await search_tool.run_search(req)
    keys = list(dict.fromkeys(["company", "role",
                               *(q.field for q in (req.semantic.queries if req.semantic else [])),
                               *(f.field for f in req.filters)]))
    contexts = [_context(h, keys) for h in resp.results]
    if contexts:
        ans = await _answer(question, contexts)
    else:
        ans = Answer(text="No one in the database matches this search.")
    search_id = await search_tool.record_search(principal, req, resp, source,
                                                extra={"ask": question})
    return AskResult(text=ans.text, citations=ans.citations, request=req,
                     rationale=planned.rationale, total=resp.total, results=resp.results,
                     search_id=search_id)
