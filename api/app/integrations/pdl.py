"""People Data Labs via Firecrawl Alexandria: `people-data-labs/person/enrich` resolves a LinkedIn
profile URL for 1 credit (a miss is free) into the richest person record we can reach: headline,
summary, current job, the whole work history, education, skills, interests and languages. Mapped
into the shared `LinkedInProfile` deterministically; PDL lowercases names and titles, so those
are title-cased here (summary, skills and interests are kept as they come)."""

from typing import Any

from app.integrations.firecrawl import AsyncFirecrawl
from app.integrations.profile import Company, Education, LinkedInProfile, Position
from app.urls import github_profile, linkedin_profile, normalize_url

PROVIDER = "people-data-labs"
CAPABILITY = "person/enrich"
ENRICH_CREDITS = 1       # listed price per record found
MIN_LIKELIHOOD = 8       # a profile-URL match scores 10; anything weaker is not this person

_SMALL = {"of", "and", "the", "at", "in", "for", "on", "de", "da", "du", "von", "van", "der", "y"}


def _word(word: str, first: bool) -> str:
    if not first and word in _SMALL:
        return word
    if len(word) <= 2 and word.isalpha():       # td, it, hr, ai, vp: an acronym, not a word
        return word.upper()
    return word.capitalize()


def _title(value: Any) -> str | None:
    """PDL's all-lowercase names and titles in Title Case ("it service desk manager at td" →
    "IT Service Desk Manager at TD"); mixed-case text is left alone."""
    if isinstance(value, dict):
        value = value.get("name")
    text = " ".join(str(value).split()) if value not in (None, "") else ""
    if not text or text != text.lower():
        return text or None
    return " ".join(_word(w, i == 0) for i, w in enumerate(text.split(" ")))


def _names(values: Any) -> list[str]:
    out: list[str] = []
    for v in values if isinstance(values, list) else []:
        name = v.get("name") if isinstance(v, dict) else v
        if name not in (None, "") and str(name) not in out:
            out.append(str(name))
    return out


def _position(e: dict[str, Any]) -> Position:
    return Position(title=_title(e.get("title")), company=_title(e.get("company")),
                    start=e.get("start_date") or None, end=e.get("end_date") or None,
                    is_current=bool(e.get("is_primary")) or not e.get("end_date"))


def _education(e: dict[str, Any]) -> Education:
    return Education(school=_title(e.get("school")),
                     degree=_title(", ".join(_names(e.get("degrees")))),
                     field=_title(", ".join(_names(e.get("majors")))),
                     start=e.get("start_date") or None, end=e.get("end_date") or None)


def from_record(rec: dict[str, Any]) -> LinkedInProfile:
    """Map a PDL person record (the `data` object) to our profile model."""
    location = rec.get("location_name") or ", ".join(
        str(rec[k]) for k in ("location_locality", "location_region", "location_country")
        if rec.get(k)) or None
    company = None
    if rec.get("job_company_name"):
        company = Company(name=_title(rec["job_company_name"]) or str(rec["job_company_name"]),
                          linkedin_url=normalize_url(rec.get("job_company_linkedin_url")),
                          website=normalize_url(rec.get("job_company_website")))
    others: list[str] = []
    for raw in (rec.get("twitter_url"), rec.get("facebook_url"),
                *(p.get("url") for p in rec.get("profiles") or [] if isinstance(p, dict))):
        url = normalize_url(raw)
        if url and not linkedin_profile(url) and not github_profile(url) and url not in others:
            others.append(url)
    positions = [p for p in (_position(e) for e in rec.get("experience") or []
                             if isinstance(e, dict)) if p.title or p.company]
    positions.sort(key=lambda p: (p.is_current, p.start or ""), reverse=True)
    return LinkedInProfile(
        full_name=_title(rec.get("full_name")), first_name=_title(rec.get("first_name")),
        last_name=_title(rec.get("last_name")), headline=rec.get("headline") or None,
        location=_title(location), summary=rec.get("summary") or None,
        current_title=_title(rec.get("job_title")), current_company=company,
        github_url=github_profile(normalize_url(rec.get("github_url"))), other_links=others,
        experience=positions,
        education=[e for e in (_education(x) for x in rec.get("education") or []
                               if isinstance(x, dict)) if e.school or e.degree],
        skills=_names(rec.get("skills")),
        languages=[t for t in map(_title, _names(rec.get("languages"))) if t],
        interests=_names(rec.get("interests")),
    )


async def match(client: AsyncFirecrawl, linkedin_url: str) -> LinkedInProfile | None:
    """The PDL record behind an exact LinkedIn URL, or None when there is none."""
    res = await client.alexandria(PROVIDER, CAPABILITY,
                                  {"profile": linkedin_url, "min_likelihood": MIN_LIKELIHOOD},
                                  expected_credits=ENRICH_CREDITS)
    rec = res.data
    if isinstance(rec, dict) and isinstance(rec.get("data"), dict):   # PDL's own envelope
        rec = rec["data"]
    if not isinstance(rec, dict) or not (rec.get("full_name") or rec.get("experience")):
        return None
    return from_record(rec)
