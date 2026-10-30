"""The enrichment ladder: LinkedIn → personal sites → GitHub and links → company, one function per
step, cheapest step first.

Every paid call is preceded by a free check; a missing input skips the step instead of searching
for it (frugal mode) or allows one bounded search (thorough mode). Nothing is overwritten: all
writes append history entries attributed to `enrich/<agent>`.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import logfire

from app.config import get_settings
from app.workflows import runner
from app.workflows.context import WorkflowDeps
from app.workflows.enrich import company as company_agent
from app.workflows.enrich import linkedin_reader, site_reader
from app.workflows.enrich.budget import LINKEDIN_EXPECTED_CREDITS, MIN_PAGE_CHARS, caps
from app.workflows.enrich.models import EnrichOptions, LinkedInProfile
from app.workflows.enrich.urls import (
    github_profile,
    host_mentions,
    host_of,
    is_html_candidate,
    is_social,
    linkedin_profile,
    normalize_url,
    url_key,
)
from app.workflows.firecrawl import BudgetExceeded, FirecrawlError, NotConfigured, NotFound

log = logging.getLogger(__name__)
WORKFLOW = "enrich"
MAX_RELATED_LINKS = 50        # a person record is not a link directory
MAX_HEADLINE_CHARS = 300
MAX_SUMMARY_CHARS = 1200


@dataclass
class State:
    """What the steps hand each other."""

    options: EnrichOptions
    caps: dict
    name: str
    previously_missing: set[str]
    profile: LinkedInProfile | None = None
    github_urls: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=lambda: {
        "linkedin": "absent", "company": "absent", "not_found": []})

    @property
    def frugal_note(self) -> str:
        return "" if self.options.mode == "thorough" else "; frugal mode does not search"


def _has_url(deps: WorkflowDeps, key: str, url: str) -> bool:
    wanted = url_key(url)
    return any(url_key(v) == wanted for v in deps.history_values(key) if isinstance(v, str))


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


async def run_item(deps: WorkflowDeps) -> dict[str, Any]:
    options: EnrichOptions = deps.options
    skip = await _recently_enriched(deps, options)
    if skip is not None:
        return skip
    last = await runner.last_done_item(WORKFLOW, deps.person_id)
    st = State(options=options, caps=caps(options), name=str(deps.current("name") or ""),
               previously_missing=set((last or {}).get("result", {}).get("not_found", []))
               if last and not options.refresh else set())
    await _step_linkedin(deps, st)
    await _step_sites(deps, st)
    await _step_github_and_links(deps, st)
    await _step_company(deps, st)
    return {**st.result, "written": deps.written, "skipped": deps.skipped}


async def _recently_enriched(deps: WorkflowDeps, options: EnrichOptions) -> dict | None:
    """Step 0: a person enriched inside the TTL is skipped without spending anything."""
    if options.refresh:
        return None
    last = await runner.last_done_item(WORKFLOW, deps.person_id)
    if not last or not last.get("finished_at"):
        return None
    ttl = timedelta(days=get_settings().enrich_ttl_days)
    if datetime.fromisoformat(last["finished_at"]) <= datetime.now(UTC) - ttl:
        return None
    deps.skip("all", f"enriched {last['finished_at'][:10]}; use refresh to run again")
    return {"linkedin": "absent", "company": "absent", "not_found": [], "status": "skipped",
            "skipped": deps.skipped}


async def _step_linkedin(deps: WorkflowDeps, st: State) -> None:
    """Step 1: the provider record for the person's LinkedIn URL, read by one agent call."""
    li = linkedin_profile(deps.current("linkedin"))
    if not li:
        deps.skip("linkedin", "no linkedin.com/in URL on the record" + st.frugal_note)
        return
    if "linkedin" in st.previously_missing:
        deps.skip("linkedin", "no provider record last time; not retried within the TTL")
        st.result["linkedin"] = "not_found"
        return
    try:
        page = await deps.firecrawl.scrape(li, formats=("markdown",),
                                           expected_credits=LINKEDIN_EXPECTED_CREDITS)
    except NotFound:
        st.result["linkedin"] = "not_found"
        st.result["not_found"].append("linkedin")
        deps.skip("linkedin", "no data provider has a record for this profile")
        return
    except BudgetExceeded as e:
        deps.skip("linkedin", str(e))
        return
    except NotConfigured:
        raise
    except FirecrawlError as e:
        if "[exchange]" in str(e):
            # The data-provider route ran and came back empty-handed (Firecrawl reports this as a
            # 500 rather than THIRD_PARTY_DATA_NOT_FOUND): same outcome as "no record".
            st.result["linkedin"] = "not_found"
            st.result["not_found"].append("linkedin")
            deps.skip("linkedin", "the enrichment providers could not resolve this profile")
            return
        # e.g. 403 "Enrichment is not enabled for person profiles": the rest of the ladder still
        # has the record's own website and company to work with.
        st.result["linkedin"] = "error"
        deps.skip("linkedin", str(e))
        return
    st.result["linkedin"] = "matched"
    st.result["linkedin_provider"] = page.provider
    if len(page.markdown) < MIN_PAGE_CHARS:
        deps.skip("linkedin", "provider record too short to read")
        return
    with logfire.span("linkedin_reader"):
        st.profile = await linkedin_reader.read(page.markdown)
    github = github_profile(st.profile.github_url)
    if github:
        st.github_urls.append(github)
    st.related.extend(st.profile.other_links)
    await deps.write(linkedin_reader.AGENT_NAME, {
        "linkedin_headline": _clip(st.profile.headline, MAX_HEADLINE_CHARS),
        "github": github,
    })


