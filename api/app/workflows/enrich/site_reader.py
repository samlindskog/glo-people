"""Agent: read a personal website and pull out GitHub / related links and a one-liner."""

from pydantic_ai import Agent, RunContext

from app.workflows.context import WorkflowDeps
from app.workflows.enrich.agents_common import budgeted_fetch, excerpt, run_agent
from app.workflows.enrich.models import SiteFindings
from app.workflows.enrich.urls import same_site

AGENT_NAME = "site_reader"

agent = Agent(
    name=f"enrich.{AGENT_NAME}",
    deps_type=WorkflowDeps,
    output_type=SiteFindings,
    instructions="""\
You are given the home page of a website that may belong to a specific person, plus the links on
it. Decide whether it is their personal site, and extract GitHub profile URLs and other links about
them (twitter/X, blog, newsletter, portfolio, talks). Write a one-sentence description of the
person in the site's own words when the page gives one.

Use fetch_page at most once, and only for an "about", "links" or "contact" page on the same site
when the home page itself says little about the person. Never invent URLs: only report links that
appear on the pages you were given.""",
)


@agent.tool
async def fetch_page(ctx: RunContext[WorkflowDeps], url: str) -> str:
    """Fetch another page of the same website (about, links, contact)."""
    home = ctx.deps.notes.get("site_home", "")
    return await budgeted_fetch(ctx, url, counter="site_follow",
                                cap=ctx.deps.notes.get("site_follow_cap", 1),
                                allowed=lambda u: same_site(u, home))


async def read(deps: WorkflowDeps, person_name: str, url: str, markdown: str,
               links: list[str], follow_cap: int) -> SiteFindings:
    deps.notes["site_home"] = url
    deps.notes["site_follow"] = 0
    deps.notes["site_follow_cap"] = follow_cap
    prompt = (f"Person: {person_name}\nSite: {url}\n\n# Home page\n{excerpt(markdown)}\n\n"
              f"## Links on the page\n" + "\n".join(links[:80]))
    return await run_agent(agent, prompt, deps)
