"""The glojobs.app store (level 1): the shared catalogue of companies, jobs and talent, and the
boards — one page per person, reachable by its UUID alone — that list jobs and talent by slug.

Several boards may list the same job or person; a board holds ordered slug lists, and a job that
is closed, a talent entry that is hidden or an entry that was deleted simply drops out of every
board (`resolve` skips what is missing; a delete also removes the slug from the arrays). Authored
HTML (a job description, a profile, a board's intro) goes through `safe_html.clean` on the way in
and is stored sanitised; writers return the stored row and, for HTML, the tags that were dropped.

Every board and every talent entry belongs to a glo-people person, by foreign key: the `people`
table in Postgres mirrors the people the boards refer to (id, name, tier), upserted by the writers
here and kept current by `sync_person`, the write observer (a merge re-keys, a delete cascades).
A writer given a record instead of an id goes through `ensure_person`: someone already in the
database is reported (`PersonExists`, with the id to pass), several plausible people likewise
(`PersonAmbiguous`), and only a stranger is created.
"""

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import any_, delete, func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db, fields, history, importer, safe_html
from app import people as people_svc
from app.auth import Principal
from app.config import get_settings
from app.history import Source

log = logging.getLogger(__name__)

Kind = Literal["jobs", "talent", "both"]
KINDS = ("jobs", "talent", "both")
JobStatus = Literal["open", "closed"]
TalentStatus = Literal["active", "hidden"]
MAX_ITEMS = 200                     # slugs on one board


class BoardError(ValueError):
    pass


class NotFound(BoardError):
    pass


class PersonExists(BoardError):
    """The record names someone already in the database: link them by `person_id` instead."""

    def __init__(self, person_id: str, name: str, keys: list[str]):
        by = ", ".join(keys) if keys else "name"
        super().__init__(f"{name or 'this person'} is already in glo-people as person_id {person_id} "
                         f"(matched by {by}); pass person_id={person_id} to link this entry to them")
        self.person_id, self.name, self.keys = person_id, name, list(keys)

    def detail(self) -> dict[str, Any]:
        return {"error": "person_exists", "person_id": self.person_id, "name": self.name,
                "keys": self.keys, "message": str(self)}


class PersonAmbiguous(BoardError):
    """Several people could be the record: nobody is linked or created until one is named."""

    def __init__(self, candidates: list[dict]):
        names = "; ".join(f"{c['person_id']} {c.get('name') or ''}".strip() for c in candidates)
        super().__init__(f"several people in glo-people could be this record ({names}); pass the "
                         "person_id of the right one")
        self.candidates = candidates

    def detail(self) -> dict[str, Any]:
        return {"error": "person_ambiguous", "candidates": self.candidates, "message": str(self)}


def slug(text: str) -> str:
    """The URL form of a title, as the reference site's build did it: lower case, `&` to `and`,
    runs of anything else to one hyphen."""
    s = (text or "").lower().replace("&", " and ").replace("+", "")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:120]


def public_url(board_id: Any) -> str:
    return f"{get_settings().glojobs_public_url.rstrip('/')}/{board_id}"


def role_url(board_id: Any, job_slug: str) -> str:
    return f"{public_url(board_id)}/roles/{job_slug}"


def person_url(board_id: Any, talent_slug: str) -> str:
    return f"{public_url(board_id)}/people/{talent_slug}"


def _row(r: Any) -> dict[str, Any]:
    out = dict(r._mapping)
    for k, v in out.items():
        if isinstance(v, uuid.UUID):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


def _uuid(board_id: Any) -> uuid.UUID:
    try:
        return board_id if isinstance(board_id, uuid.UUID) else uuid.UUID(str(board_id))
    except (ValueError, AttributeError, TypeError) as e:
        raise NotFound("board not found") from e


def _str(value: Any) -> str:
    return (str(value) if value is not None else "").strip()


async def _upsert(conn: Any, table: Any, key: str, values: dict[str, Any]) -> None:
    """Insert the row keyed by `slug`, or replace its values."""
    existing = (await conn.execute(select(table.c.slug).where(table.c.slug == key))).first()
    if existing:
        await conn.execute(update(table).where(table.c.slug == key).values(**values))
    else:
        await conn.execute(insert(table).values(slug=key, **values))


# --- the people the boards refer to ---------------------------------------------------------------

