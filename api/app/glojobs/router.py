"""The public glojobs.app pages (level 4): one board per UUID, its roles and its people, rendered
from the templates ported from the reference site's build. No authentication: the UUID is the
secret, nothing lists boards, an unknown or disabled one answers the same "not here" page, and a
role or person slug is only served on a board that lists it.

Served under `/glojobs/`; prod's nginx maps the glojobs.app root onto that prefix, dev reaches
it through the prefix. `settings.glojobs_public_url` carries the base path the links use."""

import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import boards, ops, safe_html
from app.config import get_settings

HERE = Path(__file__).resolve().parent
router = APIRouter(prefix="/glojobs", tags=["glojobs"], include_in_schema=False)
static_files = StaticFiles(directory=HERE / "static")

_env = Environment(loader=FileSystemLoader(HERE / "templates"),
                   autoescape=select_autoescape(default=True, default_for_string=True),
                   trim_blocks=True, lstrip_blocks=True)


def base_path() -> str:
    """The path prefix the public pages live under ('' on glojobs.app, '/glojobs' in dev)."""
    return urlparse(get_settings().glojobs_public_url).path.rstrip("/")


def _site() -> dict:
    s = get_settings()
    return {"name": "GLO Hiring", "contact_email": s.glojobs_contact_email,
            "community_url": s.glojobs_community_url}


def _date(iso: str | None) -> str:
    if not iso:
        return ""
    d = datetime.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def short_location(text: str) -> str:
    return re.sub(r"\s*\(.*?\)\s*", " ", text or "").strip()


def first_sentence(text: str) -> str:
    m = re.match(r"^.*?[.!?](\s|$)", text or "")
    return (m.group(0) if m else text or "").strip()


def _prepare(job: dict) -> dict:
    """A job as the index shows it: the search text and the location without parentheticals."""
    parts = [job["title"], job.get("company_name", ""), job["location"], job["type"],
             job["comp"], job["summary"], safe_html.text_of(job["html"])]
    return {**job, "short_location": short_location(job["location"]),
            "search": " ".join(p for p in parts if p).lower()}


def render(template: str, status: int = 200, **ctx) -> HTMLResponse:
    html = _env.get_template(template).render(site=_site(), base=base_path(), **ctx)
    return HTMLResponse(html, status_code=status,
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@contextmanager
def _page(**facts: Any):
    """A public page: its facts go on the request's row (`attrs`) in the request log."""
    ops.note(page=True, **facts)
    yield


def lost() -> HTMLResponse:
    return render("lost.html", 404, title="Page not found", description="This page isn't here.",
                  home=base_path() or "/", updated="")


async def _board(board_id: str) -> dict | None:
    """The resolved board, or None with the reason noted on the request's row in the request log
    (`outcome`, and for a page served: whose board, its kind and what it held), so a 404 says
    whether the link was malformed, unknown, switched off, or pointed at a slug the board does
    not list."""
    try:
        board = await boards.get_board(board_id)
    except boards.NotFound:
        ops.note(outcome="not_a_uuid")
        return None
    if not board:
        ops.note(outcome="unknown")
        return None
    if not board["enabled"]:
        ops.note(outcome="disabled")
        return None
    resolved = await boards.resolve(board)
    resolved["jobs"] = [_prepare(j) for j in resolved["jobs"]]
    for c in resolved["companies"]:
        c["jobs"] = [_prepare(j) for j in c["jobs"]]
    ops.note(outcome="ok", board=board["id"], person_id=board["person_id"], kind=board["kind"],
             jobs=len(resolved["jobs"]), talent=len(resolved["talent"]))
    return resolved


@router.get("/", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return render("home.html", title="GLO Hiring", description="Roles and people, picked for you.",
                  home=base_path() or "/", updated="")


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots() -> Response:
    # Crawling stays allowed so link previews work; every page carries noindex.
    return PlainTextResponse("User-agent: *\nAllow: /\n")


@router.get("/{board_id}", response_class=HTMLResponse)
async def board_page(board_id: str) -> HTMLResponse:
    with _page():
        board = await _board(board_id)
        if board is None:
            return lost()
        await boards.record_view(board["id"])
        n_jobs, n_people = len(board["jobs"]), len(board["talent"])
        parts = []
        if board["kind"] != "talent":
            parts.append(f"{n_jobs} open role{'' if n_jobs == 1 else 's'}")
        if board["kind"] != "jobs":
            parts.append(f"{n_people} {'person' if n_people == 1 else 'people'}")
        return render("board.html", board=board, title=board["title"],
                      description=" and ".join(parts) + ", picked for you by GLO.",
                      home=f"{base_path()}/{board['id']}", updated=_date(board["updated_at"]))


@router.get("/{board_id}/roles/{slug}", response_class=HTMLResponse)
async def role_page(board_id: str, slug: str) -> HTMLResponse:
    with _page(slug=slug):
        board = await _board(board_id)
        if board is None:
            return lost()
        if board["kind"] == "talent":
            ops.note(outcome="wrong_kind")
            return lost()
        job = next((j for j in board["jobs"] if j["slug"] == slug), None)
        if job is None:
            ops.note(outcome="not_listed")
            return lost()
        company = next(c for c in board["companies"] if c["slug"] == job["company_slug"])
        more = [j for j in company["jobs"] if j["slug"] != slug]
        desc = ". ".join(p for p in (job["short_location"], job["comp"],
                                     first_sentence(job["summary"])) if p)
        return render("role.html", board=board, job=job, company=company, more=more,
                      title=f"{job['title']} at {company['name']}", description=desc,
                      home=f"{base_path()}/{board['id']}", updated=_date(job["updated_at"]))


@router.get("/{board_id}/people/{slug}", response_class=HTMLResponse)
async def person_page(board_id: str, slug: str) -> HTMLResponse:
    with _page(slug=slug):
        board = await _board(board_id)
        if board is None:
            return lost()
        if board["kind"] == "jobs":
            ops.note(outcome="wrong_kind")
            return lost()
        person = next((t for t in board["talent"] if t["slug"] == slug), None)
        if person is None:
            ops.note(outcome="not_listed")
            return lost()
        more = [t for t in board["talent"] if t["slug"] != slug]
        desc = ". ".join(p for p in (person["headline"], person["location"],
                                     first_sentence(person["summary"])) if p)
        return render("person.html", board=board, person=person, more=more,
                      title=person["name"], description=desc,
                      home=f"{base_path()}/{board['id']}", updated=_date(person["updated_at"]))
