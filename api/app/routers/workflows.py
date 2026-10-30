from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError

from app import people
from app.auth import Principal, require
from app.search import FieldFilter, SearchRequest, mongo_query
from app.workflows import REGISTRY, runner

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

Reader = Annotated[Principal, Depends(require("people:read"))]
Writer = Annotated[Principal, Depends(require("people:write"))]

MAX_BULK = 5000


class RunRequest(BaseModel):
    person_ids: list[str] | None = Field(None, max_length=MAX_BULK)
    all_with_field: str | None = Field(None, description="Run for everyone whose field is set")
    tier: Literal["member", "plus_one"] | None = None
    options: dict[str, Any] = Field(default_factory=dict)


@router.get("")
async def list_workflows(_: Reader) -> list[dict]:
    return [spec.public() for spec in REGISTRY.values()]


@router.post("/{key}/runs", status_code=201)
async def start_run(key: str, body: RunRequest, principal: Writer) -> dict:
    spec = REGISTRY.get(key)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown workflow")
    try:
        options = spec.options_model.model_validate(body.options)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    if body.person_ids:
        ids = list(dict.fromkeys(body.person_ids))
    elif body.all_with_field:
        req = SearchRequest(tier=body.tier, filters=[
            FieldFilter(field=body.all_with_field, op="exists", value=True)], limit=1)
        query = await mongo_query(req)
        ids = await people.find_ids(query, limit=MAX_BULK)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "person_ids or all_with_field is required")
    if not ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no people selected")
    run = await runner.create_run(spec, principal, ids, {
        "options": options.model_dump(), "selector": body.all_with_field, "tier": body.tier})
    runner.start(run["id"], principal)
    return run


@router.get("/runs")
async def list_runs(_: Reader, limit: Annotated[int, Query(ge=1, le=100)] = 25,
                    offset: Annotated[int, Query(ge=0)] = 0) -> dict:
    return await runner.list_runs(limit, offset)


@router.get("/runs/{run_id}")
async def get_run(run_id: int, _: Reader) -> dict:
    run = await runner.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return {**run, "items": await runner.get_items(run_id)}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int, _: Writer) -> dict:
    if not await runner.cancel(run_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not running")
    return await runner.get_run(run_id)
