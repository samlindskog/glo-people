from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import AgentRunError
from sqlalchemy import select

from app import activity, db, embeddings, importer, jobs
from app import people as people_svc
from app.agents.storage import Proposal
from app.auth import Principal, require
from app.history import Source
from app.normalize import person as normalize_person
from app.normalize.person import NormalizeReport
from app.routers.search import agent_error
from app.search import store
from app.search.store import StoreOptions, StoreReport

router = APIRouter(prefix="/api", tags=["people"])

Reader = Annotated[Principal, Depends(require("people:read"))]
Writer = Annotated[Principal, Depends(require("people:write"))]

MAX_UPLOAD = 25 * 1024 * 1024
MAX_PASSAGE = 20_000


class PersonCreate(BaseModel):
    tier: Literal["member", "plus_one"]
    fields: dict[str, Any]


class PersonPatch(BaseModel):
    tier: Literal["member", "plus_one"] | None = None
    fields: dict[str, Any] = {}


def _bad_request(e: Exception) -> HTTPException:
    if isinstance(e, people_svc.ConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(e))
    if isinstance(e, AgentRunError):
        return agent_error(e)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


# --- people -------------------------------------------------------------------------------------

@router.get("/people/{person_id}")
async def get_person(person_id: str, _: Reader) -> dict:
    try:
        person = await people_svc.get(person_id)
    except people_svc.PersonError as e:
        raise _bad_request(e) from e
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    return person


@router.post("/people", status_code=201)
async def create_person(body: PersonCreate, principal: Writer) -> dict:
    try:
        return await people_svc.create(principal, body.tier, body.fields)
    except people_svc.PersonError as e:
        raise _bad_request(e) from e


@router.patch("/people/{person_id}")
async def patch_person(person_id: str, body: PersonPatch, principal: Writer) -> dict:
    try:
        person = await people_svc.update(principal, person_id, body.fields, body.tier)
    except people_svc.PersonError as e:
        raise _bad_request(e) from e
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    return person


@router.delete("/people/{person_id}", status_code=204)
async def delete_person(person_id: str, principal: Writer) -> None:
    try:
        deleted = await people_svc.delete(principal, person_id)
    except people_svc.PersonError as e:
        raise _bad_request(e) from e
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")


async def _read_upload(file: UploadFile) -> str:
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "CSV larger than 25 MB")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


@router.post("/people/import/preview")
async def import_preview(file: Annotated[UploadFile, File()], _: Writer) -> dict:
    return await importer.preview((await _read_upload(file)).lstrip("﻿"))


@router.post("/people/import")
async def import_people(file: Annotated[UploadFile, File()],
                        tier: Annotated[Literal["member", "plus_one"], Form()],
                        principal: Writer) -> dict:
    text = await _read_upload(file)
    try:
        result = await importer.import_csv(text, file.filename or "upload.csv", tier)
    except ValueError as e:                      # includes people.ConflictError
        raise _bad_request(e) from e
    summary = {"file": file.filename, "tier": tier, "rows": result.rows,
               "inserted": result.inserted, "updated": result.updated,
               "skipped": result.skipped, "new_properties": result.new_properties}
    await activity.record("people.import", principal, file.filename, summary)

    async def embed(job: dict) -> int:
        n = 0
        for start in range(0, len(result.ids), 500):
            n += await embeddings.sync_people(result.ids[start:start + 500])
            job["progress"] = min(start + 500, len(result.ids))
        return n

    job = jobs.spawn(f"embed import {file.filename}", embed)
    return {**summary, "job_id": job["id"]}


# --- agentic storage and normalisation of one person ------------------------------------------

class StoreBody(BaseModel):
    passage: str = Field(min_length=1, max_length=MAX_PASSAGE)
    options: StoreOptions = Field(default_factory=StoreOptions)


class ApplyBody(BaseModel):
    proposal: Proposal
    options: StoreOptions = Field(default_factory=StoreOptions)


def _source(principal: Principal, agent: str) -> Source:
    """The passage came from this person (UI or API key); the agent only restructured it."""
    base = Source.for_rest(principal)
    ref = f"{base.ref} agent:{agent}" if base.ref else f"agent:{agent}"
    return Source(base.kind, base.by, ref)


