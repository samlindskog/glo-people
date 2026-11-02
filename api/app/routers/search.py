"""Search over HTTP (level 4 surface). Tools: POST /api/search runs a request, POST
/api/search/plan turns a question into a request. Assistant: POST /api/search/ask answers a
question in text. Tuning travels with each call (option models with defaults)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import AgentRunError

from app import activity, fields
from app.assistants.ask import AskResult, ask
from app.auth import Principal, require
from app.tools.plan import PlanResult, Tuning, plan_search
from app.tools.search import (
    FieldFilter,
    SearchRequest,
    SearchResponse,
    describe,
    record_search,
    run_search,
)

router = APIRouter(prefix="/api", tags=["search"])

Reader = Annotated[Principal, Depends(require("people:read"))]


@router.post("/search")
async def search(req: SearchRequest, principal: Reader) -> SearchResponse:
    try:
        resp = await run_search(req)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    resp.search_id = await record_search(principal, req, resp,
                                         "api" if principal.via_api_key else "ui")
    await activity.record("search", principal, str(resp.search_id),
                          {**describe(req), "total": resp.total})
    return resp


class QuestionBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    tier: fields.Tier | None = None
    filters: list[FieldFilter] = Field(default_factory=list, max_length=10,
                                       description="Extra literal filters, ANDed with the plan")
    options: Tuning = Field(default_factory=Tuning)


def agent_error(e: Exception) -> HTTPException:
    """422 for a bad request, 502 when the model could not produce a usable output."""
    if isinstance(e, AgentRunError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, f"agent failed: {e}")
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


@router.post("/search/plan")
async def plan(body: QuestionBody, principal: Reader) -> PlanResult:
    """Tool: the executable search for a question (run it with POST /api/search)."""
    try:
        return await plan_search(body.question, body.tier, body.filters, body.options)
    except (ValueError, AgentRunError) as e:
        raise agent_error(e) from e


@router.post("/search/ask")
async def ask_people(body: QuestionBody, principal: Reader) -> AskResult:
    """Assistant: plan, search and answer the question in text, with citations."""
    try:
        result = await ask(body.question, body.options, principal, body.tier, body.filters,
                           source="api" if principal.via_api_key else "ui")
    except (ValueError, AgentRunError) as e:
        raise agent_error(e) from e
    await activity.record("ask", principal, str(result.search_id),
                          {"question": body.question, "total": result.total,
                           **describe(result.request)})
    return result

