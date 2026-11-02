import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_ai.exceptions import AgentRunError
from sqlalchemy import select

from app import activity, db, fields, importer
from app import people as people_svc
from app.assistants import merge, runner
from app.assistants.merge import MergeOptions
from app.auth import Principal, require
from app.history import Source
from app.integrations.firecrawl import AsyncFirecrawl
from app.routers.search import agent_error
from app.tools import invoke
from app.tools import normalize as normalize_tool
from app.tools import store as store_tool
from app.tools.normalize import NormalizeOptions
from app.tools.store import StoreOptions

router = APIRouter(prefix="/api", tags=["people"])

Reader = Annotated[Principal, Depends(require("people:read"))]
Writer = Annotated[Principal, Depends(require("people:write"))]

MAX_UPLOAD = 25 * 1024 * 1024
MAX_PASSAGE = fields.MAX_TEXT_CHARS


class PersonCreate(BaseModel):
    tier: fields.Tier
    fields: dict[str, Any]


class PersonPatch(BaseModel):
    tier: fields.Tier | None = None
    fields: dict[str, Any] = {}


def _bad_request(e: Exception) -> HTTPException:
    if isinstance(e, people_svc.ConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(e))
    if isinstance(e, AgentRunError):
        return agent_error(e)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


# --- people -------------------------------------------------------------------------------------

@router.get("/people/find")
async def find_people(_: Reader, name: str | None = None, email: str | None = None,
                      phone: str | None = None, linkedin: str | None = None,
                      github: str | None = None) -> dict:
    """Who a record is: the people sharing an identifier (email, phone, LinkedIn or GitHub URL,
    compared on canonical forms) or the name, with the merge assistant's decision (matched by
    what, ambiguous between whom, or nobody). The deterministic lookup before a search."""
    values = {k: v for k, v in {"name": name, "email": email, "phone_number": phone,
                                "linkedin": linkedin, "github": github}.items() if v}
    if not values:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "give a name or an identifier")
    return (await importer.match_record(values)).public()


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
        return (await people_svc.create(principal, body.tier, body.fields)).person
    except people_svc.PersonError as e:
        raise _bad_request(e) from e


@router.patch("/people/{person_id}")
async def patch_person(person_id: str, body: PersonPatch, principal: Writer) -> dict:
    try:
        res = await people_svc.update(principal, person_id, body.fields, body.tier)
    except people_svc.PersonError as e:
        raise _bad_request(e) from e
    if res is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    return res.person


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