async def _shadow_upsert(conn: Any, person: dict) -> None:
    """The person's row in the mirrored `people` table, inserted or refreshed."""
    values = {"name": _str(person.get("name")), "tier": _str(person.get("tier")),
              "updated_at": datetime.now(UTC)}
    stmt = pg_insert(db.people).values(id=person["id"], **values)
    await conn.execute(stmt.on_conflict_do_update(index_elements=[db.people.c.id], set_=values))


async def _person(person_id: str) -> dict:
    person = await people_svc.get(person_id, include_history=False)
    if not person:
        raise NotFound("person not found")
    return person


async def _drop_talent_of(conn: Any, person_id: str) -> None:
    row = (await conn.execute(select(db.board_talent.c.slug)
                              .where(db.board_talent.c.person_id == person_id))).first()
    if row:
        await _delete_talent(conn, row.slug)


async def _rekey(conn: Any, drop_id: str, keep_id: str) -> None:
    """A merge: what named the dropped person now names the kept one. The kept person's own
    talent entry wins; the dropped one's moves over only when they had none."""
    keep = await people_svc.get(keep_id, include_history=False)
    if keep is None:
        await _drop_talent_of(conn, drop_id)
        return
    await _shadow_upsert(conn, keep)
    keeps = (await conn.execute(select(db.board_talent.c.slug)
                                .where(db.board_talent.c.person_id == keep_id))).first()
    drops = (await conn.execute(select(db.board_talent.c.slug)
                                .where(db.board_talent.c.person_id == drop_id))).first()
    if drops and keeps:
        await _delete_talent(conn, drops.slug)
    elif drops:
        await conn.execute(update(db.board_talent).where(db.board_talent.c.slug == drops.slug)
                           .values(person_id=keep_id, updated_at=datetime.now(UTC)))
    await conn.execute(update(db.boards).where(db.boards.c.person_id == drop_id)
                       .values(person_id=keep_id, updated_at=datetime.now(UTC)))


async def sync_person(info: "people_svc.WriteInfo") -> None:
    """Write observer (registered in `app.wiring`): a person the boards refer to keeps their name
    and tier current here; a deleted person's row goes, and with it their boards and talent entry
    (the foreign keys cascade; the entry leaves every board's list first); a merge re-keys."""
    async with db.engine().begin() as conn:
        if info.deleted:
            if info.merged_into:
                await _rekey(conn, info.person_id, info.merged_into)
            else:
                await _drop_talent_of(conn, info.person_id)
            await conn.execute(delete(db.people).where(db.people.c.id == info.person_id))
            return
        known = (await conn.execute(select(db.people.c.id)
                                    .where(db.people.c.id == info.person_id))).first()
        if not known:
            return
        person = await people_svc.get(info.person_id, include_history=False)
        if person:
            await _shadow_upsert(conn, person)


async def ensure_person(record: dict[str, Any] | None, principal: Principal | None,
                        source: Source | None = None, tier: str = "plus_two") -> str:
    """The id of the person a record names. Deterministic matching first (a shared email, phone,
    LinkedIn or GitHub URL, else a unique or corroborated name): someone already in the database
    is `PersonExists` — the caller links them by id, so the boards never make a double — and
    several plausible people are `PersonAmbiguous`. Only a stranger is created, through the
    people store, so the write observers embed and review the record like any other."""
    values = {k: v for k, v in (record or {}).items() if v not in (None, "", [])}
    if not _str(values.get("name")):
        raise BoardError("a person record needs a name")
    if principal is None and source is None:
        raise BoardError("creating a person needs a principal or a source to attribute it to")
    match = await importer.match_record(values)
    if match.decision == "matched":
        assert match.person_id is not None
        name = next((c.get("name") for c in match.candidates if c["person_id"] == match.person_id),
                    None)
        raise PersonExists(match.person_id, _str(name) or _str(values["name"]), match.keys)
    if match.decision == "ambiguous":
        raise PersonAmbiguous(match.candidates)
    try:
        res = await people_svc.create(principal, tier, values, source)
    except people_svc.PersonError as e:
        raise BoardError(str(e)) from e
    return res.person["id"]


