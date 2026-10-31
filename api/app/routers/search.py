"""Search over HTTP: the engine (POST /api/search) and agentic search (POST /api/search/ask).
Tuning travels with each call (option models with defaults)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import AgentRunError

from app import activity
from app.auth import Principal, require
from app.search import (
    FieldFilter,
    SearchRequest,
    SearchResponse,
    describe,
    record_search,
    run_search,
)
from app.search.ask import AskOptions, AskResult, ask

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


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    tier: Literal["member", "plus_one"] | None = None
    filters: list[FieldFilter] = Field(default_factory=list, max_length=10,
                                       description="Extra literal filters, ANDed with the plan")
    options: AskOptions = Field(default_factory=AskOptions)


def agent_error(e: Exception) -> HTTPException:
    """422 for a bad request, 502 when the model could not produce a usable output."""
    if isinstance(e, AgentRunError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, f"agent failed: {e}")
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


@router.post("/search/ask")
async def ask_people(body: AskBody, principal: Reader) -> AskResult:
    try:
        result = await ask(body.question, body.options, principal, body.tier, body.filters,
                           source="api" if principal.via_api_key else "ui")
    except (ValueError, AgentRunError) as e:
        raise agent_error(e) from e
    await activity.record("ask", principal, str(result.search_id),
                          {"question": body.question, "total": result.total,
                           "plan": result.plan.model_dump(mode="json", exclude_none=True)})
    return result

