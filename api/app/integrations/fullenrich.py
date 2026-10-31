"""LinkedIn profiles via FullEnrich on Firecrawl Alexandria: structured data, no scraping, no LLM.

`fullenrich/people/match` resolves an exact LinkedIn URL for 5 credits; a miss costs nothing.
The record is mapped into `LinkedInProfile` deterministically.
"""

from typing import Any

from pydantic import BaseModel, Field

from app.integrations.firecrawl import AsyncFirecrawl
from app.urls import github_profile, is_social, normalize_url

PROVIDER = "fullenrich"
MATCH_CREDITS = 5      # listed price of people/match per record found


class Company(BaseModel):
    name: str = Field(description="Company name as written")
    linkedin_url: str | None = Field(None, description="linkedin.com/company/... if present")
    website: str | None = Field(None, description="Company website if the record states it")
    description: str | None = Field(None, description="The provider's own description")


class LinkedInProfile(BaseModel):
    """What the data-provider record says about the person."""

    full_name: str | None = Field(None, description="The profile's name as the provider has it")
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = Field(None, description="Profile headline / tagline, verbatim")
    location: str | None = None
    summary: str | None = Field(None, description="The profile's about text")
    current_title: str | None = None
    current_company: Company | None = Field(None, description="Current employer, if any")
    personal_sites: list[str] = Field(
        default_factory=list, description="Personal or portfolio websites listed on the profile "
                                          "(not the employer's site, not social networks)")
    github_url: str | None = Field(None, description="github.com/<user> if listed")
    other_links: list[str] = Field(default_factory=list,
                                   description="Other URLs on the profile (twitter, blog, ...)")


def profile_values(profile: LinkedInProfile, current: dict[str, Any]) -> dict[str, Any]:
    """What a matched profile adds to a record: the headline, the GitHub account, the location
    when the record has none, and the current company when it differs from the record's (the
    old one stays in history). `current` is the record's current values. A paid lookup should
    never be thrown away: every caller that matched a profile writes these."""
    values: dict[str, Any] = {}
    if profile.headline:
        values["linkedin_headline"] = " ".join(profile.headline.split())
    if g := github_profile(profile.github_url):
        values["github"] = g
    if profile.location and not current.get("location"):
        values["location"] = profile.location
    company = profile.current_company.name if profile.current_company else None
    now = current.get("company")
    if company and (not isinstance(now, str) or now.strip().lower() != company.strip().lower()):
        values["company"] = company
    return values


async def match(client: AsyncFirecrawl, linkedin_url: str) -> LinkedInProfile | None:
    """The profile for an exact LinkedIn URL, or None when no record matched."""
    res = await client.alexandria(PROVIDER, "people/match", {"linkedin_url": linkedin_url},
                                  expected_credits=MATCH_CREDITS)
    people = (res.data or {}).get("people") if isinstance(res.data, dict) else None
    return from_record(people[0]) if people else None


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
    )
