"""Credit discipline for the enrichment workflow: per-mode caps and what each step may spend."""

from app.workflows.enrich.models import EnrichOptions
from app.workflows.firecrawl import Budget

# Worst-case provider cost for one LinkedIn profile (Data Legion premium); used to reserve credits
# before the call so the per-item cap is honoured even when the provider is expensive.
LINKEDIN_EXPECTED_CREDITS = 40

CAPS = {
    "frugal": {"max_scrapes": 4, "max_searches": 1, "max_credits": 60,
               "sites": 2, "follow": 1, "company_pages": 2},
    "thorough": {"max_scrapes": 8, "max_searches": 3, "max_credits": 120,
                 "sites": 4, "follow": 2, "company_pages": 2},
}

MIN_PAGE_CHARS = 200          # a page shorter than this is not worth an LLM call
MAX_PROMPT_CHARS = 8000       # page text handed to an agent


def budget_for(options: EnrichOptions) -> Budget:
    c = CAPS[options.mode]
    return Budget(max_scrapes=c["max_scrapes"], max_searches=c["max_searches"],
                  max_credits=c["max_credits"])


def caps(options: EnrichOptions) -> dict:
    return CAPS[options.mode]
