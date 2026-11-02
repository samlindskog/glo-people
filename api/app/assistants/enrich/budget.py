"""Credit discipline for the enrichment assistant: the per-item caps and what each step may spend."""

from app.assistants.enrich.models import EnrichOptions
from app.assistants.enrich.providers import PROVIDERS
from app.integrations import github
from app.integrations.firecrawl import SCRAPE_CREDITS, SEARCH_CREDITS, Budget

CAPS = {"max_scrapes": 4, "max_searches": 1, "max_credits": 60,
        "sites": 2, "follow": 1, "company_pages": 2}

MIN_PAGE_CHARS = 200          # a page shorter than this is not worth an LLM call


def budget_for(options: EnrichOptions) -> Budget:
    """The item's budget: `max_scrapes` page scrapes plus one data-tool call per provider asked
    and one for GitHub (data-tool calls go through the same counter), one search, the credit cap."""
    tool_calls = len(dict.fromkeys(options.providers)) + 1
    return Budget(max_scrapes=CAPS["max_scrapes"] + tool_calls, max_searches=CAPS["max_searches"],
                  max_credits=CAPS["max_credits"])


def item_cap(options: EnrichOptions) -> int:
    """What the runner reserves per item in flight against the run's credit cap: the worst case
    these options can spend (17 with the default providers), not the per-item safety ceiling
    `CAPS["max_credits"]`, so a modest cap still lets several items run at once."""
    return worst_case(options)


def worst_case(options: EnrichOptions) -> int:
    """The most one item can spend with these options: every provider matched, the GitHub tool,
    every site and company page, the one search. Must stay within `CAPS["max_credits"]` (a test
    holds the line), so a new provider's price is checked before it joins the table."""
    return (sum(PROVIDERS[k].credits for k in dict.fromkeys(options.providers))
            + github.USER_CREDITS + (CAPS["sites"] + CAPS["company_pages"]) * SCRAPE_CREDITS
            + CAPS["max_searches"] * SEARCH_CREDITS)
