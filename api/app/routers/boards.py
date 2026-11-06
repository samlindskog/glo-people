"""REST for the glojobs.app boards (the glo-people UI): the catalogue of companies, jobs and
talent, and the boards that list them. Reading needs people:read, changing people:write.
Everything here is a plain store call; the public pages are in `app.glojobs`."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app import activity, boards, fields, safe_html
from app.auth import Principal, require
from app.history import Source

router = APIRouter(prefix="/api/boards", tags=["boards"])

Reader = Annotated[Principal, Depends(require("people:read"))]
Writer = Annotated[Principal, Depends(require("people:write"))]


class CompanyIn(BaseModel):
    name: str
    web: str = ""
    line: str = ""
    about_html: str = ""


class JobIn(BaseModel):
    company: str
    title: str
    location: str = ""
    type: str = ""
    comp: str = ""
    summary: str = ""
    html: str = ""
    status: boards.JobStatus = "open"
    source_url: str = ""


class TalentIn(BaseModel):
    person_id: str | None = None
    person: dict[str, Any] | None = None      # instead of person_id: a record, see ensure_person
    tier: fields.Tier = "plus_two"            # for a person `person` creates
    name: str | None = None
    headline: str | None = None
    location: str | None = None
    summary: str | None = None
    html: str | None = None
    status: boards.TalentStatus | None = None


class BoardCreate(BaseModel):
    person_id: str | None = None
    person: dict[str, Any] | None = None      # instead of person_id: a record, see ensure_person
    tier: fields.Tier = "plus_two"
    kind: boards.Kind
    title: str | None = None
    intro_html: str = ""
    job_slugs: list[str] = Field(default_factory=list)
    talent_slugs: list[str] = Field(default_factory=list)
    enabled: bool = True


class BoardPatch(BaseModel):
    person_id: str | None = None
    kind: boards.Kind | None = None
    title: str | None = None
    intro_html: str | None = None
    job_slugs: list[str] | None = None
    talent_slugs: list[str] | None = None
    enabled: bool | None = None


class Preview(BaseModel):
    html: str


def _error(e: Exception) -> HTTPException:
    if isinstance(e, boards.NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    if isinstance(e, boards.PersonExists | boards.PersonAmbiguous):
        # the record names someone already there (or several): the detail carries the ids
        return HTTPException(status.HTTP_409_CONFLICT, e.detail())
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))


# --- companies ----------------------------------------------------------------------------------

@router.get("/companies")
async def list_companies(_: Reader) -> list[dict]:
    """Tool: the companies of the job catalogue."""
    return await boards.list_companies()


@router.put("/companies/{slug}")
async def put_company(slug: str, body: CompanyIn, principal: Writer) -> dict:
    """Tool: create or replace a company; `stripped` lists the tags dropped from its about text."""
    try:
        row = await boards.upsert_company(slug, **body.model_dump())
    except boards.BoardError as e:
        raise _error(e) from e
    await activity.record("board_company.put", principal, row["slug"])
    return row


@router.delete("/companies/{slug}", status_code=204)
async def delete_company(slug: str, principal: Writer) -> None:
    """Tool: delete a company that no job names."""
    try:
        ok = await boards.delete_company(slug)
    except boards.BoardError as e:
        raise _error(e) from e
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    await activity.record("board_company.delete", principal, slug)


# --- jobs ---------------------------------------------------------------------------------------

@router.get("/jobs")
async def list_jobs(_: Reader, company: str | None = None, status: str | None = None,
                    q: str | None = None) -> list[dict]:
    """Tool: the job catalogue, filtered by company, status (open | closed) or a search."""
    return await boards.list_jobs(company, status, q)


@router.get("/jobs/{slug}")
async def get_job(slug: str, _: Reader) -> dict:
    """Tool: one job with the boards that list it."""
    job = await boards.get_job(slug)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return {**job, "boards": await boards.boards_listing("job_slugs", slug)}


@router.put("/jobs/{slug}")
async def put_job(slug: str, body: JobIn, principal: Writer) -> dict:
    """Tool: create or replace a job (the body is sanitised; `stripped` lists dropped tags)."""
    try:
        row = await boards.upsert_job(slug, **body.model_dump(), user_id=principal.user_id)
    except boards.BoardError as e:
        raise _error(e) from e
    await activity.record("board_job.put", principal, row["slug"], {"company": row["company_slug"]})
    return row


@router.delete("/jobs/{slug}", status_code=204)
async def delete_job(slug: str, principal: Writer) -> None:
    """Tool: delete a job; it leaves every board that listed it."""
    if not await boards.delete_job(slug):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    await activity.record("board_job.delete", principal, slug)


# --- talent -------------------------------------------------------------------------------------

@router.get("/talent")
async def list_talent(_: Reader, status: str | None = None, q: str | None = None,
                      person_id: str | None = None) -> list[dict]:
    """Tool: the talent catalogue, filtered by status (active | hidden), a search or a person."""
    return await boards.list_talent(status, q, person_id)


@router.get("/talent/draft")
async def talent_draft(person_id: str, _: Reader) -> dict:
    """Tool: a talent entry drafted from a person's record (name, role at company, location,
    one-liner, bio and about as HTML) for the editor to start from."""
    try:
        return await boards.talent_draft(person_id)
    except boards.BoardError as e:
        raise _error(e) from e


@router.get("/talent/{slug}")
async def get_talent(slug: str, _: Reader) -> dict:
    """Tool: one talent entry with the boards that list it."""
    row = await boards.get_talent(slug)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "talent not found")
    return {**row, "boards": await boards.boards_listing("talent_slugs", slug)}


@router.put("/talent/{slug}")
async def put_talent(slug: str, body: TalentIn, principal: Writer) -> dict:
    """Tool: create or replace a talent entry; fields left null are drafted from the person's
    record. `person_id` links the entry, or `person` (a record) is matched or created: a match
    answers 409 with the person_id to pass (`person_exists`), several matches 409 with the
    candidates (`person_ambiguous`)."""
    try:
        row = await boards.upsert_talent(slug, **body.model_dump(), user_id=principal.user_id,
                                         principal=principal, source=Source.for_rest(principal))
    except boards.BoardError as e:
        raise _error(e) from e
    await activity.record("board_talent.put", principal, row["slug"], {"person_id": row["person_id"]})
    return row


@router.delete("/talent/{slug}", status_code=204)
async def delete_talent(slug: str, principal: Writer) -> None:
    """Tool: delete a talent entry; it leaves every board that listed it."""
    if not await boards.delete_talent(slug):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "talent not found")
    await activity.record("board_talent.delete", principal, slug)


# --- boards -------------------------------------------------------------------------------------

@router.get("/guide")
async def guide(_: Reader) -> dict:
    """Tool: the guide an agent reads before working with the boards (the model, the order of
    calls, how people are linked, the URLs, what HTML survives)."""
    return {"guide": boards.GUIDE}


@router.get("/presence/{person_id}")
async def presence(person_id: str, _: Reader) -> dict:
    """Tool: where a person is on glojobs.app: their own boards, and the talent entry that lists
    them with every board showing it and their page URL there."""
    return await boards.presence(person_id)


@router.post("/preview")
async def preview(body: Preview, _: Reader) -> dict:
    """Tool: the sanitised form of an HTML fragment, as the public page would show it."""
    try:
        cleaned = safe_html.clean(body.html)
    except ValueError as e:
        raise _error(e) from e
    return {"html": cleaned.html, "stripped": cleaned.stripped}


@router.get("")
async def list_boards(_: Reader, person_id: str | None = None) -> list[dict]:
    """Tool: every board (or a person's), newest change first, with the public url and counts."""
    return await boards.list_boards(person_id)


@router.post("", status_code=201)
async def create_board(body: BoardCreate, principal: Writer) -> dict:
    """Tool: a new board for a person (`person_id`, or `person` matched or created as for a
    talent entry); returns it with its public url."""
    try:
        row = await boards.create_board(**body.model_dump(), user_id=principal.user_id,
                                        principal=principal, source=Source.for_rest(principal))
    except boards.BoardError as e:
        raise _error(e) from e
    await activity.record("board.create", principal, row["id"],
                          {"person_id": row["person_id"], "kind": row["kind"]})
    return row


@router.get("/{board_id}")
async def get_board(board_id: str, _: Reader) -> dict:
    """Tool: one board with its items resolved (open jobs with their company, active talent)."""
    try:
        row = await boards.get_board(board_id)
    except boards.NotFound:
        row = None
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "board not found")
    return await boards.resolve(row)


@router.patch("/{board_id}")
async def patch_board(board_id: str, body: BoardPatch, principal: Writer) -> dict:
    """Tool: change a board's person, kind, title, intro, lists or enabled flag."""
    changes: dict[str, Any] = body.model_dump(exclude_unset=True)
    try:
        row = await boards.update_board(board_id, **changes)
    except boards.BoardError as e:
        raise _error(e) from e
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "board not found")
    await activity.record("board.update", principal, row["id"], {"changed": sorted(changes)})
    return row


@router.delete("/{board_id}", status_code=204)
async def delete_board(board_id: str, principal: Writer) -> None:
    """Tool: delete a board; its link stops working."""
    try:
        ok = await boards.delete_board(board_id)
    except boards.NotFound:
        ok = False
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "board not found")
    await activity.record("board.delete", principal, board_id)
