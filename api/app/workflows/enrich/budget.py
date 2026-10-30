"""Credit discipline for the enrichment workflow: the per-item caps and what each step may spend."""

from app.integrations.firecrawl import Budget
from app.workflows.enrich.models import EnrichOptions

CAPS = {"max_scrapes": 4, "max_searches": 1, "max_credits": 60,
        "sites": 2, "follow": 1, "company_pages": 2}

MIN_PAGE_CHARS = 200          # a page shorter than this is not worth an LLM call


def budget_for(options: EnrichOptions) -> Budget:
    return Budget(max_scrapes=CAPS["max_scrapes"], max_searches=CAPS["max_searches"],
                  max_credits=CAPS["max_credits"])


def item_cap(options: EnrichOptions) -> int:
    return CAPS["max_credits"]
