"""Assistant runs over HTTP (level 4 surface): list the assistants, start a run for chosen people
or everyone with a property, follow runs (paged items, Server-Sent Events), pause, resume with
other options, stop."""

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError
from sse_starlette.sse import EventSourceResponse

from app import people
from app.assistants import REGISTRY, runner
from app.assistants.base import ItemSpec
from app.auth import Principal, require
from app.tools.search import FieldFilter, SearchRequest, mongo_query

router = APIRouter(prefix="/api/assistants", tags=["assistants"])

Reader = Annotated[Principal, Depends(require("people:read"))]
Writer = Annotated[Principal, Depends(require("people:write"))]

MAX_BULK = 5000
PING_SECONDS = 15


class RunRequest(BaseModel):
    person_ids: list[str] | None = Field(None, max_length=MAX_BULK)
    all_with_field: str | None = Field(None, description="Run for everyone whose field is set")
    tier: Literal["member", "plus_one"] | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    label: str | None = Field(None, max_length=200)


class ResumeRequest(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict,
                                    description="Options to change, e.g. a higher max_credits")


@router.get("")
async def list_assistants(_: Reader) -> list[dict]:
    """The assistants that can be run as a run."""
    return [spec.public() for spec in REGISTRY.values()]


async def _people_items(body: RunRequest) -> list[ItemSpec]:
    if body.person_ids:
        ids = list(dict.fromkeys(body.person_ids))
    elif body.all_with_field:
        req = SearchRequest(tier=body.tier, filters=[
            FieldFilter(field=body.all_with_field, op="exists", value=True)], limit=1)
        ids = await people.find_ids(await mongo_query(req), limit=MAX_BULK)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "person_ids or all_with_field is required")
    if not ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no people selected")
    names = {str(d["_id"]): (d.get("name") or {}).get("v") for d in
             await people.fetch(ids, projection={"name": 1})}
    return [{"person_id": pid, "payload": {}, "label": names.get(pid) or pid} for pid in ids]


@router.post("/{key}/runs", status_code=201)
async def start_run(key: str, body: RunRequest, principal: Writer) -> dict:
    spec = REGISTRY.get(key)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown assistant")
    try:
        options = spec.options_model.model_validate(body.options)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    if spec.scope == "people":
        items = await _people_items(body)
    elif spec.prepare is not None:
        items = await spec.prepare(body.model_dump(), options)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"{key} cannot be started with this request")
    run = await runner.create_run(spec, principal, items, {
        "options": options.model_dump(), "selector": body.all_with_field, "tier": body.tier},
        label=body.label)
    await runner.start(run["id"], principal)
    return await runner.get_run(run["id"])


@router.get("/runs")
async def list_runs(_: Reader, limit: Annotated[int, Query(ge=1, le=100)] = 25,
                    offset: Annotated[int, Query(ge=0)] = 0,
                    status_: Annotated[str | None, Query(alias="status",
                                                         description="active, or one status")] = None
                    ) -> dict:
    return await runner.list_runs(limit, offset, status_)


@router.get("/runs/{run_id}")
async def get_run(run_id: int, _: Reader) -> dict:
    run = await runner.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return {**run, "items": await runner.get_items(run_id)}


@router.get("/runs/{run_id}/items")
async def run_items(run_id: int, _: Reader, item_status: Annotated[str | None, Query(alias="status")] = None,
                    after: Annotated[int, Query(ge=0)] = 0,
                    limit: Annotated[int, Query(ge=1, le=500)] = 200) -> list[dict]:
    if await runner.get_run(run_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return await runner.get_items(run_id, status=item_status, after=after, limit=limit)


@router.get("/runs/{run_id}/events")
async def run_events(run_id: int, _: Reader,
                     last_event_id: Annotated[str | None, Header()] = None):
    """The run's activity as Server-Sent Events: every event so far (after `Last-Event-ID`), then
    live ones, with the run's state in between, until it has finished."""
    if await runner.get_run(run_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    async def stream():
        async for msg in runner.subscribe(run_id, after):
            if msg.get("type") == "run":
                yield {"event": "run", "data": _json(msg)}
            else:
                yield {"event": msg["kind"], "id": str(msg["id"]), "data": _json(msg)}

    return EventSourceResponse(stream(), ping=PING_SECONDS, headers={"X-Accel-Buffering": "no"})


def _json(msg: dict) -> str:
    return json.dumps(msg, default=str)


@router.post("/runs/{run_id}/pause")
async def pause_run(run_id: int, principal: Writer) -> dict:
    if not await runner.pause(run_id, principal):
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not running")
    return await runner.get_run(run_id)


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: int, principal: Writer, body: ResumeRequest | None = None) -> dict:
    try:
        ok = await runner.resume(run_id, (body or ResumeRequest()).options, principal)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not paused")
    return await runner.get_run(run_id)


@router.post("/runs/{run_id}/stop")
@router.post("/runs/{run_id}/cancel")
async def stop_run(run_id: int, principal: Writer) -> dict:
    """Hard stop: in-flight tool invocations are stopped (nothing written), the rest skipped."""
    if not await runner.stop(run_id, principal):
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not running")
    return await runner.get_run(run_id)
