"""Tool (level 2): resolve a company's official website (when not already known) and summarise
what it does, with a confidence and the page it rests on. One model run with a bounded number
of page fetches and at most one web search, within the caller's budget."""

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from app.agents.common import AgentDeps, budgeted_fetch, run_agent
from app.integrations.firecrawl import BudgetExceeded, FirecrawlError, SearchHit
from app.urls import host_of, is_social, registrable

AGENT_NAME = "company"


class CompanySummary(BaseModel):
    website: str | None = Field(None, description="The company's official website, or None")
    summary: str | None = Field(None, description="Two or three plain sentences: what the "
                                                  "company does, for whom, and anything notable "
                                                  "(stage, location). None if unresolved.")
    confidence: Literal["high", "medium", "low"] = Field(
        description="high: the page names the company and matches the person's context; "
                    "medium: likely but not corroborated; low: guess or no page found")
    evidence_url: str | None = Field(None, description="Page the summary is based on")


agent = Agent(
    name=f"agents.{AGENT_NAME}",
    deps_type=AgentDeps,
    output_type=CompanySummary,
    instructions="""\
You identify a company's official website and write a short factual summary of what it does.

Procedure:
1. If a candidate website is given, fetch it first with fetch_page. If the page clearly belongs to
   the named company (the company name appears in the title or the page), summarise it with
   confidence "high" (or "medium" if the match is plausible but not explicit).
2. Otherwise, if web_search is available, search once for the company (use the person's location
   or industry hints to disambiguate), then fetch at most two of the results that look like the
   company's own domain. LinkedIn, Crunchbase, news and directory pages cannot be fetched and must
   not be used as evidence.
3. If no page corroborates the company name, answer with confidence "low", website None and
   summary None. Never guess a domain. Never summarise a different company with the same name.

The summary is two or three plain sentences: what the company does, for whom, anything notable
(stage, location, scale) stated on the page. No marketing language. Leave out figures that look
like unrendered placeholders (a count of 0, "{{n}}", "N/A") rather than commenting on them.""",
)


def _allow(url: str, hosts: set[str]) -> bool:
    return registrable(host_of(url)) in hosts


def allow_hosts(deps: AgentDeps, hits: list[SearchHit]) -> None:
    """Let fetch_page visit the search results' own sites. Social and directory hosts are left
    out: a LinkedIn company page goes through paid data providers, and a directory is not the
    company speaking for itself."""
    deps.notes.setdefault("company_allowed", set()).update(
        registrable(host_of(h.url)) for h in hits if host_of(h.url) and not is_social(h.url))


@agent.tool
async def fetch_page(ctx: RunContext[AgentDeps], url: str) -> str:
    """Fetch a web page: only the candidate website or a site returned by web_search."""
    # Page text and search snippets are untrusted; the model may only fetch sites we have seen
    # a reason to visit (candidate site, search results), never an arbitrary URL from a page.
    allowed = ctx.deps.notes.setdefault("company_allowed", set())
    return await budgeted_fetch(ctx, url, counter="company_pages",
                                cap=ctx.deps.notes.get("company_pages_cap", 2),
                                allowed=lambda u: _allow(u, allowed))


@agent.tool
async def web_search(ctx: RunContext[AgentDeps], query: str) -> str:
    """Search the web (one call allowed). Returns up to five results as `title — url — snippet`."""
    deps = ctx.deps
    if deps.notes.get("company_search", 0) >= deps.notes.get("company_search_cap", 1):
        return "Search limit reached; use the results you already have or answer with low confidence."
    deps.notes["company_search"] = deps.notes.get("company_search", 0) + 1
    try:
        hits = await deps.firecrawl.search(query, limit=5)
    except BudgetExceeded as e:
        return f"Budget exhausted ({e}); answer with low confidence."
    except FirecrawlError as e:
        return f"Search failed: {e}"
    if not hits:
        return "No results."
    allow_hosts(deps, hits)
    return "\n".join(f"{h.title} — {h.url} — {h.description[:200]}" for h in hits)


async def research_company(deps: AgentDeps, company: str, candidate_site: str | None,
                           context: str, allow_search: bool, pages_cap: int) -> CompanySummary:
    deps.notes["company_pages"] = 0
    deps.notes["company_pages_cap"] = pages_cap
    deps.notes["company_search"] = 0
    deps.notes["company_search_cap"] = 1 if allow_search else 0
    deps.notes["company_allowed"] = {registrable(host_of(candidate_site))} if candidate_site else set()
    prompt = (f"Company: {company}\n"
              f"Candidate website: {candidate_site or 'none known'}\n"
              f"Context about the person: {context or 'none'}\n"
              f"Web search available: {'yes, once' if allow_search else 'no'}")
    return await run_agent(agent, prompt, deps)