async def _store(person_id: str, passage: str, options: StoreOptions,
                 principal: Principal) -> StoreReport:
    try:
        report = await store.store(person_id, passage, options, principal,
                                   _source(principal, "store"))
    except (ValueError, AgentRunError) as e:
        raise _bad_request(e) from e
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    await _record_store(principal, person_id, report, chars=len(passage))
    return report


async def _record_store(principal: Principal, person_id: str, report: StoreReport, **meta) -> None:
    if report.applied:
        await activity.record("person.store", principal, person_id, {
            "items": [{"key": i.key, "action": i.action} for i in report.items],
            "created": report.created, **meta})


@router.post("/people/{person_id}/store")
async def store_passage(person_id: str, body: StoreBody, principal: Writer) -> StoreReport:
    """Store what a passage says about the person. With options.dry_run the agent's proposal
    comes back with what would happen and nothing is written."""
    return await _store(person_id, body.passage, body.options, principal)


@router.post("/people/{person_id}/store/apply")
async def store_apply(person_id: str, body: ApplyBody, principal: Writer) -> StoreReport:
    """Apply a proposal (from a dry run, possibly edited); it is checked like the agent's."""
    try:
        report = await store.apply(person_id, body.proposal, body.options, principal,
                                   _source(principal, "store"), commit=not body.options.dry_run)
    except (ValueError, AgentRunError) as e:
        raise _bad_request(e) from e
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    await _record_store(principal, person_id, report, applied_proposal=True)
    return report


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
        raise _bad_request(e) from e
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    return report

@router.get("/jobs")
async def list_jobs(_: Reader) -> list[dict]:
    return jobs.recent()


# --- history ------------------------------------------------------------------------------------

@router.get("/searches")
async def list_searches(
    principal: Annotated[Principal, Depends(require("searches:read_own"))],
    all_users: Annotated[bool, Query(alias="all")] = False,
    user_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    s, u = db.searches, db.users
    q = select(s.c.id, s.c.user_id, u.c.username, s.c.api_key_id, s.c.source, s.c.params,
               s.c.result_count, s.c.created_at).join(u, u.c.id == s.c.user_id)
    if all_users or (user_id is not None and user_id != principal.user_id):
        if not principal.can("searches:read_all"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing permission: searches:read_all")
        if user_id is not None:
            q = q.where(s.c.user_id == user_id)
    else:
        q = q.where(s.c.user_id == principal.user_id)
    async with db.engine().connect() as conn:
        rows = (await conn.execute(q.order_by(s.c.created_at.desc()).limit(limit + 1)
                                   .offset(offset))).mappings().all()
    return {"items": [dict(r) for r in rows[:limit]], "has_more": len(rows) > limit}


@router.get("/searches/{search_id}")
async def get_search(search_id: int,
                     principal: Annotated[Principal, Depends(require("searches:read_own"))]
                     ) -> dict:
    async with db.engine().connect() as conn:
        row = (await conn.execute(select(db.searches).where(db.searches.c.id == search_id))
               ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "search not found")
    if row["user_id"] != principal.user_id and not principal.can("searches:read_all"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "search not found")
    return dict(row)


@router.get("/activity")
async def list_activity(
    _: Annotated[Principal, Depends(require("activity:read"))],
    user_id: int | None = None,
    action: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    a, u = db.activity, db.users
    q = select(a.c.id, a.c.user_id, u.c.username, a.c.api_key_id, a.c.action, a.c.target,
               a.c.meta, a.c.ip, a.c.created_at).join(u, u.c.id == a.c.user_id, isouter=True)
    if user_id is not None:
        q = q.where(a.c.user_id == user_id)
    if action:
        q = q.where(a.c.action.startswith(action, autoescape=True))
    if since:
        q = q.where(a.c.created_at >= since)
    if until:
        q = q.where(a.c.created_at < until)
    async with db.engine().connect() as conn:
        rows = (await conn.execute(q.order_by(a.c.created_at.desc(), a.c.id.desc())
                                   .limit(limit + 1).offset(offset))).mappings().all()
    return {"items": [dict(r) for r in rows[:limit]], "has_more": len(rows) > limit}