async def audit_people(*, create: bool = False, tier: str = "plus_two",
                       principal: Principal | None = None, source: Source | None = None) -> dict:
    """Bring the mirrored people and the talent links up to date (the CLI's `boards-link`):
    every mirrored row is refreshed from its document or, when the person is gone, dropped with
    their boards and entry; every talent entry with no person is matched by name — linked when
    the match is certain, created when `create` is on and nobody matches, else reported."""
    report: dict[str, Any] = {"refreshed": 0, "gone": [], "linked": [], "created": [],
                              "ambiguous": [], "unlinked": []}
    async with db.engine().connect() as conn:
        ids = [r.id for r in await conn.execute(select(db.people.c.id))]
    for pid in ids:
        person = await people_svc.get(pid, include_history=False)
        async with db.engine().begin() as conn:
            if person is None:
                await _drop_talent_of(conn, pid)
                await conn.execute(delete(db.people).where(db.people.c.id == pid))
                report["gone"].append(pid)
            else:
                await _shadow_upsert(conn, person)
                report["refreshed"] += 1
    for row in await list_talent():
        if row["person_id"]:
            continue
        record = {"name": row["name"], "location": row["location"]}
        try:
            person_id = await ensure_person(record, principal, source, tier) if create else None
            if person_id is None:
                match = await importer.match_record(record)
                if match.decision == "ambiguous":
                    raise PersonAmbiguous(match.candidates)
                if match.decision == "new":
                    report["unlinked"].append({"slug": row["slug"], "name": row["name"]})
                    continue
                assert match.person_id is not None
                person_id = match.person_id
                outcome = "linked"
            else:
                outcome = "created"
        except PersonExists as e:
            person_id, outcome = e.person_id, "linked"
        except PersonAmbiguous as e:
            report["ambiguous"].append({"slug": row["slug"], "name": row["name"],
                                        "candidates": e.candidates})
            continue
        except BoardError as e:
            report["unlinked"].append({"slug": row["slug"], "name": row["name"], "error": str(e)})
            continue
        person = await people_svc.get(person_id, include_history=False)
        async with db.engine().begin() as conn:
            taken = (await conn.execute(select(db.board_talent.c.slug)
                                        .where(db.board_talent.c.person_id == person_id))).first()
            if taken or person is None:
                report["unlinked"].append({"slug": row["slug"], "name": row["name"],
                                           "error": f"{person_id} already has entry {taken.slug}"
                                           if taken else f"{person_id} does not exist"})
                continue
            await _shadow_upsert(conn, person)
            await conn.execute(update(db.board_talent).where(db.board_talent.c.slug == row["slug"])
                               .values(person_id=person_id, updated_at=datetime.now(UTC)))
        report[outcome].append({"slug": row["slug"], "person_id": person_id})
    return report


# --- companies ----------------------------------------------------------------------------------

async def list_companies() -> list[dict]:
    async with db.engine().connect() as conn:
        rows = await conn.execute(select(db.board_companies).order_by(db.board_companies.c.name))
        return [_row(r) for r in rows]


async def get_company(company_slug: str) -> dict | None:
    async with db.engine().connect() as conn:
        r = (await conn.execute(select(db.board_companies)
                                .where(db.board_companies.c.slug == company_slug))).first()
        return _row(r) if r else None


async def upsert_company(company_slug: str | None, *, name: str, web: str = "", line: str = "",
                         about_html: str = "") -> dict:
    """Create or replace a company. Returns the row plus `stripped` (tags dropped from the
    about text)."""
    name = _str(name)
    if not name:
        raise BoardError("a company needs a name")
    key = slug(company_slug or name)
    if not key:
        raise BoardError("a company needs a slug")
    cleaned = safe_html.clean(about_html)
    web = re.sub(r"^https?://", "", _str(web)).rstrip("/")
    values = {"name": name, "web": web, "line": _str(line), "about_html": cleaned.html,
              "updated_at": datetime.now(UTC)}
    async with db.engine().begin() as conn:
        await _upsert(conn, db.board_companies, key, values)
        row = (await conn.execute(select(db.board_companies)
                                  .where(db.board_companies.c.slug == key))).first()
    return {**_row(row), "stripped": cleaned.stripped}


async def delete_company(company_slug: str) -> bool:
    """Refused while a job still names the company."""
    async with db.engine().begin() as conn:
        n = (await conn.execute(select(func.count()).select_from(db.board_jobs)
                                .where(db.board_jobs.c.company_slug == company_slug))).scalar_one()
        if n:
            raise BoardError(f"{n} job(s) still belong to {company_slug}; delete or move them first")
        res = await conn.execute(delete(db.board_companies)
                                 .where(db.board_companies.c.slug == company_slug))
        return bool(res.rowcount)


# --- jobs ---------------------------------------------------------------------------------------

