"""The enrichment ladder: LinkedIn (FullEnrich via Alexandria) → personal sites → GitHub (github-com
via Alexandria) and links → company, one function per step, cheapest step first.

Every paid call is preceded by a free check; a missing input skips the step instead of searching
for it (frugal mode) or allows one bounded search (thorough mode). Nothing is overwritten: all
writes append history entries attributed to `enrich/<agent>`.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import logfire

from app.config import get_settings
from app.workflows import runner
from app.workflows.context import WorkflowDeps
from app.workflows.enrich import company as company_agent
from app.workflows.enrich import github, linkedin_reader, site_reader
from app.workflows.enrich.budget import MIN_PAGE_CHARS, caps
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
from app.workflows.firecrawl import (
    BudgetExceeded,
    FirecrawlError,
    NotConfigured,
    NotFound,
    TermsRequired,
)

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


async def _provider_call(deps: WorkflowDeps, st: State, step: str, call):
    """Run one Alexandria call with the ladder's error policy: a miss is `not_found`, unaccepted
    provider terms or a provider error are `error` (the rest of the ladder still runs)."""
    try:
        return await call()
    except NotFound:
        st.result[step] = "not_found"
        st.result["not_found"].append(step)
        deps.skip(step, "the data provider has no record")
    except BudgetExceeded as e:
        st.result[step] = "skipped"
        deps.skip(step, str(e))
    except TermsRequired as e:
        st.result[step] = "error"
        deps.skip(step, f"{e}" + (f" Accept them at {e.url}" if e.url else ""))
    except NotConfigured:
        raise
    except FirecrawlError as e:
        st.result[step] = "error"
        deps.skip(step, str(e))
    return None


async def _step_linkedin(deps: WorkflowDeps, st: State) -> None:
    """Step 1: the person's FullEnrich record (exact LinkedIn URL, 5 credits; a miss is free)."""
    li = linkedin_profile(deps.current("linkedin"))
    company_domain = host_of(deps.current("company_website")) if isinstance(
        deps.current("company_website"), str) else ""
    if not li and not (st.options.mode == "thorough" and st.name and company_domain):
        deps.skip("linkedin", "no linkedin.com/in URL on the record" + st.frugal_note)
        return
    if "linkedin" in st.previously_missing:
        deps.skip("linkedin", "no provider record last time; not retried within the TTL")
        st.result["linkedin"] = "not_found"
        return
    profile = None
    with logfire.span("linkedin_reader"):
        if li:
            found = await _provider_call(deps, st, "linkedin",
                                         lambda: linkedin_reader.match(deps, li))
            if found:
                profile, st.result["linkedin_provider"] = found
        if profile is None and st.options.mode == "thorough" and st.name and company_domain \
                and st.result.get("linkedin") not in ("error",):
            profile = await _provider_call(
                deps, st, "linkedin", lambda: linkedin_reader.lookup(deps, st.name, company_domain))
            if profile:
                st.result["linkedin_provider"] = linkedin_reader.PROVIDER
    if profile is None:
        if st.result["linkedin"] == "absent":
            st.result["linkedin"] = "not_found"
            st.result["not_found"].append("linkedin")
            deps.skip("linkedin", "no data provider has a record for this profile")
        return
    st.result["linkedin"] = "matched"
    st.profile = profile
    github_url = github_profile(profile.github_url)
    if github_url:
        st.github_urls.append(github_url)
    st.related.extend(profile.other_links)
    values: dict[str, Any] = {"linkedin_headline": _clip(profile.headline, MAX_HEADLINE_CHARS),
                              "github": github_url}
    if not deps.current("location") and profile.location:
        values["location"] = profile.location
    company = profile.current_company.name if profile.current_company else None
    current_company = deps.current("company")
    if company and (not isinstance(current_company, str)
                    or current_company.strip().lower() != company.strip().lower()):
        values["company"] = company        # a new employer: appended, the old one stays in history
    await deps.write(linkedin_reader.AGENT_NAME, values)


def split_urls(value: Any) -> list[str]:
    """A field may hold several URLs in one string ("a.com , b.com"), a list, or one URL."""
    if isinstance(value, list):
        return [u for v in value for u in split_urls(v)]
    if not isinstance(value, str):
        return []
    return [p for p in re.split(r"[\s,;|]+", value) if p and "." in p]


def _site_candidates(deps: WorkflowDeps, st: State) -> list[str]:
    raws = [*(st.profile.personal_sites if st.profile else []),
            *split_urls(deps.current("website")),
            *(u for v in deps.history_values("website") for u in split_urls(v))]
    out: list[str] = []
    for raw in raws:
        n = normalize_url(raw)
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
        with logfire.span("site_reader {site}", site=site):
            findings = await site_reader.read(deps, st.name, site, page.markdown, page.links,
                                              st.caps["follow"])
        if not findings.is_personal:
            # A company or unrelated site: its links are about the company, not the person.
            deps.skip("sites", f"{site}: not the person's own site")
            continue
        if not _has_url(deps, "website", site):
            await deps.write(site_reader.AGENT_NAME, {"website": site})
        st.github_urls += [g for g in map(github_profile, [*page.links, *findings.github_urls])
                           if g]
        st.related += [link.url for link in findings.related_links]


async def _step_github_and_links(deps: WorkflowDeps, st: State) -> None:
    """Step 3: GitHub and related links come only from what the record and pages gave us (no
    searching); a known GitHub account is summarised through the github-com data tool."""
    github_urls = list(dict.fromkeys(g for g in st.github_urls if g))
    current_github = github_profile(deps.current("github")) if isinstance(
        deps.current("github"), str) else None
    if not github_urls and current_github:
        github_urls = [current_github]
    if github_urls:
        if not _has_url(deps, "github", github_urls[0]):
            await deps.write(site_reader.AGENT_NAME, {"github": github_urls[0]})
        await _github_facts(deps, st, github_urls[0])
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


async def _github_facts(deps: WorkflowDeps, st: State, github_url: str) -> None:
    if deps.current("github_summary") and url_key(deps.current("github")) == url_key(github_url) \
            and not st.options.refresh:
        deps.skip("github", "already summarised for this account")
        return
    with logfire.span("github {url}", url=github_url):
        found = await _provider_call(deps, st, "github",
                                     lambda: github.lookup(deps, github_url))
    if not found:
        return
    st.result["github"] = "matched"
    values: dict[str, Any] = {"github_summary": _clip(found.summary(), MAX_SUMMARY_CHARS)}
    if found.blog and not is_social(found.blog) and is_html_candidate(found.blog) \
            and not _has_url(deps, "website", found.blog):
        values["website"] = found.blog       # the account's blog field is usually the personal site
    await deps.write(github.AGENT_NAME, values)


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
    if st.profile and st.profile.current_company and st.profile.current_company.description:
        context += f". The data provider describes the company as: " \
                   f"{_clip(st.profile.current_company.description, 400)}"
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
