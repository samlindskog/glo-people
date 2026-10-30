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


class LinkedInProfile(BaseModel):
    """What the LinkedIn data-provider record says about the person."""

    headline: str | None = Field(None, description="Profile headline / tagline, verbatim")
    location: str | None = None
    summary: str | None = Field(None, description="Two or three sentences about the person")
    current_company: Company | None = Field(
        None, description="Where the person works now (most recent position without an end "
                          "date). None if unclear.")
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


class CompanySummary(BaseModel):
    website: str | None = Field(None, description="The company's official website, or None")
    summary: str | None = Field(None, description="Two or three plain sentences: what the "
                                                  "company does, for whom, and anything notable "
                                                  "(stage, location). None if unresolved.")
    confidence: Literal["high", "medium", "low"] = Field(
        description="high: the page names the company and matches the person's context; "
                    "medium: likely but not corroborated; low: guess or no page found")
    evidence_url: str | None = Field(None, description="Page the summary is based on")