def _job_query(company: str | None = None, status: str | None = None, q: str | None = None):
    stmt = (select(db.board_jobs, db.board_companies.c.name.label("company_name"))
            .join(db.board_companies, db.board_companies.c.slug == db.board_jobs.c.company_slug)
            .order_by(db.board_companies.c.name, db.board_jobs.c.title))
    if company:
        stmt = stmt.where(db.board_jobs.c.company_slug == company)
    if status:
        stmt = stmt.where(db.board_jobs.c.status == status)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(db.board_jobs.c.title.ilike(like), db.board_companies.c.name.ilike(like),
                              db.board_jobs.c.location.ilike(like), db.board_jobs.c.summary.ilike(like)))
    return stmt


async def list_jobs(company: str | None = None, status: str | None = None,
                    q: str | None = None) -> list[dict]:
    async with db.engine().connect() as conn:
        rows = await conn.execute(_job_query(company, status, q))
        return [_row(r) for r in rows]


async def get_job(job_slug: str) -> dict | None:
    async with db.engine().connect() as conn:
        r = (await conn.execute(_job_query().where(db.board_jobs.c.slug == job_slug))).first()
        return _row(r) if r else None


async def upsert_job(job_slug: str | None, *, company: str, title: str, location: str = "",
                     type: str = "", comp: str = "", summary: str = "", html: str = "",
                     status: str = "open", source_url: str = "", user_id: int | None = None) -> dict:
    """Create or replace a job. The slug defaults to `<company>-<title>`; the company must
    exist. Returns the row plus `stripped`."""
    title = _str(title)
    company = slug(company)
    if not title:
        raise BoardError("a job needs a title")
    if status not in ("open", "closed"):
        raise BoardError("status must be open or closed")
    key = slug(job_slug) if job_slug else slug(f"{company} {title}")
    if not key:
        raise BoardError("a job needs a slug")
    source_url = _str(source_url)
    if source_url and not re.match(r"^https?://", source_url, re.IGNORECASE):
        raise BoardError("source_url must start with http:// or https://")   # it becomes a public href
    cleaned = safe_html.clean(html)
    values = {"company_slug": company, "title": title, "location": _str(location),
              "type": _str(type), "comp": _str(comp), "summary": _str(summary),
              "html": cleaned.html, "status": status, "source_url": source_url,
              "updated_at": datetime.now(UTC), "updated_by": user_id}
    async with db.engine().begin() as conn:
        co = (await conn.execute(select(db.board_companies.c.slug)
                                 .where(db.board_companies.c.slug == company))).first()
        if not co:
            raise BoardError(f"unknown company {company!r}; create it first")
        await _upsert(conn, db.board_jobs, key, values)
        row = (await conn.execute(_job_query().where(db.board_jobs.c.slug == key))).first()
    return {**_row(row), "stripped": cleaned.stripped}


async def delete_job(job_slug: str) -> bool:
    """Deletes the job and drops its slug from every board."""
    async with db.engine().begin() as conn:
        res = await conn.execute(delete(db.board_jobs).where(db.board_jobs.c.slug == job_slug))
        if res.rowcount:
            await conn.execute(update(db.boards)
                               .where(literal(job_slug) == any_(db.boards.c.job_slugs))
                               .values(job_slugs=func.array_remove(db.boards.c.job_slugs, job_slug),
                                       updated_at=datetime.now(UTC)))
        return bool(res.rowcount)


# --- talent -------------------------------------------------------------------------------------

def _talent_query(status: str | None = None, q: str | None = None, person_id: str | None = None):
    stmt = select(db.board_talent).order_by(db.board_talent.c.name)
    if status:
        stmt = stmt.where(db.board_talent.c.status == status)
    if person_id:
        stmt = stmt.where(db.board_talent.c.person_id == person_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(db.board_talent.c.name.ilike(like), db.board_talent.c.headline.ilike(like),
                              db.board_talent.c.location.ilike(like), db.board_talent.c.summary.ilike(like)))
    return stmt


async def list_talent(status: str | None = None, q: str | None = None,
                      person_id: str | None = None) -> list[dict]:
    async with db.engine().connect() as conn:
        rows = await conn.execute(_talent_query(status, q, person_id))
        return [_row(r) for r in rows]


async def get_talent(talent_slug: str) -> dict | None:
    async with db.engine().connect() as conn:
        r = (await conn.execute(select(db.board_talent)
                                .where(db.board_talent.c.slug == talent_slug))).first()
        return _row(r) if r else None


