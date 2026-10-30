"""Structured outputs of the enrichment agents and the workflow's options."""

from typing import Literal

from pydantic import BaseModel, Field


class EnrichOptions(BaseModel):
    mode: Literal["frugal", "thorough"] = Field(
        "frugal", description="frugal: only follow links the pages give us, no web searches "
                              "except for the company site; thorough: allow bounded searches "
                              "for a missing LinkedIn profile or GitHub account")
    refresh: bool = Field(False, description="Re-run for people enriched recently")
    max_credits: int = Field(1500, ge=1, le=100_000,
                             description="Firecrawl credit cap for the whole run")


class Company(BaseModel):
    name: str = Field(description="Company name as written")
    linkedin_url: str | None = Field(None, description="linkedin.com/company/... if present")
    website: str | None = Field(None, description="Company website if the record states it")
    description: str | None = Field(None, description="The provider's own description")


class LinkedInProfile(BaseModel):
    """What the data-provider record (FullEnrich) says about the person."""

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


class RelatedLink(BaseModel):
    url: str
    kind: Literal["github", "twitter", "blog", "newsletter", "portfolio", "company", "video",
                  "other"] = "other"


class SiteFindings(BaseModel):
    """What a personal website says about its owner."""

    is_personal: bool = Field(description="True if this is the person's own site (portfolio, "
                                          "blog, about page), False if it is a company or an "
                                          "unrelated site")
    github_urls: list[str] = Field(default_factory=list, description="github.com/<user> links")
    related_links: list[RelatedLink] = Field(default_factory=list,
                                             description="Outbound links about the person")
    one_liner: str | None = Field(None, description="One sentence describing the person, from "
                                                    "the site's own words")


class GitHubProfile(BaseModel):
    """What the GitHub data tool says about an account."""

    url: str
    login: str
    name: str | None = None
    bio: str | None = None
    company: str | None = None
    location: str | None = None
    blog: str | None = None
    followers: int = 0
    public_repos: int = 0
    top_repos: list[str] = Field(default_factory=list, description="'name (★stars)' entries")
    languages: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        bits = [b for b in (self.name, self.bio) if b]
        head = " — ".join(bits) if bits else self.login
        facts = [f"{self.public_repos} public repos", f"{self.followers} followers"]
        if self.company:
            facts.append(f"at {self.company}")
        out = f"{head}. {', '.join(facts)}."
        if self.top_repos:
            out += " Top repos: " + ", ".join(self.top_repos[:5]) + "."
        if self.languages:
            out += " Languages: " + ", ".join(self.languages[:5]) + "."
        return out


class CompanySummary(BaseModel):
    website: str | None = Field(None, description="The company's official website, or None")
    summary: str | None = Field(None, description="Two or three plain sentences: what the "
                                                  "company does, for whom, and anything notable "
                                                  "(stage, location). None if unresolved.")
    confidence: Literal["high", "medium", "low"] = Field(
        description="high: the page names the company and matches the person's context; "
                    "medium: likely but not corroborated; low: guess or no page found")
    evidence_url: str | None = Field(None, description="Page the summary is based on")
