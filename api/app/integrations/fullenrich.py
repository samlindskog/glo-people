"""LinkedIn profiles via FullEnrich on Firecrawl Alexandria: structured data, no scraping, no LLM.

`fullenrich/people/match` resolves an exact LinkedIn URL for 5 credits; a miss costs nothing.
The whole record is mapped into the shared `LinkedInProfile` deterministically: identity, headline,
location, current position, the work history, education, languages and skills, and the links.
"""

from typing import Any

from app.integrations.firecrawl import AsyncFirecrawl
from app.integrations.profile import (  # noqa: F401  (re-exported for the callers)
    Company,
    Education,
    LinkedInProfile,
    Position,
    profile_lines,
    profile_values,
)
from app.urls import github_profile, is_social, normalize_url

PROVIDER = "fullenrich"
MATCH_CREDITS = 5      # listed price of people/match per record found


async def match(client: AsyncFirecrawl, linkedin_url: str) -> LinkedInProfile | None:
    """The profile for an exact LinkedIn URL, or None when no record matched."""
    res = await client.alexandria(PROVIDER, "people/match", {"linkedin_url": linkedin_url},
                                  expected_credits=MATCH_CREDITS)
    people = (res.data or {}).get("people") if isinstance(res.data, dict) else None
    return from_record(people[0]) if people else None


def _pick(d: Any, *keys: str) -> Any:
    """The first present key of a provider object (their field names vary between tools)."""
    if not isinstance(d, dict):
        return None
    return next((d[k] for k in keys if d.get(k) not in (None, "", [], {})), None)


def _name(v: Any) -> str | None:
    """A provider value that is a string or an object with a name."""
    if isinstance(v, dict):
        v = _pick(v, "name", "title", "label")
    return str(v) if v not in (None, "") else None


def _names(values: Any) -> list[str]:
    return [n for n in map(_name, values if isinstance(values, list) else []) if n]


def _position(e: dict[str, Any]) -> Position:
    return Position(title=_name(_pick(e, "title", "position", "role")),
                    company=_name(_pick(e, "company", "company_name", "organization")),
                    start=_name(_pick(e, "start_date", "start", "started_at", "from")),
                    end=_name(_pick(e, "end_date", "end", "ended_at", "to")),
                    is_current=bool(e.get("is_current")))


def _education(e: dict[str, Any]) -> Education:
    return Education(school=_name(_pick(e, "school", "school_name", "institution", "university")),
                     degree=_name(_pick(e, "degree", "degree_name")),
                     field=_name(_pick(e, "field_of_study", "field", "major")),
                     start=_name(_pick(e, "start_date", "start", "from")),
                     end=_name(_pick(e, "end_date", "end", "to")))


def from_record(rec: dict[str, Any]) -> LinkedInProfile:
    """Map a FullEnrich person record to our profile model."""
    loc = rec.get("location") or {}
    location = ", ".join(str(loc[k]) for k in ("city", "region", "country") if loc.get(k)) or None
    employment = rec.get("employment") or {}
    history = [e for e in employment.get("all") or [] if isinstance(e, dict)]
    cur = employment.get("current") or next((e for e in history if e.get("is_current")), None) or {}
    company = None
    if cur.get("company", {}).get("name"):
        c = cur["company"]
        li = ((c.get("social_profiles") or {}).get("professional_network") or {}).get("url")
        company = Company(name=str(c["name"]), linkedin_url=li,
                          website=normalize_url(c.get("website") or c.get("domain")),
                          description=c.get("description"))
    if cur and not any(e is cur or (e.get("title") == cur.get("title") and e.get("is_current"))
                       for e in history):
        history.insert(0, cur)
    github_url, sites, others = None, [], []
    for key, prof in (rec.get("social_profiles") or {}).items():
        url = normalize_url((prof or {}).get("url") if isinstance(prof, dict) else prof)
        if not url or key == "professional_network":
            continue
        if g := github_profile(url):
            github_url = github_url or g
        elif is_social(url):
            others.append(url)
        else:
            sites.append(url)
    return LinkedInProfile(
        full_name=(rec.get("full_name") or " ".join(
            str(rec[k]) for k in ("first_name", "last_name") if rec.get(k)) or None),
        first_name=rec.get("first_name") or None,
        last_name=rec.get("last_name") or None,
        headline=rec.get("headline") or None,
        location=location,
        summary=rec.get("description") or None,
        current_title=cur.get("title") or None,
        current_company=company,
        personal_sites=sites,
        github_url=github_url,
        other_links=others,
        experience=[p for p in map(_position, history) if p.title or p.company],
        education=[e for e in map(_education, rec.get("educations") or [])
                   if isinstance(e, Education) and (e.school or e.degree)],
        skills=_names(rec.get("skills")),
        languages=_names(rec.get("languages")),
        interests=_names(_pick(rec, "interests", "hobbies")),
    )
