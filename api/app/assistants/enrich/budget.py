"""Credit discipline for the enrichment assistant: the per-item caps, what each step may spend,
and what a set of options costs, described for a plan."""

from typing import Any

from app.assistants.enrich.models import EnrichOptions
from app.assistants.enrich.providers import PROVIDERS
from app.integrations import github
from app.integrations.firecrawl import (
    OBSERVED_PRICES,
    SCRAPE_CREDITS,
    SEARCH_CREDITS,
    Budget,
    overpriced,
)

CAPS = {"max_scrapes": 4, "max_searches": 1, "max_credits": 80,
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
    these options can spend (16 with the default provider), not the per-item safety ceiling
    `CAPS["max_credits"]`, so a modest cap still lets several items run at once."""
    return worst_case(options)


def worst_case(options: EnrichOptions) -> int:
    """The most one item can spend with these options: every provider matched, the GitHub tool,
    every site and company page, the one search. Must stay within `CAPS["max_credits"]` (a test
    holds the line), so a new provider's price is checked before it joins the table."""
    return (sum(PROVIDERS[k].credits for k in dict.fromkeys(options.providers))
            + github.USER_CREDITS + (CAPS["sites"] + CAPS["company_pages"]) * SCRAPE_CREDITS
            + CAPS["max_searches"] * SEARCH_CREDITS)


def provider_prices() -> list[dict[str, Any]]:
    """Every provider in the table with its listed price per match and the last bill seen in this
    process (None until it was called), for the balance view and the plan."""
    return [{"key": p.key, "label": p.label, "credits_per_match": p.credits,
             "last_billed": OBSERVED_PRICES.get(p.source)} for p in PROVIDERS.values()]


def typical_case(options: EnrichOptions) -> int:
    """What one item usually costs with these options when no history says better: the providers
    the policy asks on a match (the first under `gaps`, every one under `all`) plus two page
    reads; a miss is free, GitHub and the search are rare."""
    asked = list(dict.fromkeys(options.providers))
    if options.provider_policy == "gaps":
        asked = asked[:1]
    return sum(PROVIDERS[k].credits for k in asked) + 2 * SCRAPE_CREDITS


def describe(options: EnrichOptions) -> dict[str, Any]:
    """What these options mean for cost, for a plan or a run view: the providers asked in order
    with their prices, the policy, the typical and worst case per item, which past items are
    comparable (the same providers), and a warning for every provider priced far above the
    default one — a choice an agent should only make when the person asked for it by name."""
    asked = list(dict.fromkeys(options.providers))
    prices = {p["key"]: p for p in provider_prices()}
    default = PROVIDERS[EnrichOptions().providers[0]]
    warnings = [f"{PROVIDERS[k].label} bills {PROVIDERS[k].credits} credits per match; the default "
                f"({default.label}) bills {default.credits}. Keep it only when the person asked for "
                f"{PROVIDERS[k].label} by name; otherwise leave options.providers unset."
                for k in asked if PROVIDERS[k].credits > overpriced(default.credits)]
    return {"providers": [prices[k] for k in asked], "provider_policy": options.provider_policy,
            "typical_per_item": typical_case(options), "worst_case_per_item": worst_case(options),
            "comparable": ("providers", asked), "warnings": warnings}