def _site_candidates(deps: WorkflowDeps, st: State) -> list[str]:
    raws = [*(st.profile.personal_sites if st.profile else []), deps.current("website"),
            *deps.history_values("website")]
    out: list[str] = []
    for raw in raws:
        n = normalize_url(raw) if isinstance(raw, str) else None
        if n and is_html_candidate(n) and not is_social(n) and url_key(n) not in map(url_key, out):
            out.append(n)
    return out


async def _step_sites(deps: WorkflowDeps, st: State) -> None:
    """Step 2: read the person's own site(s): the profile's, plus any already on the record."""
    candidates = _site_candidates(deps, st)
    if not candidates:
        deps.skip("sites", "no personal website known")
        return
    for site in candidates[:st.caps["sites"]]:
        try:
            page = await deps.firecrawl.scrape(site, formats=("markdown", "links"))
        except NotFound:
            deps.skip("sites", f"{site}: not found")
            continue
        except BudgetExceeded as e:
            deps.skip("sites", str(e))
            return
        except NotConfigured:
            raise
        except FirecrawlError as e:
            deps.skip("sites", f"{site}: {e}")
            continue
        if (page.status_code and page.status_code >= 400) or len(page.markdown) < MIN_PAGE_CHARS:
            deps.skip("sites", f"{site}: HTTP {page.status_code}, {len(page.markdown)} chars")
            continue
        st.github_urls += [g for g in map(github_profile, page.links) if g]
        with logfire.span("site_reader {site}", site=site):
            findings = await site_reader.read(deps, st.name, site, page.markdown, page.links,
                                              st.caps["follow"])
        if findings.is_personal and not _has_url(deps, "website", site):
            await deps.write(site_reader.AGENT_NAME, {"website": site})
        st.github_urls += [g for g in map(github_profile, findings.github_urls) if g]
        st.related += [link.url for link in findings.related_links]


async def _step_github_and_links(deps: WorkflowDeps, st: State) -> None:
    """Step 3: GitHub and related links come only from what the pages gave us (no searching)."""
    github_urls = list(dict.fromkeys(g for g in st.github_urls if g))
    if github_urls:
        if not _has_url(deps, "github", github_urls[0]):
            await deps.write(site_reader.AGENT_NAME, {"github": github_urls[0]})
    else:
        st.result["not_found"].append("github")
        deps.skip("github", "no GitHub link on the profile or sites" + st.frugal_note)
    exclude = {url_key(deps.current("website")), *map(url_key, github_urls)}
    links = [n for n in map(normalize_url, st.related)
             if n and not linkedin_profile(n) and url_key(n) not in exclude]
    if not links:
        return
    current = deps.current("related_links")
    current = current if isinstance(current, list) else []
    merged = list(dict.fromkeys([*current, *links]))[:MAX_RELATED_LINKS]
    if len(merged) > len(current):
        await deps.write(site_reader.AGENT_NAME, {"related_links": merged})


def _company_candidate(deps: WorkflowDeps, st: State, company: str) -> str | None:
    """The cheapest plausible company website: profile → record → the person's own domain."""
    if st.profile and st.profile.current_company and st.profile.current_company.website:
        return normalize_url(st.profile.current_company.website)
    if isinstance(deps.current("company_website"), str):
        return normalize_url(deps.current("company_website"))
    site = deps.current("website")
    if isinstance(site, str) and host_mentions(host_of(site), company):
        return normalize_url(site)
    return None


async def _step_company(deps: WorkflowDeps, st: State) -> None:
    """Step 4: resolve and summarise the current company; low confidence writes nothing."""
    company = (st.profile.current_company.name if st.profile and st.profile.current_company
               else None) or deps.current("company")
    if not company:
        deps.skip("company", "no current company known")
        return
    company = str(company)
    if "company" in st.previously_missing:
        deps.skip("company", "unresolved last time; not retried within the TTL")
        st.result["company"] = "unresolved"
        return
    context = ", ".join(str(x) for x in (deps.current("location"), deps.current("sectors"),
                                          deps.current("role")) if x)
    try:
        with logfire.span("company {company}", company=company):
            summary = await company_agent.research(
                deps, company, _company_candidate(deps, st, company), context,
                allow_search=deps.budget.searches < deps.budget.max_searches,
                pages_cap=st.caps["company_pages"])
    except BudgetExceeded as e:
        deps.skip("company", str(e))
        return
    if summary.confidence == "low" or not summary.website:
        st.result["company"] = "unresolved"
        st.result["not_found"].append("company")
        deps.skip("company", f"could not corroborate a website for {company!r}")
        return
    st.result["company"] = "resolved"
    # Kept on the run item (not the person) so a reviewer can see what the summary rests on.
    st.result["company_evidence"] = {"confidence": summary.confidence,
                                     "evidence_url": summary.evidence_url}
    website = normalize_url(summary.website)
    values: dict[str, Any] = {"company_website": website}
    # A reworded summary of the same website is not new information: only write one when the
    # record has none yet or the company website changed.
    same_site = url_key(deps.current("company_website")) == url_key(website)
    if not (same_site and deps.current("company_summary")):
        values["company_summary"] = _clip(summary.summary, MAX_SUMMARY_CHARS)
    else:
        deps.skip("company", "summary kept: same website as before")
    if not deps.current("company"):
        values["company"] = company
    await deps.write(company_agent.AGENT_NAME, values)
