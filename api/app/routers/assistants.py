"""Assistant runs over HTTP (level 4 surface): list the assistants, start a run for chosen people
or everyone with a property, follow runs (paged items, Server-Sent Events), pause, resume with
other options, stop."""

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError
from sse_starlette.sse import EventSourceResponse

from app import fields, people
from app.assistants import REGISTRY, runner
from app.assistants import credits as credits_svc
from app.assistants.base import AssistantSpec, ItemSpec
from app.auth import Principal, require
from app.config import get_settings
from app.tools.search import FieldFilter, SearchRequest, mongo_query

router = APIRouter(prefix="/api/assistants", tags=["assistants"])

Reader = Annotated[Principal, Depends(require("people:read"))]
Writer = Annotated[Principal, Depends(require("people:write"))]

MAX_BULK = 5000           # ids or records in one request body
MAX_SELECT = 100_000      # people a selector (from_run, where) may resolve to
PING_SECONDS = 15


class RunRequest(BaseModel):
    """Who a run is for: exactly one of the selectors (`records` for merge)."""

    person_ids: list[str] | None = Field(None, max_length=MAX_BULK)
    from_run: int | None = Field(None, description="The people another run touched (its items "
                                                   "with a person: created, matched or processed), "
                                                   "e.g. the merge run of an upload")
    where: list[FieldFilter] | None = Field(None, max_length=20,
                                            description="Everyone matching these filters (ANDed; "
                                                        "`tier` narrows further)")
    all_with_field: str | None = Field(None, description="Everyone whose property is set")
    records: list[dict[str, Any]] | None = Field(None, max_length=MAX_BULK,
                                                 description="Incoming records for the merge "
                                                             "assistant (property key → value)")
    tier: fields.Tier | None = None
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
    chosen = [k for k in ("person_ids", "from_run", "where", "all_with_field")
              if getattr(body, k) not in (None, [], "")]
    if len(chosen) != 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "exactly one of person_ids, from_run, where or all_with_field is required")
    if body.person_ids:
        ids = list(dict.fromkeys(pid for pid in body.person_ids if pid))
    elif body.from_run is not None:
        if await runner.get_run(body.from_run) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {body.from_run} not found")
        items = await runner.get_items(body.from_run, limit=MAX_SELECT)
        ids = list(dict.fromkeys(it["person_id"] for it in items if it.get("person_id")))
    else:
        filters = body.where or [FieldFilter(field=body.all_with_field, op="exists", value=True)]
        req = SearchRequest(tier=body.tier, filters=filters, limit=1)
        ids = await people.find_ids(await mongo_query(req), limit=MAX_SELECT)
    if not ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no people selected")
    names = {str(d["_id"]): (d.get("name") or {}).get("v") for d in
             await people.fetch(ids, projection={"name": 1})}
    return [{"person_id": pid, "payload": {}, "label": names.get(pid) or pid} for pid in ids]