async def talent_draft(person_id: str) -> dict:
    """A talent entry drafted from a person's record: name, headline (role at company),
    location, a one-line summary and an HTML body from the bio and about texts."""
    return _draft(await _person(person_id))


def _draft(person: dict) -> dict:
    def val(key: str) -> str:
        v = person.get(key)
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        return _str(v)

    role, company = val("role"), val("company")
    headline = " at ".join(p for p in (role, company) if p)
    summary = val("one_liner") or val("linkedin_headline") or val("intro_bio")
    paras = [t for t in (val("bio"), val("about")) if t]
    body = "".join(f"<p>{safe_html.escape(t)}</p>" for t in paras)
    return {"slug": slug(val("name")), "person_id": person["id"], "name": val("name"),
            "headline": headline, "location": val("location") or val("city"),
            "summary": summary, "html": body, "status": "active"}


NEEDS_PERSON = ("a talent entry must be linked to a glo-people record: pass person_id, or "
                "person={name, email, company, …} for someone not in the database yet")


async def upsert_talent(talent_slug: str | None, *, name: str | None = None,
                        person_id: str | None = None, person: dict[str, Any] | None = None,
                        headline: str | None = None, location: str | None = None,
                        summary: str | None = None, html: str | None = None,
                        status: str | None = None, user_id: int | None = None,
                        principal: Principal | None = None, source: Source | None = None,
                        tier: str = "plus_two") -> dict:
    """Create or replace a talent entry. Every entry is one person's: `person_id` names them, or
    `person` (a record) goes through `ensure_person`. Every field left None is drafted from the
    person's record (`talent_draft`); the slug defaults to the name. A person has one entry: a
    second slug for the same person is refused with the existing one. Returns the row plus
    `stripped`."""
    if not person_id:
        if not person:
            raise BoardError(NEEDS_PERSON)
        person_id = await ensure_person(person, principal, source, tier)
    doc = await _person(person_id)
    draft = _draft(doc)
    name = _str(name) or draft["name"]
    if not name:
        raise BoardError("a talent entry needs a name")
    key = slug(talent_slug) if talent_slug else draft["slug"] or slug(name)
    if not key:
        raise BoardError("a talent entry needs a slug")
    status = status or draft["status"]
    if status not in ("active", "hidden"):
        raise BoardError("status must be active or hidden")

    def pick(value: str | None, field: str) -> str:
        return _str(value) if value is not None else _str(draft.get(field))

    cleaned = safe_html.clean(pick(html, "html"))
    values = {"person_id": person_id, "name": name, "headline": pick(headline, "headline"),
              "location": pick(location, "location"), "summary": pick(summary, "summary"),
              "html": cleaned.html, "status": status, "updated_at": datetime.now(UTC),
              "updated_by": user_id}
    async with db.engine().begin() as conn:
        # one writer per person at a time, so two first links cannot both pass the check below
        # and meet the unique index as a bare integrity error
        await conn.execute(select(func.pg_advisory_xact_lock(func.hashtext(person_id))))
        other = (await conn.execute(select(db.board_talent.c.slug)
                                    .where(db.board_talent.c.person_id == person_id,
                                           db.board_talent.c.slug != key))).first()
        if other:
            raise BoardError(f"{name} already has the talent entry '{other.slug}'; pass that slug "
                             "to replace it")
        # the slug may be new, this person's own, or a legacy row with no person (linked now);
        # never another person's, whose entry would silently become someone else's
        held = (await conn.execute(select(db.board_talent.c.person_id, db.board_talent.c.name)
                                   .where(db.board_talent.c.slug == key).with_for_update())).first()
        if held and held.person_id and held.person_id != person_id:
            raise BoardError(f"'{key}' is {held.name}'s entry (person_id {held.person_id}); give "
                             "this one its own slug")
        await _shadow_upsert(conn, doc)
        await _upsert(conn, db.board_talent, key, values)
        row = (await conn.execute(select(db.board_talent)
                                  .where(db.board_talent.c.slug == key))).first()
    return {**_row(row), "stripped": cleaned.stripped}


async def _delete_talent(conn: Any, talent_slug: str) -> bool:
    res = await conn.execute(delete(db.board_talent).where(db.board_talent.c.slug == talent_slug))
    if res.rowcount:
        await conn.execute(update(db.boards)
                           .where(literal(talent_slug) == any_(db.boards.c.talent_slugs))
                           .values(talent_slugs=func.array_remove(db.boards.c.talent_slugs,
                                                                  talent_slug),
                                   updated_at=datetime.now(UTC)))
    return bool(res.rowcount)