def _columns(raw: str | None) -> dict[str, str] | None:
    """The caller's column mapping: a JSON object header → property key | "id" | "skip"."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"columns: {e}") from e
    if not isinstance(parsed, dict) or not all(isinstance(v, str) for v in parsed.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "columns must map each header to a property key, 'id' or 'skip'")
    return parsed


@router.post("/people/import/preview")
async def import_preview(file: Annotated[UploadFile, File()], _: Writer,
                         columns: Annotated[str | None, Form()] = None) -> dict:
    """Each column's plan (identifier, verbatim, agent, new property, skip), the row count, and the
    match decisions of the first rows against the database. `columns` (JSON, header → key | id |
    skip) overrides the header mapping. Nothing is written."""
    try:
        return await importer.preview(await _read_upload(file), file.filename or "upload.csv",
                                      _columns(columns))
    except ValueError as e:
        raise _bad_request(e) from e


@router.post("/people/import", status_code=201)
async def import_people(file: Annotated[UploadFile, File()],
                        tier: Annotated[fields.Tier, Form()],
                        principal: Writer,
                        dry_run: Annotated[bool, Form()] = False,
                        agent: Annotated[bool, Form()] = True,
                        review: Annotated[bool, Form()] = True,
                        columns: Annotated[str | None, Form()] = None) -> dict:
    """Assistant: merge the CSV's rows into the people database as a run, one item per person
    the file describes (follow it on /api/assistants/runs). Columns the catalog lacks are created
    first (except on a dry run); `tier` is for the people the run creates."""
    text = await _read_upload(file)
    name = file.filename or "upload.csv"
    options = MergeOptions(tier=tier, dry_run=dry_run, agent=agent, review=review)
    try:
        parsed, created, items = await merge.prepare_csv(text, name, dry_run=dry_run,
                                                         columns=_columns(columns))
    except ValueError as e:                     # PropertyError is one
        raise _bad_request(e) from e
    if not items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no rows with a name")
    run = await runner.create_run(merge.SPEC, principal, items, {
        "options": options.model_dump(), "file": parsed.file},
        label=f"{'dry run of ' if dry_run else ''}{parsed.file}")
    await runner.start(run["id"], principal)
    summary = {"file": parsed.file, "tier": tier, "rows": parsed.total, "skipped": parsed.skipped,
               "new_properties": created, "run_id": run["id"], "items": len(items),
               "dry_run": dry_run}
    await activity.record("people.import", principal, parsed.file, summary)
    return summary


class MergeBody(BaseModel):
    record: dict[str, Any] = Field(description="Property key → value; a `person_id` names the "
                                               "person, otherwise the record is matched by "
                                               "identifier or name")
    options: MergeOptions = Field(default_factory=MergeOptions)


@router.post("/people/{keep_id}/merge/{drop_id}")
async def merge_people(keep_id: str, drop_id: str, principal: Writer) -> dict:
    """Fold a duplicate document into another: every entry of the dropped person joins the kept
    one's histories with its own time and source, the duplicate is deleted. Returns the kept
    person and the changes."""
    try:
        res = await people_svc.merge_documents(principal, keep_id, drop_id)
    except people_svc.ConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except people_svc.PersonError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return {"person": res.person, "changes": [c.model_dump() for c in res.changes]}


@router.post("/people/merge")
async def merge_person(body: MergeBody, principal: Writer) -> dict:
    """Assistant: merge one record now (a one-item merge run, waited for). Returns the item's
    result: matched (by what) or created, what was written verbatim and filed by the storage
    agent, the review, every change; an ambiguous record is skipped with its candidates."""
    try:
        return await merge.merge_one(body.record, body.options, principal)
    except (ValueError, TypeError) as e:
        raise _bad_request(e) from e


# --- tools on one person: invocations ------------------------------------------------------------------

Wait = Annotated[bool, Query(description="Block until the tool is done and return its result "
                                         "(200) instead of the invocation handle (202)")]


class StoreBody(BaseModel):
    passage: str = Field(min_length=1, max_length=MAX_PASSAGE)
    options: StoreOptions = Field(default_factory=StoreOptions)


def _source(principal: Principal, agent: str) -> Source:
    """The passage came from this person (UI or API key); the agent only restructured it."""
    base = Source.for_rest(principal)
    ref = f"{base.ref} agent:{agent}" if base.ref else f"agent:{agent}"
    return Source(base.kind, base.by, ref)


async def _invoke(tool, person_id: str, principal: Principal, source: Source, wait: bool, *,
                  firecrawl: AsyncFirecrawl | None = None, action: str, meta: dict[str, Any],
                  **kwargs: Any):
    """Start a tool on one record. 202 with the invocation handle (follow its events, stop it,
    fetch its result), or with `wait` the tool's result once it has committed."""
    try:
        ctx = await invoke.make_context(person_id, source, principal, firecrawl=firecrawl,
                                        owns_firecrawl=firecrawl is not None)
    except people_svc.PersonError as e:
        raise _bad_request(e) from e
    if ctx is None:
        if firecrawl is not None:
            await firecrawl.aclose()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    inv = invoke.invoke(tool, ctx, **kwargs)

    async def record(done: invoke.Invocation) -> None:
        result = done.result
        await activity.record(action, principal, person_id, {
            **meta, "invocation": done.id, "status": done.status, "credits": done.credits,
            "changes": [{"key": c.key, "action": c.action} for c in result.changes] if result else [],
            "created": getattr(result, "created_properties", [])})

    inv.on_done(record)
    if not wait:
        return JSONResponse(inv.public(), status_code=status.HTTP_202_ACCEPTED)
    try:
        result = await inv.wait()
    except invoke.Stopped as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except (ValueError, AgentRunError) as e:
        raise _bad_request(e) from e
    return JSONResponse(result.model_dump(mode="json"))


@router.post("/people/{person_id}/store", status_code=202, response_model=None)
async def store_passage(person_id: str, body: StoreBody, principal: Writer, wait: Wait = False):
    """Tool: store what a passage says about the person. Returns the invocation (202); with
    `wait=true`, the StoreResult with every change."""
    return await _invoke(store_tool.store, person_id, principal, _source(principal, "store"), wait,
                         action="person.store", meta={"chars": len(body.passage)},
                         passage=body.passage, opts=body.options)


@router.post("/people/{person_id}/store/file", status_code=202, response_model=None)
async def store_file(person_id: str, file: Annotated[UploadFile, File()], principal: Writer,
                     wait: Wait = False):
    """Tool: a .txt or .md file as the passage, stored with the default options."""
    data = await file.read(MAX_PASSAGE + 1)
    if len(data) > MAX_PASSAGE:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE,
                            f"file larger than {MAX_PASSAGE // 1000} kB")
    try:
        passage = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "file is not UTF-8 text") from e
    if not passage.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "file is empty")
    return await _invoke(store_tool.store, person_id, principal, _source(principal, "store"), wait,
                         action="person.store", meta={"chars": len(passage), "file": file.filename},
                         passage=passage, opts=StoreOptions())


class NormalizeBody(BaseModel):
    options: NormalizeOptions = Field(default_factory=NormalizeOptions)


@router.post("/people/{person_id}/normalize", status_code=202, response_model=None)
async def normalize_one(person_id: str, principal: Writer, body: NormalizeBody | None = None,
                        wait: Wait = False):
    """Tool: re-parse the person's typed values (an agent decides what the parser cannot, merging
    into or extending the enum sets) and, for a record without a record kind, repair its identity
    data and classify it. Returns the invocation (202); with `wait=true`, the NormalizeResult."""
    options = (body or NormalizeBody()).options
    return await _invoke(normalize_tool.normalize, person_id, principal,
                         Source("agent", "normalizer", f"user:{principal.username}"), wait, action="person.normalize.tool",
                         meta={"options": options.model_dump()}, opts=options)


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
