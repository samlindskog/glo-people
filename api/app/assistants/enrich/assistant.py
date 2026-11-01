"""Assistant (level 3): the enrichment ladder — LinkedIn (FullEnrich) → personal sites → GitHub
and links → company → storage — one function per step, cheapest step first, using the site,
company and store tools and the data providers.

Every paid call is preceded by a free check and a missing input skips the step; nothing is
searched for when the record gives no lead. Identifiers and our own tools' outputs are written
directly; everything prose- or tag-like the sources return (about text, title, work history,
education, skills, languages, the site's one-liner) is collected into one labelled passage that
the last step hands to the storage agent once, so it lands in the right properties with
restatements confirmed and conflicts marked. Nothing is overwritten: all writes append history
entries attributed to `enrich/<agent>`. Per person it returns text for the run table plus the
changes it caused.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import logfire
from pydantic import BaseModel

from app import fields
from app.assistants import runner
from app.assistants.base import StopRun
from app.assistants.context import AssistantDeps
from app.assistants.enrich.budget import CAPS, MIN_PAGE_CHARS, budget_for
from app.assistants.enrich.models import EnrichOptions
from app.assistants.enrich.passage import Passage
from app.assistants.enrich.providers import PROVIDERS
from app.auth import Principal
from app.config import get_settings
from app.integrations import github
from app.integrations.firecrawl import (
    AsyncFirecrawl,
    BudgetExceeded,
    CreditsExhausted,
    FirecrawlError,
    NotConfigured,
    NotFound,
    TermsRequired,
)
from app.integrations.profile import LinkedInProfile, name_fill, profile_lines, profile_values
from app.tools import company as company_tool
from app.tools import site as site_tool
from app.tools import store as store_tool
from app.urls import (
    github_profile,
    host_mentions,
    host_of,
    is_html_candidate,
    is_social,
    linkedin_profile,
    normalize_url,
    url_key,
)

log = logging.getLogger(__name__)
ASSISTANT = "enrich"
# Decided deterministically from the same provider record by the direct writes (or by the
# repair tool): the storage agent may confirm them but never derives them from the passage.
LOCKED = frozenset({"name", "company", "linkedin", "linkedin_headline", "location", "record_kind",
                    "company_description", "github_summary", "related_links"})
MAX_RELATED_LINKS = 50        # a person record is not a link directory


async def make_deps(run_id: int, item: dict, options: BaseModel,
                    principal: Principal | None) -> AssistantDeps:
    budget = budget_for(options)
    return AssistantDeps(firecrawl=AsyncFirecrawl(budget=budget), assistant=ASSISTANT, run_id=run_id,
                        item_id=item["id"], person=item["person"], label=item.get("label"),
                        principal=principal, budget=budget, options=options)


async def run_item(deps: AssistantDeps) -> dict[str, Any]:
    try:
        return await _ladder(deps)
    except CreditsExhausted as e:           # the Firecrawl account itself is empty
        raise StopRun(str(e)) from e


@dataclass
class State:
    """What the steps hand each other."""

    options: EnrichOptions
    name: str
    previously_missing: set[str]
    profile: LinkedInProfile | None = None
    github_urls: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    passage: Passage = field(default_factory=Passage)   # what the storage step files
    redo: bool = False      # an earlier attempt already ran the ladder to the end for this person
    result: dict[str, Any] = field(default_factory=lambda: {
        "linkedin": "absent", "company": "absent", "storage": "skipped", "not_found": [],
        "providers": {}})


def _has_url(deps: AssistantDeps, key: str, url: str) -> bool:
    wanted = url_key(url)
    return any(url_key(v) == wanted for v in deps.history_values(key) if isinstance(v, str))


def _clip(text: str | None, limit: int = fields.MAX_TEXT_CHARS) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def split_urls(value: Any) -> list[str]:
    """A field may hold several URLs in one string ("a.com , b.com"), a list, or one URL."""
    if isinstance(value, list):
        return [u for v in value for u in split_urls(v)]
    if not isinstance(value, str):
        return []
    return [p for p in re.split(r"[\s,;|]+", value) if p and "." in p]


async def _ladder(deps: AssistantDeps) -> dict[str, Any]:
    options: EnrichOptions = deps.options
    skip = await _recently_enriched(deps, options)
    if skip is not None:
        return skip
    last = await runner.last_done_item(ASSISTANT, deps.person_id)
    st = State(options=options, name=str(deps.current("name") or ""),
               previously_missing=set((last or {}).get("result", {}).get("not_found", []))
               if last and not options.refresh else set())
    await _step_linkedin(deps, st)
    await _step_sites(deps, st)
    await _step_github_and_links(deps, st)
    await _step_company(deps, st)
    await _step_store(deps, st)
    return {**st.result, "text": _text(st, deps), "changes": [c.model_dump() for c in deps.changes],
            "skipped": deps.skipped}


_STEP_TEXT = {
    ("linkedin", "matched"): "LinkedIn profile matched",
    ("linkedin", "not_found"): "no LinkedIn record at the data provider",
    ("linkedin", "absent"): "no LinkedIn URL on the record",
    ("linkedin", "error"): "the LinkedIn lookup failed",
    ("linkedin", "skipped"): "LinkedIn skipped",
    ("github", "matched"): "GitHub account summarised",
    ("company", "resolved"): "company website resolved and summarised",
    ("company", "unresolved"): "company not resolved",
    ("storage", "stored"): "facts filed through the storage agent",
    ("storage", "unchanged"): "the same facts were filed before",
    ("storage", "nothing"): "the storage agent filed nothing",
    ("storage", "error"): "the storage agent failed",
}


def _text(st: State, deps: AssistantDeps) -> str:
    """One sentence for the run table: what happened and what was written."""
    parts = [_STEP_TEXT[(step, st.result[step])]
             for step in ("linkedin", "github", "company", "storage")
             if (step, st.result.get(step)) in _STEP_TEXT]
    written = sorted(k for k in deps.written if k not in store_tool.AUDIT_KEYS)
    parts.append(f"wrote {', '.join(written)}" if written else "nothing new to write")
    return "; ".join(parts).capitalize() + "."


def _recent(deps: AssistantDeps, key: str, agent: str) -> bool:
    """An entry of `key` written by this assistant's `agent` within the TTL: a step that already
    ran for this person (an earlier attempt was interrupted), not worth paying for again."""
    ttl = datetime.now(UTC) - timedelta(days=get_settings().enrich_ttl_days)
    for entry in ((deps.person or {}).get("history") or {}).get(key, []):
        src = entry.get("src") or {}
        if src.get("by") == f"{ASSISTANT}/{agent}" and entry.get("ts") \
                and datetime.fromisoformat(entry["ts"]) >= ttl:
            return True
    return False


async def _recently_enriched(deps: AssistantDeps, options: EnrichOptions) -> dict | None:
    """Step 0: a person enriched inside the TTL is skipped without spending anything."""
    if options.refresh:
        return None
    last = await runner.last_done_item(ASSISTANT, deps.person_id)
    if not last or not last.get("finished_at"):
        return None
    ttl = timedelta(days=get_settings().enrich_ttl_days)
    if datetime.fromisoformat(last["finished_at"]) <= datetime.now(UTC) - ttl:
        return None
    deps.skip("all", f"enriched {last['finished_at'][:10]}; use refresh to run again")
    return {"linkedin": "absent", "company": "absent", "not_found": [], "status": "skipped",
            "text": f"Enriched on {last['finished_at'][:10]}; not run again within the TTL.",
            "changes": [], "skipped": deps.skipped}


async def _provider_call(deps: AssistantDeps, step: str, call, who: str = "the data provider"):
    """Run one data-provider call with the ladder's error policy: `(result, "ok")`, or None with
    the status: a miss is `not_found`, an exhausted item budget `skipped`, unaccepted provider
    terms or a provider error `error` (the rest of the ladder still runs)."""
    try:
        return await call(), "ok"
    except NotFound:
        deps.skip(step, f"{who} has no record")
        return None, "not_found"
    except BudgetExceeded as e:
        deps.skip(step, str(e))
        return None, "skipped"
    except TermsRequired as e:
        deps.skip(step, f"{who}: {e}" + (f" Accept them at {e.url}" if e.url else ""))
        return None, "error"
    except NotConfigured:
        raise
    except FirecrawlError as e:
        deps.skip(step, f"{who}: {e}")
        return None, "error"


async def _step_linkedin(deps: AssistantDeps, st: State) -> None:
    """Step 1: the person's record at each data provider the run names, in order (exact LinkedIn
    URL; FullEnrich 5 credits; a miss is free). The first match is the profile the direct writes
    and the later steps rest on; every match adds its facts to the passage."""
    li = linkedin_profile(deps.current("linkedin"))
    if not li:
        deps.skip("linkedin", "no linkedin.com/in URL on the record")
        return
    if not st.options.refresh and _recent(deps, "linkedin_headline", "linkedin_reader") \
            and _recent(deps, "enrichment_passage", "storage"):
        # Both the headline and the filed passage are on the record: the earlier attempt ran the
        # ladder to its end. (Interrupted between the two, the lookup is paid once more so the
        # facts get filed.)
        deps.skip("linkedin", "already read and filed within the TTL (an earlier attempt)")
        st.result["linkedin"] = "skipped"
        st.redo = True
        return
    if "linkedin" in st.previously_missing:
        deps.skip("linkedin", "no provider record last time; not retried within the TTL")
        st.result["linkedin"] = "not_found"
        return
    statuses: dict[str, str] = st.result["providers"]
    for spec in (PROVIDERS[k] for k in dict.fromkeys(st.options.providers)):
        deps.emit("step", f"LinkedIn: asking {spec.label} about {li}")
        with logfire.span("linkedin_reader {provider}", provider=spec.key):
            profile, statuses[spec.key] = await _provider_call(
                deps, "linkedin", lambda match=spec.match: match(deps.firecrawl, li), spec.label)
        if profile is None:
            if statuses[spec.key] == "ok":          # a clean miss: the provider has no record
                statuses[spec.key] = "not_found"
                deps.skip("linkedin", f"{spec.label} has no record for this profile")
            continue
        statuses[spec.key] = "matched"
        st.passage.add(spec.source, "LinkedIn profile", profile_lines(profile))
        if st.profile is None:
            st.profile = profile
            st.result["linkedin"] = "matched"
            st.result["linkedin_provider"] = spec.key
            if github_url := github_profile(profile.github_url):
                st.github_urls.append(github_url)
            st.related.extend(profile.other_links)
            values = profile_values(profile, deps.person or {})
            if name := name_fill(profile, deps.current("name")):
                values["name"] = name           # "Alina" → the profile's "Alina Kharina"
                st.name = name
            await deps.write("linkedin_reader", values)
    if st.profile is None:
        # the step's status is the worst of the providers': an error beats a miss
        st.result["linkedin"] = ("error" if "error" in statuses.values() else
                                 "skipped" if "skipped" in statuses.values() else "not_found")
        if st.result["linkedin"] == "not_found":
            st.result["not_found"].append("linkedin")


def _site_candidates(deps: AssistantDeps, st: State) -> list[str]:
    """The profile's personal sites plus every website on the record, except the ones the company
    step itself wrote (a company site is not the person's own; reading it again on a refresh
    would cost a page and a model call for nothing)."""
    company_sites = {url_key(e["v"]) for e in ((deps.person or {}).get("history") or {})
                     .get("website", []) if isinstance(e.get("v"), str)
                     and (e.get("src") or {}).get("by") == f"{ASSISTANT}/{company_tool.AGENT_NAME}"}
    raws = [*(st.profile.personal_sites if st.profile else []),
            *split_urls(deps.current("website")),
            *(u for v in deps.history_values("website") for u in split_urls(v))]
    raws = [r for r in raws if url_key(r) not in company_sites]
    out: list[str] = []
    for raw in raws:
        n = normalize_url(raw)
        if n and is_html_candidate(n) and not is_social(n) and url_key(n) not in map(url_key, out):
            out.append(n)
    return out


async def _step_sites(deps: AssistantDeps, st: State) -> None:
    """Step 2: read the person's own site(s): the profile's, plus any already on the record."""
    candidates = _site_candidates(deps, st)
    if not candidates:
        deps.skip("sites", "no personal website known")
        return
    deps.emit("step", f"sites: reading {', '.join(candidates[:CAPS['sites']])}")
    for site in candidates[:CAPS["sites"]]:
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
            findings = await site_tool.read_site(deps, st.name, site, page.markdown, page.links,
                                              CAPS["follow"])
        if not findings.is_personal:
            # A company or unrelated site: its links are about the company, not the person.
            deps.skip("sites", f"{site}: not the person's own site")
            continue
        if not _has_url(deps, "website", site):
            await deps.write(site_tool.AGENT_NAME, {"website": site})
        if findings.one_liner:
            st.passage.add(site_tool.AGENT_NAME, f"Personal site {site}",
                           [("one_liner", findings.one_liner)])
        st.github_urls += [g for g in map(github_profile, [*page.links, *findings.github_urls])
                           if g]
        st.related += [link.url for link in findings.related_links]


async def _step_github_and_links(deps: AssistantDeps, st: State) -> None:
    """Step 3: GitHub and related links come only from what the record and pages gave us (no
    searching); a known GitHub account is summarised through the github-com data tool."""
    github_urls = list(dict.fromkeys(g for g in st.github_urls if g))
    current_github = github_profile(deps.current("github")) if isinstance(
        deps.current("github"), str) else None
    if not github_urls and current_github:
        github_urls = [current_github]
    if github_urls:
        if not _has_url(deps, "github", github_urls[0]):
            await deps.write(site_tool.AGENT_NAME, {"github": github_urls[0]})
        await _github_facts(deps, st, github_urls[0])
    else:
        st.result["not_found"].append("github")
        deps.skip("github", "no GitHub link on the profile or sites")
    exclude = {url_key(deps.current("website")), *map(url_key, github_urls)}
    links = [n for n in map(normalize_url, st.related)
             if n and not linkedin_profile(n) and url_key(n) not in exclude]
    if not links:
        return
    current = deps.current("related_links")
    current = current if isinstance(current, list) else []
    merged = list(dict.fromkeys([*current, *links]))[:MAX_RELATED_LINKS]
    if len(merged) > len(current):
        await deps.write(site_tool.AGENT_NAME, {"related_links": merged})


async def _github_facts(deps: AssistantDeps, st: State, github_url: str) -> None:
    if deps.current("github_summary") and url_key(deps.current("github")) == url_key(github_url) \
            and not st.options.refresh:
        deps.skip("github", "already summarised for this account")
        return
    with logfire.span("github {url}", url=github_url):
        found, status = await _provider_call(deps, "github",
                                             lambda: github.lookup(deps.firecrawl, github_url),
                                             "the GitHub data tool")
    if not found:
        st.result["github"] = status
        if status == "not_found":
            st.result["not_found"].append("github")
        return
    st.result["github"] = "matched"
    values: dict[str, Any] = {"github_summary": _clip(found.summary())}
    if found.blog and not is_social(found.blog) and is_html_candidate(found.blog) \
            and not _has_url(deps, "website", found.blog):
        values["website"] = found.blog       # the account's blog field is usually the personal site
    if found.location and not deps.current("location"):
        values["location"] = found.location
    await deps.write("github", values)


def _company_candidate(deps: AssistantDeps, st: State, company: str) -> str | None:
    """The cheapest plausible company website: the provider's, else a website on the record whose
    host names the company."""
    if st.profile and st.profile.current_company and st.profile.current_company.website:
        return normalize_url(st.profile.current_company.website)
    for site in [deps.current("website"), *deps.history_values("website")]:
        if isinstance(site, str) and host_mentions(host_of(site), company):
            return normalize_url(site)
    return None


async def _step_company(deps: AssistantDeps, st: State) -> None:
    """Step 4: resolve and summarise the current company; low confidence writes nothing."""
    company = (st.profile.current_company.name if st.profile and st.profile.current_company
               else None) or deps.current("company")
    if not company:
        deps.skip("company", "no current company known")
        return
    company = str(company)
    if not st.options.refresh and _recent(deps, "company_description", "company"):
        deps.skip("company", "already summarised within the TTL (an earlier attempt)")
        st.result["company"] = "resolved"
        return
    deps.emit("step", f"company: researching {company}")
    if "company" in st.previously_missing:
        deps.skip("company", "unresolved last time; not retried within the TTL")
        st.result["company"] = "unresolved"
        return
    context = ", ".join(str(x) for x in (deps.current("location"), deps.current("sectors"),
                                          deps.current("role")) if x)
    if st.profile and st.profile.current_company and st.profile.current_company.description:
        context += ". The data provider describes the company as: " \
                   f"{_clip(st.profile.current_company.description, 400)}"
    try:
        with logfire.span("company {company}", company=company):
            summary = await company_tool.research_company(
                deps, company, _company_candidate(deps, st, company), context,
                allow_search=deps.budget.searches < deps.budget.max_searches,
                pages_cap=CAPS["company_pages"])
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
    # One `website` property holds the person's and the company's sites as history entries; the
    # company site becomes current only when it is not already in the history.
    values: dict[str, Any] = {}
    if website and not _has_url(deps, "website", website):
        values["website"] = website
    # The summary goes on `company_description`, whose earlier values (e.g. the imported one)
    # stay in its history. A reworded summary of the same website is not new information: only
    # write one when the record has no description yet or the company website is new.
    if "website" in values or not deps.current("company_description"):
        values["company_description"] = _clip(summary.summary)
    else:
        deps.skip("company", "summary kept: same website as before")
    if not deps.current("company"):
        values["company"] = company
    await deps.write(company_tool.AGENT_NAME, values)


async def _step_store(deps: AssistantDeps, st: State) -> None:
    """Step 5: everything prose- or tag-like the sources returned goes to the storage agent in one
    passage (labelled lines, one per property, a section per source), so it lands in the right
    properties with restatements confirmed and conflicts marked; facts the catalog has no home
    for are dropped unless the run allows new properties. The passage itself is then kept on the
    record as `enrichment_passage`, so the facts and their source sit side by side in history;
    a refresh whose passage equals the one on the record makes no model call."""
    if st.redo:
        deps.skip("storage", "filed by the earlier attempt")
        return
    if not st.passage:
        deps.skip("storage", "nothing gathered that needs filing")
        return
    text = st.passage.render()
    st.result["passage"], st.result["passage_hash"] = text, st.passage.digest()
    for source, keys in st.passage.dropped_by_source().items():
        deps.skip("storage", f"{source} also stated {', '.join(keys)}; the first source's "
                             "values were kept")
    if deps.current("enrichment_passage") == text:
        st.result["storage"] = "unchanged"
        deps.skip("storage", "the same facts were filed before")
        return
    sources = dict.fromkeys(s for ss in st.passage.sources.values() for s in ss)
    deps.emit("step", f"storage: filing {len(st.passage.keys)} facts from {', '.join(sources)}")
    opts = store_tool.StoreOptions(max_new=st.options.max_new_properties, overflow="ignore",
                                   locked=sorted(LOCKED))
    try:
        with logfire.span("storage"):
            res = await deps.tool(store_tool.store, "storage", passage=text, opts=opts)
    except Exception as e:  # the item stays done; the facts wait for `refresh`
        log.exception("enrich: the storage agent failed for %s", deps.person_id)
        st.result["storage"] = "error"
        deps.skip("storage", f"the storage agent failed: {e}")
        return
    if res is None:
        return
    assert isinstance(res, store_tool.StoreResult)
    if not (res.evidence or res.confirmed or res.changes):
        # nothing filed and nothing confirmed: do not record the passage, so a refresh tries again
        st.result["storage"] = "nothing"
        deps.skip("storage", "the storage agent filed nothing from the passage")
        return
    st.result["storage"] = "stored"
    st.result["stored"] = {key: {"provider": st.passage.source_of(key), "evidence": evidence}
                           for key, evidence in res.evidence.items()}
    st.result["confirmed"] = list(res.confirmed)
    if res.dropped:
        st.result["dropped"] = list(res.dropped)
    if res.created_properties:
        st.result["created_properties"] = list(res.created_properties)
    await deps.write("storage", {"enrichment_passage": text})