async def delete_talent(talent_slug: str) -> bool:
    async with db.engine().begin() as conn:
        return await _delete_talent(conn, talent_slug)


# --- boards -------------------------------------------------------------------------------------

async def _person_names(ids: list[str]) -> dict[str, str]:
    docs = await people_svc.fetch(list({i for i in ids if i}), {"name": 1})
    return {str(d["_id"]): _str(history.current(d.get("name"))) for d in docs}


async def _check_slugs(conn: Any, job_slugs: list[str], talent_slugs: list[str]) -> None:
    if len(job_slugs) > MAX_ITEMS or len(talent_slugs) > MAX_ITEMS:
        raise BoardError(f"at most {MAX_ITEMS} items of a kind on one board")
    if job_slugs:
        known = {r[0] for r in await conn.execute(select(db.board_jobs.c.slug)
                                                   .where(db.board_jobs.c.slug.in_(job_slugs)))}
        if missing := [s for s in job_slugs if s not in known]:
            raise BoardError(f"unknown job(s): {', '.join(missing)}")
    if talent_slugs:
        known = {r[0] for r in await conn.execute(select(db.board_talent.c.slug)
                                                   .where(db.board_talent.c.slug.in_(talent_slugs)))}
        if missing := [s for s in talent_slugs if s not in known]:
            raise BoardError(f"unknown talent: {', '.join(missing)}")


def _dedupe(slugs: list[str] | None) -> list[str]:
    """The slugs in order, each once, blanks dropped."""
    return fields.distinct((_str(s) for s in slugs or []), key=str)


def _with_url(row: dict, names: dict[str, str]) -> dict:
    return {**row, "url": public_url(row["id"]), "person_name": names.get(row["person_id"], ""),
            "job_count": len(row["job_slugs"]), "talent_count": len(row["talent_slugs"])}


async def list_boards(person_id: str | None = None) -> list[dict]:
    stmt = select(db.boards).order_by(db.boards.c.updated_at.desc())
    if person_id:
        stmt = stmt.where(db.boards.c.person_id == person_id)
    async with db.engine().connect() as conn:
        rows = [_row(r) for r in await conn.execute(stmt)]
    names = await _person_names([r["person_id"] for r in rows])
    return [_with_url(r, names) for r in rows]


async def boards_listing(field: str, slug_: str) -> list[dict]:
    """The boards whose `job_slugs` or `talent_slugs` hold this slug, as a short reference."""
    column = db.boards.c.job_slugs if field == "job_slugs" else db.boards.c.talent_slugs
    async with db.engine().connect() as conn:
        rows = [_row(r) for r in await conn.execute(
            select(db.boards).where(literal(slug_) == any_(column)).order_by(db.boards.c.updated_at.desc()))]
    names = await _person_names([r["person_id"] for r in rows])
    item_url = role_url if field == "job_slugs" else person_url
    return [{"id": r["id"], "person_id": r["person_id"], "person_name": names.get(r["person_id"], ""),
             "kind": r["kind"], "url": item_url(r["id"], slug_), "board_url": public_url(r["id"])}
            for r in rows]


async def presence(person_id: str) -> dict:
    """Where a person is on glojobs.app: `boards`, their own pages, and `listed_on`, their talent
    entry with every board that shows it and their page URL on each."""
    own = [{k: b[k] for k in ("id", "kind", "title", "url", "enabled", "views", "last_viewed_at",
                              "job_count", "talent_count")} for b in await list_boards(person_id)]
    listed = [{"slug": t["slug"], "name": t["name"], "status": t["status"],
               "boards": await boards_listing("talent_slugs", t["slug"])}
              for t in await list_talent(person_id=person_id)]
    return {"boards": own, "listed_on": listed}


async def get_board(board_id: Any) -> dict | None:
    async with db.engine().connect() as conn:
        r = (await conn.execute(select(db.boards).where(db.boards.c.id == _uuid(board_id)))).first()
    if not r:
        return None
    row = _row(r)
    return _with_url(row, await _person_names([row["person_id"]]))


