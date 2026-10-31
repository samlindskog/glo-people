"""Shared plumbing for every pydantic-ai agent in the app: the model to run on (or its fake),
usage limits, the dependencies web-reading tools receive, a budgeted page-fetch helper, and the
property catalog rendered for prompts."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.usage import UsageLimits

from app.config import get_settings
from app.integrations.firecrawl import AsyncFirecrawl, BudgetExceeded, FirecrawlError
from app.properties import Property

REQUEST_LIMIT = 6
MAX_PROMPT_CHARS = 8000       # page text handed to an agent


@dataclass
class AgentDeps:
    """What web-reading agent tools can reach: a budgeted Firecrawl client and a scratch space
    for per-run counters. Callers that need more (workflows) subclass it."""

    firecrawl: AsyncFirecrawl | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    async def aclose(self) -> None:
        if self.firecrawl is not None:
            await self.firecrawl.aclose()


def model_settings() -> dict[str, Any]:
    model = get_settings().agent_model
    if model.startswith("openai"):
        return {"openai_reasoning_effort": "low"}
    return {}


async def run_agent(agent: Agent, prompt: str, deps: Any = None,
                    request_limit: int = REQUEST_LIMIT) -> Any:
    """Run an agent with the configured model (or its deterministic fake under FAKE_AGENTS) and
    this app's usage limits; returns `.output`."""
    if get_settings().fake_agents:
        from app.agents import fakes
        model: Any = fakes.model_for(agent)
    else:
        model = get_settings().agent_model
    result = await agent.run(prompt, deps=deps, model=model,
                             usage_limits=UsageLimits(request_limit=request_limit),
                             model_settings=model_settings())
    return result.output


def catalog_prompt(props: Sequence[Property], max_values: int = 12) -> str:
    """The property catalog as an agent reads it: one line per property with type, whether it is
    embeddable (searchable by passage), the canonical values of enum types and, when the caller
    passed coverage, how many people have a value."""
    lines = []
    for p in props:
        bits = [p.type]
        if p.embeddable:
            bits.append("embeddable")
        if p.values:
            more = ", …" if len(p.values) > max_values else ""
            bits.append("values: " + ", ".join(p.values[:max_values]) + more)
        if p.count:
            bits.append(f"{p.count} people")
        desc = f" — {p.description}" if p.description else ""
        lines.append(f"- {p.key} ({'; '.join(bits)}): {p.label}{desc}")
    return "\n".join(lines)


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
