"""Agentic search: answer a question about people. The planner picks what to search, the engine
runs it, the answerer reads the hits. Tuning is per call (`AskOptions`); the executed search is
saved like any other so it can be re-run from the history."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app import people, properties
from app.agents import answer as answer_agent
from app.agents import planner
from app.agents.answer import Citation
from app.agents.planner import SearchPlan
from app.auth import Principal
from app.search import engine
from app.search.engine import FieldFilter, SearchHit, SearchRequest, SemanticQuery

MAX_VALUE_CHARS = 600
_sem_defaults = SemanticQuery.model_fields


class AskOptions(BaseModel):
    """How strictly the planned search is run; the planner decides what to search."""

    k: int = Field(12, ge=1, le=50, description="People handed to the answerer")
    mode: Literal["avg", "max"] = Field(_sem_defaults["mode"].default,
                                        description="How a person's property scores combine")
    threshold: float = Field(_sem_defaults["threshold"].default, ge=-1, le=1,
                             description="Minimum aggregate similarity for a semantic hit")


class AskResult(BaseModel):
    answer: str
    citations: list[Citation]
    plan: SearchPlan
    request: SearchRequest                  # what was executed (re-runnable)
    total: int
    results: list[SearchHit]
    search_id: int | None = None


def _context(hit: SearchHit, keys: list[str]) -> dict[str, Any]:
    """What the answerer sees of one hit: identity, scores, and the planned properties' text
    and normalised values."""
    person = hit.person
    out: dict[str, Any] = {"id": hit.id, "name": person.get("name"), "tier": hit.tier}
    if hit.score is not None:
        out["score"] = hit.score
        out["field_scores"] = hit.field_scores
    norm = person.get("norm") or {}
    for key in keys:
        if key in person:
            value = person[key]
            text = str(value)
            out[key] = text[:MAX_VALUE_CHARS] if isinstance(value, str) else value
        if key in norm:
            out[f"{key}_normalised"] = norm[key]
    return out


async def ask(question: str, opts: AskOptions, principal: Principal,
              tier: str | None = None, filters: list[FieldFilter] = (),
              source: str = "ui") -> AskResult:
    catalog = await properties.catalog(await people.coverage())
    plan = await planner.plan(question, tier, catalog)
    semantic = SemanticQuery(queries=plan.semantic, mode=opts.mode,
                             threshold=opts.threshold) if plan.semantic else None
    req = SearchRequest(tier=tier, filters=[*filters, *plan.filters], semantic=semantic,
                        limit=opts.k)
    resp = await engine.run_search(req)
    keys = list(dict.fromkeys(["company", "role",
                               *(q.field for q in plan.semantic),
                               *(f.field for f in plan.filters)]))
    contexts = [_context(h, keys) for h in resp.results]
    if contexts:
        ans = await answer_agent.answer(question, contexts)
    else:
        ans = answer_agent.Answer(text="No one in the database matches this search.",
                                  citations=[])
    search_id = await engine.record_search(principal, req, resp, source, extra={"ask": question})
    return AskResult(answer=ans.text, citations=ans.citations, plan=plan, request=req,
                     total=resp.total, results=resp.results, search_id=search_id)