async def create_board(*, kind: str, person_id: str | None = None,
                       person: dict[str, Any] | None = None, title: str | None = None,
                       intro_html: str = "", job_slugs: list[str] | None = None,
                       talent_slugs: list[str] | None = None, enabled: bool = True,
                       user_id: int | None = None, principal: Principal | None = None,
                       source: Source | None = None, tier: str = "plus_two") -> dict:
    """A new board. `person_id` names whose page it is, or `person` (a record) goes through
    `ensure_person`: matched people are reported with their id, only a stranger is created."""
    if kind not in KINDS:
        raise BoardError("kind must be jobs, talent or both")
    if not person_id:
        if not person:
            raise BoardError("pass person_id or person")
        person_id = await ensure_person(person, principal, source, tier)
    doc = await _person(person_id)
    jobs, talent = _dedupe(job_slugs), _dedupe(talent_slugs)
    cleaned = safe_html.clean(intro_html)
    board_id = uuid.uuid4()
    async with db.engine().begin() as conn:
        await _check_slugs(conn, jobs, talent)
        await _shadow_upsert(conn, doc)
        await conn.execute(insert(db.boards).values(
            id=board_id, person_id=doc["id"], kind=kind, title=_str(title) or None,
            intro_html=cleaned.html, job_slugs=jobs, talent_slugs=talent, enabled=enabled,
            created_by=user_id))
    board = await get_board(board_id)
    assert board is not None
    return {**board, "stripped": cleaned.stripped}


async def update_board(board_id: Any, *, kind: str | None = None, title: str | None = None,
                       intro_html: str | None = None, job_slugs: list[str] | None = None,
                       talent_slugs: list[str] | None = None, add_jobs: list[str] | None = None,
                       remove_jobs: list[str] | None = None, add_talent: list[str] | None = None,
                       remove_talent: list[str] | None = None, enabled: bool | None = None,
                       person_id: str | None = None) -> dict | None:
    """Change a board. `job_slugs` / `talent_slugs` replace the lists; `add_*` / `remove_*` edit
    them in place (applied after a replacement). An empty title string clears the title."""
    key = _uuid(board_id)
    if kind is not None and kind not in KINDS:
        raise BoardError("kind must be jobs, talent or both")
    stripped: list[str] = []
    values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
    if kind is not None:
        values["kind"] = kind
    if title is not None:
        values["title"] = _str(title) or None
    if intro_html is not None:
        cleaned = safe_html.clean(intro_html)
        values["intro_html"], stripped = cleaned.html, cleaned.stripped
    if enabled is not None:
        values["enabled"] = enabled
    owner = await _person(person_id) if person_id is not None else None
    if owner is not None:
        values["person_id"] = owner["id"]
    async with db.engine().begin() as conn:
        # locked: two concurrent add_* / remove_* calls must not both read the same lists
        cur = (await conn.execute(select(db.boards).where(db.boards.c.id == key)
                                  .with_for_update())).first()
        if not cur:
            return None
        if owner is not None:
            await _shadow_upsert(conn, owner)
        jobs = _dedupe(job_slugs) if job_slugs is not None else list(cur.job_slugs)
        talent = _dedupe(talent_slugs) if talent_slugs is not None else list(cur.talent_slugs)
        jobs = _dedupe(jobs + (add_jobs or []))
        jobs = [s for s in jobs if s not in set(_dedupe(remove_jobs))]
        talent = _dedupe(talent + (add_talent or []))
        talent = [s for s in talent if s not in set(_dedupe(remove_talent))]
        await _check_slugs(conn, jobs, talent)
        values["job_slugs"], values["talent_slugs"] = jobs, talent
        await conn.execute(update(db.boards).where(db.boards.c.id == key).values(**values))
    board = await get_board(key)
    return {**board, "stripped": stripped} if board else None


async def delete_board(board_id: Any) -> bool:
    async with db.engine().begin() as conn:
        res = await conn.execute(delete(db.boards).where(db.boards.c.id == _uuid(board_id)))
        return bool(res.rowcount)


async def record_view(board_id: Any) -> None:
    async with db.engine().begin() as conn:
        await conn.execute(update(db.boards).where(db.boards.c.id == _uuid(board_id))
                           .values(views=db.boards.c.views + 1, last_viewed_at=datetime.now(UTC)))


def default_title(board: dict) -> str:
    first = (board.get("person_name") or "").split(" ")[0]
    who = f" for {first}" if first else ""
    if board["kind"] == "talent":
        return f"People{who}"
    if board["kind"] == "both":
        return f"Roles and people{who}"
    return f"Roles picked{who}"


