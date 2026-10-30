"""Agent: read a personal website and pull out GitHub / related links and a one-liner."""

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from app.agents.common import AgentDeps, budgeted_fetch, excerpt, run_agent
from app.urls import same_site

AGENT_NAME = "site_reader"


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


agent = Agent(
    name=f"agents.{AGENT_NAME}",
    deps_type=AgentDeps,
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
async def fetch_page(ctx: RunContext[AgentDeps], url: str) -> str:
    """Fetch another page of the same website (about, links, contact)."""
    home = ctx.deps.notes.get("site_home", "")
    return await budgeted_fetch(ctx, url, counter="site_follow",
                                cap=ctx.deps.notes.get("site_follow_cap", 1),
                                allowed=lambda u: same_site(u, home))


async def read(deps: AgentDeps, person_name: str, url: str, markdown: str,
               links: list[str], follow_cap: int) -> SiteFindings:
    deps.notes["site_home"] = url
    deps.notes["site_follow"] = 0
    deps.notes["site_follow_cap"] = follow_cap
    prompt = (f"Person: {person_name}\nSite: {url}\n\n# Home page\n{excerpt(markdown)}\n\n"
              f"## Links on the page\n" + "\n".join(links[:80]))
    return await run_agent(agent, prompt, deps)
