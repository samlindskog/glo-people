"""The shape of a data-provider profile of a person, shared by the providers (FullEnrich today),
and how one is turned into record values: `profile_values` for the deterministic direct writes
(identifiers), `profile_lines` for the labelled lines the enrich assistant hands to the storage
agent. Pure data: no provider calls, no app-level decisions."""

import html
import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from app import fields
from app.urls import github_profile


class Company(BaseModel):
    name: str = Field(description="Company name as written")
    linkedin_url: str | None = Field(None, description="linkedin.com/company/... if present")
    website: str | None = Field(None, description="Company website if the record states it")
    description: str | None = Field(None, description="The provider's own description")


class Position(BaseModel):
    title: str | None = None
    company: str | None = None
    start: str | None = Field(None, description="ISO date or year, as the provider has it")
    end: str | None = None
    is_current: bool = False


class Education(BaseModel):
    school: str | None = None
    degree: str | None = None
    field: str | None = None
    start: str | None = None
    end: str | None = None


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
    experience: list[Position] = Field(default_factory=list, description="Most recent first")
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)


_NOT_A_NAME = re.compile(r"[|@/#&_(){}\[\]<>\d]|\bhttp")   # handles, titles, taglines, addresses


def _tokens(name: str) -> list[str]:
    return fields.fold(name).split()


def is_full_name(name: str) -> bool:
    """A first and a last name: two or more tokens, the last one a word of two or more letters,
    and none of the marks of a handle or tagline ("Polly I.", "Ada O", "Jordan | Cofounder | UXUY"
    and "Lena@Cohere" do not count)."""
    if _NOT_A_NAME.search(name or ""):
        return False
    toks = _tokens(name)
    return len(toks) >= 2 and len(toks[-1]) >= 2 and not toks[-1].isdigit()


def full_name(profile: LinkedInProfile | None) -> str | None:
    """The profile's first and last name, or None when it has none: the provider's full_name
    field when it is one, else first and last name joined (a provider may give "Jack" as the full
    name next to a last name of its own)."""
    if profile is None:
        return None
    candidates = [profile.full_name or "",
                  f"{profile.first_name or ''} {profile.last_name or ''}"]
    for raw in candidates:
        name = " ".join(raw.split())
        if is_full_name(name):
            return name
    return None


def name_fill(profile: LinkedInProfile, current: Any) -> str | None:
    """The matched profile's full name for a record whose name is missing or not a full name
    ("Alina" → "Alina Kharina"; "Fleek", a company name on a person's record, → the person behind
    the LinkedIn URL). A record that already has a first and last name keeps it: the profile's
    other facts are written anyway, so a wrong URL would show there, not in a renamed record."""
    full = full_name(profile)
    if not full or fields.fold(full) == fields.fold(str(current or "")):
        return None
    return full if not is_full_name(str(current or "")) else None


def profile_values(profile: LinkedInProfile, current: dict[str, Any]) -> dict[str, Any]:
    """What a matched profile adds to a record directly: the headline, the GitHub account, the
    location when the record has none, and the current company when it differs from the record's
    (the old one stays in history). `current` is the record's current values. A paid lookup should
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


Line = tuple[str, str | list[str]]

LIST_KEYS = ("skills", "languages", "interests")     # every one of these has a list spec


def _years(start: str | None, end: str | None, current: bool) -> str:
    """'2021–present', '2018–2021', a lone year, 'present', or '' when nothing is dated."""
    a, b = (start or "")[:4], (end or "")[:4]
    if current:
        b = "present"
    return f"{a}–{b}" if a and b else a or b


_INVISIBLE = str.maketrans({"\u2800": " ", "\u200b": "", "\u00a0": " ", "\ufeff": ""})


def _clean(text: str | None) -> str:
    """One line: HTML entities decoded ("&amp;"), braille blanks and zero-width characters (used
    as spacers on profiles) removed, whitespace collapsed."""
    return " ".join(html.unescape(str(text or "")).translate(_INVISIBLE).split())


def _language(name: str) -> str:
    """Language names as proper nouns: providers return "english" and "English" alike."""
    return name.capitalize() if name == name.lower() else name


def position_text(p: Position) -> str:
    head = " at ".join(s for s in (_clean(p.title), _clean(p.company)) if s)
    years = _years(p.start, p.end, p.is_current)
    return f"{head} ({years})" if head and years else head


def education_text(e: Education) -> str:
    degree = " ".join(s for s in (_clean(e.degree), _clean(e.field)) if s)
    head = ", ".join(s for s in (degree, _clean(e.school)) if s)
    years = _years(e.start, e.end, False)
    return f"{head} ({years})" if head and years else head


def _distinct(texts: Iterable[str]) -> list[str]:
    """Non-empty texts, first occurrence kept (providers repeat an entry now and then)."""
    return [t for t in dict.fromkeys(texts) if t]


def profile_lines(profile: LinkedInProfile) -> list[Line]:
    """The profile's facts as `(property key, value)` lines for the storage agent: the about text,
    the current title, the work history and education as one clause per entry, and the list
    properties. Empty values are skipped; the keys are all spec properties."""
    lines: list[Line] = []
    if about := _clean(profile.summary):
        lines.append(("about", about))
    if title := _clean(profile.current_title):
        lines.append(("title_enriched", title))
    if positions := _distinct(map(position_text, profile.experience)):
        lines.append(("experience", "; ".join(positions)))
    if schools := _distinct(map(education_text, profile.education)):
        lines.append(("education", "; ".join(schools)))
    for key in LIST_KEYS:
        if items := [_clean(v) for v in getattr(profile, key) if _clean(v)]:
            lines.append((key, [_language(v) for v in items] if key == "languages" else items))
    return lines