async def resolve(board: dict) -> dict:
    """The board with its items as the page shows them: `jobs` (open, in the board's order, each
    with its company), `companies` (in order of first appearance, with their open jobs), and
    `talent` (active, in order). Missing slugs are skipped."""
    jobs: list[dict] = []
    talent: list[dict] = []
    async with db.engine().connect() as conn:
        if board["job_slugs"]:
            rows = {r.slug: _row(r) for r in await conn.execute(
                _job_query(status="open").where(db.board_jobs.c.slug.in_(board["job_slugs"])))}
            jobs = [rows[s] for s in board["job_slugs"] if s in rows]
            co_slugs = _dedupe([j["company_slug"] for j in jobs])
            cos = {r.slug: _row(r) for r in await conn.execute(
                select(db.board_companies).where(db.board_companies.c.slug.in_(co_slugs)))} if co_slugs else {}
        else:
            cos, co_slugs = {}, []
        if board["talent_slugs"]:
            rows = {r.slug: _row(r) for r in await conn.execute(
                _talent_query(status="active").where(db.board_talent.c.slug.in_(board["talent_slugs"])))}
            talent = [rows[s] for s in board["talent_slugs"] if s in rows]
    jobs = [{**j, "url": role_url(board["id"], j["slug"])} for j in jobs]
    talent = [{**t, "url": person_url(board["id"], t["slug"])} for t in talent]
    companies = [{**cos[c], "jobs": [j for j in jobs if j["company_slug"] == c]}
                 for c in co_slugs if c in cos]
    return {**board, "title": board.get("title") or default_title(board), "jobs": jobs,
            "companies": companies, "talent": talent}


# --- the guide an agent reads first ---------------------------------------------------------------

GUIDE = """\
# glojobs.app boards — how an agent works with them

**What a board is.** A board is one person's private page on glojobs.app, reachable only by its
UUID link (there is no index; the link is the secret). It lists open roles, people, or both, in
the order you give, drawn from a shared catalogue. The same job or person can sit on many boards.

**The catalogue comes first.** Companies hold jobs; talent entries are people. Create or update
them before a board names them:
1. upsert_board_company(slug, name, web, line, about_html) — the slug is the URL key.
2. upsert_board_job(company=<company slug>, title, location, type, comp, summary, html,
   status=open|closed, source_url). The slug defaults to "<company>-<title>" slugified; pass the
   existing slug to replace. A closed job leaves every board. list_board_jobs / get_board_job read.
3. upsert_board_talent(person_id=…, or person={name, email, company, role, location, …}) — see
   below. Fields left out (name, headline "role at company", location, summary, the profile) are
   drafted from the glo-people record; pass any to override. status=hidden takes the entry off
   every board. list_board_talent / get_board_talent read.

**Every talent entry and every board is a glo-people person.** glojobs.app shows only what you
put in the entry, never the record, but the link is required so the two systems agree on who is
who. Give person_id when you know it (find_people tells you; get_person shows `glojobs.boards`
and `glojobs.listed_on`). Give `person` (a record with at least a name) when you do not: the
matcher looks the
record up by email, phone, LinkedIn or GitHub URL, else by a unique or corroborated name.
- Nobody matches: the person is created in glo-people (tier plus_two unless you pass `tier`) and
  linked.
- Someone matches: the call fails with "already in glo-people as person_id X" — call again with
  that person_id. Nothing was written.
- Several could match: the call fails listing the candidates — pick one and pass its person_id.
A person has one talent entry; a second slug for the same person is refused with the existing one.
Deleting a person in glo-people deletes their boards and talent entry; merging two people moves
them to the kept person.

**Boards.** create_board(kind=jobs|talent|both, person_id or person, title, intro_html,
job_slugs, talent_slugs) returns the board with `url` — hand that link to the person. get_board
resolves it: open jobs grouped by company and active talent, each item with its own `url`
(<board>/roles/<slug>, <board>/people/<slug>), plus views and last_viewed_at. update_board changes
the lists (job_slugs / talent_slugs replace; add_jobs, remove_jobs, add_talent, remove_talent edit
in place), the title ('' restores the default "Roles picked for <first name>"), the intro, the
owner, or enabled=false to switch the link off without deleting. list_boards(person_id) lists a
person's pages. delete_board removes one.

**HTML.** Descriptions, profiles and intros are HTML: h2, p, ul/li, strong, em, a. Scripts,
styles, images and iframes are dropped and the writer returns `stripped` naming what went.

**Slugs** are lower case, hyphenated, at most 120 characters; the tools derive them from titles and
names, so you rarely pass one except to replace an existing entry.
"""
