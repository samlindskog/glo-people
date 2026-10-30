"""Agent operations over HTTP: ask a question, store a passage about a person, normalise one
person. Tuning travels with each call (option models with defaults)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import AgentRunError

from app import activity
from app import people as people_svc
from app.agents import normalize_person, store
from app.agents.ask import AskOptions, AskResult, ask
from app.agents.normalize_person import NormalizeReport
from app.agents.store import StoreOptions, StoreReport
from app.auth import Principal, require
from app.history import Source
from app.search import FieldFilter

router = APIRouter(prefix="/api", tags=["agents"])

Reader = Annotated[Principal, Depends(require("people:read"))]
Writer = Annotated[Principal, Depends(require("people:write"))]

MAX_PASSAGE = 20_000


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    tier: Literal["member", "plus_one"] | None = None
    filters: list[FieldFilter] = Field(default_factory=list, max_length=10,
                                       description="Extra literal filters, ANDed with the plan")
    options: AskOptions = Field(default_factory=AskOptions)


class StoreBody(BaseModel):
    passage: str = Field(min_length=1, max_length=MAX_PASSAGE)
    options: StoreOptions = Field(default_factory=StoreOptions)


def _error(e: Exception) -> HTTPException:
    if isinstance(e, people_svc.ConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(e))
    if isinstance(e, AgentRunError):      # the model could not produce a usable output
        return HTTPException(status.HTTP_502_BAD_GATEWAY, f"agent failed: {e}")
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


def _source(principal: Principal, agent: str) -> Source:
    """The passage came from this person (UI or API key); the agent only restructured it."""
    base = Source.for_rest(principal)
    ref = f"{base.ref} agent:{agent}" if base.ref else f"agent:{agent}"
    return Source(base.kind, base.by, ref)


@router.post("/ask")
async def ask_people(body: AskBody, principal: Reader) -> AskResult:
    try:
        result = await ask(body.question, body.options, principal, body.tier, body.filters,
                           source="api" if principal.via_api_key else "ui")
    except (ValueError, AgentRunError) as e:
        raise _error(e) from e
    await activity.record("ask", principal, str(result.search_id),
                          {"question": body.question, "total": result.total,
                           "plan": result.plan.model_dump(mode="json", exclude_none=True)})
    return result


async def _store(person_id: str, passage: str, options: StoreOptions,
                 principal: Principal) -> StoreReport:
    try:
        report = await store.store(person_id, passage, options, principal,
                                   _source(principal, "store"))
    except (ValueError, AgentRunError) as e:
        raise _error(e) from e
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    await activity.record("person.store", principal, person_id, {
        "items": [{"tag": i.tag, "action": i.action, "key": i.key} for i in report.items],
        "created": report.created, "chars": len(passage)})
    return report


@router.post("/people/{person_id}/store")
async def store_passage(person_id: str, body: StoreBody, principal: Writer) -> StoreReport:
    return await _store(person_id, body.passage, body.options, principal)


@router.post("/people/{person_id}/store/file")
async def store_file(person_id: str, file: Annotated[UploadFile, File()],
                     principal: Writer) -> StoreReport:
    """A .txt or .md file as the passage, stored with the default options."""
    data = await file.read(MAX_PASSAGE + 1)
    if len(data) > MAX_PASSAGE:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file larger than 20 kB")
    try:
        passage = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "file is not UTF-8 text") from e
    if not passage.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "file is empty")
    return await _store(person_id, passage, StoreOptions(), principal)


@router.post("/people/{person_id}/normalize")
async def normalize_one(person_id: str, principal: Writer) -> NormalizeReport:
    try:
        report = await normalize_person.run(
            person_id, principal, Source("agent", "normalizer", f"user:{principal.username}"))
    except (ValueError, AgentRunError) as e:
        raise _error(e) from e
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    return report