@router.post("/{key}/runs", status_code=201)
async def start_run(key: str, body: RunRequest, principal: Reader) -> dict:
    spec = REGISTRY.get(key)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown assistant")
    if not principal.can(spec.permission):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Missing permission: {spec.permission}")
    try:
        options = spec.options_model.model_validate(body.options)
        if spec.scope == "people":
            items = await _people_items(body)
        elif spec.prepare is not None:
            items = await spec.prepare(body.model_dump(), options)
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                f"{key} cannot be started with this request")
    except (ValidationError, ValueError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    label = body.label or (items[0].get("label") if len(items) == 1 else None)
    run = await runner.create_run(spec, principal, items, {
        "options": options.model_dump(mode="json"), "selector": body.all_with_field,
        "from_run": body.from_run, "where": [f.model_dump() for f in body.where or []] or None,
        "tier": body.tier}, label=label)
    await runner.start(run["id"], principal)
    return await runner.get_run(run["id"])


async def plan(spec: AssistantSpec, body: RunRequest) -> dict[str, Any]:
    """What a run over this selection would cost before it is started: how many people, how many
    an enrichment can work on (a LinkedIn URL or a site) and how many it would skip (enriched
    within the TTL), the recent cost of one item (credits, model tokens, seconds) and the
    estimates and recommended cap that follow, against the Firecrawl balance."""
    items = await _people_items(body)
    ids = [it["person_id"] for it in items if it.get("person_id")]
    averages = await runner.history_averages(spec.key)
    out: dict[str, Any] = {"assistant": spec.key, "people": len(ids), "averages": averages}
    eligible = len(ids)
    if spec.key == "enrich":
        docs = await people.fetch(ids, projection={"linkedin": 1, "website": 1, "github": 1})
        with_linkedin = sum(1 for d in docs if (d.get("linkedin") or {}).get("v"))
        with_site = sum(1 for d in docs if not (d.get("linkedin") or {}).get("v")
                        and ((d.get("website") or {}).get("v") or (d.get("github") or {}).get("v")))
        nothing = len(docs) - with_linkedin - with_site
        refresh = bool(body.options.get("refresh"))
        recent: set[str] = set()
        if not refresh:
            ttl_days = get_settings().enrich_ttl_days
            recent = await runner.recently_done("enrich", ids, ttl_days)
            # and the records themselves: entries this assistant wrote within the TTL (a database
            # copied from another stack keeps them while the run history stays behind)
            cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
            recent |= set(await people.find_ids({
                "_id": {"$in": [people._oid(i) for i in ids]},
                "enrichment_passage.h": {"$elemMatch": {"ts": {"$gte": cutoff}}}}))
        eligible = sum(1 for d in docs if str(d["_id"]) not in recent
                       and ((d.get("linkedin") or {}).get("v") or (d.get("website") or {}).get("v")
                            or (d.get("github") or {}).get("v")))
        out.update({"with_linkedin": with_linkedin, "with_site_only": with_site,
                    "nothing_to_work_with": nothing, "already_enriched": len(recent),
                    "eligible": eligible})
    per_item = averages["credits_per_item"] if averages["credits_per_item"] is not None else (
        4.0 if spec.key == "enrich" else 0.0)
    est_credits = round(eligible * per_item)
    est_tokens = eligible * (averages["tokens_per_item"] or 0)
    secs = averages["seconds_per_item"] or (20.0 if spec.key == "enrich" else 1.0)
    est_minutes = round(eligible * secs / max(1, get_settings().assistant_concurrency) / 60)
    balance = await credits_svc.summary() if spec.key == "enrich" else None
    available = None
    if balance and balance.get("remaining") is not None:
        available = int(balance["remaining"]) - int(balance.get("reserved") or 0)
    recommended = {}
    if spec.key == "enrich":
        cap = round(est_credits * 1.25) or 1
        if available is not None:
            cap = min(cap, max(1, available))
        recommended["max_credits"] = cap
    out.update({"estimate": {"credits": est_credits, "tokens": est_tokens, "minutes": est_minutes,
                             "concurrency": get_settings().assistant_concurrency,
                             "truncated": len(ids) >= MAX_SELECT},
                "balance": balance and {k: balance.get(k) for k in ("remaining", "reserved", "plan", "period_end")},
                "fits_balance": None if available is None else est_credits <= available,
                "recommended_options": recommended})
    return out


@router.post("/{key}/plan")
async def plan_run(key: str, body: RunRequest, principal: Reader) -> dict:
    """Estimate a run before starting it (same selectors as a start): people selected, what an
    enrichment could work on and would skip, the recent per-item cost, the estimated credits,
    tokens and minutes, the Firecrawl balance and a recommended credit cap. Writes nothing."""
    spec = REGISTRY.get(key)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown assistant")
    if spec.scope != "people":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{key} is not run over people")
    try:
        spec.options_model.model_validate(body.options)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    return await plan(spec, body)


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
    return {**run, "progress": await runner.progress(run), "items": await runner.get_items(run_id)}


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
