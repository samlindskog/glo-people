"""LinkedIn profile via FullEnrich (Firecrawl Alexandria): structured data, no scraping, no LLM.

`fullenrich/people/match` resolves an exact LinkedIn URL for 5 credits; `people/lookup` (10
credits) resolves a full name plus the employer's domain and is only used in thorough mode.
A miss costs nothing. The record is mapped into `LinkedInProfile` deterministically.
"""

from typing import Any

from app.workflows.context import WorkflowDeps
from app.workflows.enrich.budget import FULLENRICH_LOOKUP_CREDITS, FULLENRICH_MATCH_CREDITS
from app.workflows.enrich.models import Company, LinkedInProfile
from app.workflows.enrich.urls import github_profile, is_social, normalize_url

AGENT_NAME = "linkedin_reader"
PROVIDER = "fullenrich"


async def match(deps: WorkflowDeps, linkedin_url: str) -> tuple[LinkedInProfile | None, str]:
    """(profile, provider) for an exact LinkedIn URL; profile is None when no record matched."""
    res = await deps.firecrawl.alexandria(PROVIDER, "people/match", {"linkedin_url": linkedin_url},
                                          expected_credits=FULLENRICH_MATCH_CREDITS)
    return _first(res.data), PROVIDER


async def lookup(deps: WorkflowDeps, name: str, company_domain: str) -> LinkedInProfile | None:
    """Thorough mode: resolve a person from their name and employer domain."""
    res = await deps.firecrawl.alexandria(PROVIDER, "people/lookup", {
        "person_name": name, "company_domain": company_domain},
        expected_credits=FULLENRICH_LOOKUP_CREDITS)
    return _first(res.data)


def _first(data: Any) -> LinkedInProfile | None:
    people = (data or {}).get("people") if isinstance(data, dict) else None
    if not people:
        return None
    return from_record(people[0])


def from_record(rec: dict[str, Any]) -> LinkedInProfile:
    """Map a FullEnrich person record to our profile model."""
    loc = rec.get("location") or {}
    location = ", ".join(str(loc[k]) for k in ("city", "region", "country") if loc.get(k)) or None
    employment = rec.get("employment") or {}
    cur = employment.get("current") or next(
        (e for e in employment.get("all") or [] if e.get("is_current")), None) or {}
    company = None
    if cur.get("company", {}).get("name"):
        c = cur["company"]
        li = ((c.get("social_profiles") or {}).get("professional_network") or {}).get("url")
        company = Company(name=str(c["name"]), linkedin_url=li,
                          website=normalize_url(c.get("website") or c.get("domain")),
                          description=c.get("description"))
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
        headline=rec.get("headline") or None,
        location=location,
        summary=rec.get("description") or None,
        current_title=cur.get("title") or None,
        current_company=company,
        personal_sites=sites,
        github_url=github_url,
        other_links=others,
    )
