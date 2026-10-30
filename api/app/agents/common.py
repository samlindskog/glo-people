"""Shared plumbing for every pydantic-ai agent in the app: the model to run on, usage limits, the
dependencies tools receive, and a budgeted page-fetch helper for tools that read the web."""

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.usage import UsageLimits

from app.config import get_settings
from app.integrations.firecrawl import AsyncFirecrawl, BudgetExceeded, FirecrawlError

REQUEST_LIMIT = 6
MAX_PROMPT_CHARS = 8000       # page text handed to an agent


@dataclass
class AgentDeps:
    """What agent tools can reach: a budgeted Firecrawl client and a scratch space for per-run
    counters. Callers that need more (workflows) subclass it."""

    firecrawl: AsyncFirecrawl
    notes: dict[str, Any] = field(default_factory=dict)

    async def aclose(self) -> None:
        await self.firecrawl.aclose()


def model_settings() -> dict[str, Any]:
    model = get_settings().agent_model
    if model.startswith("openai"):
        return {"openai_reasoning_effort": "low"}
    return {}


async def run_agent(agent: Agent, prompt: str, deps: AgentDeps | None = None) -> Any:
    """Run an agent with the configured model and this app's usage limits; returns `.output`."""
    result = await agent.run(prompt, deps=deps, model=get_settings().agent_model,
                             usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
                             model_settings=model_settings())
    return result.output


def excerpt(text: str, limit: int = MAX_PROMPT_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


async def budgeted_fetch(ctx: RunContext[AgentDeps], url: str, counter: str, cap: int,
                         allowed=None) -> str:
    """Fetch a page for an agent tool, bounded by a per-agent counter and the item budget.
    Returns page text or a short explanation the model can act on; never raises."""
    deps = ctx.deps
    n = deps.notes.get(counter, 0)
    if n >= cap:
        return f"Fetch limit reached ({cap}); answer with the information you already have."
    if allowed is not None and not allowed(url):
        return "That URL is outside the allowed site; answer with what you have."
    deps.notes[counter] = n + 1
    try:
        page = await deps.firecrawl.scrape(url, formats=("markdown", "links"))
    except BudgetExceeded as e:
        return f"Budget exhausted ({e}); answer with the information you already have."
    except FirecrawlError as e:
        return f"Could not fetch {url}: {e}"
    if page.status_code and page.status_code >= 400:
        return f"{url} answered HTTP {page.status_code}."
    links = "\n".join(page.links[:60])
    return f"# {page.title}\n\n{excerpt(page.markdown)}\n\n## Links on the page\n{links}"
