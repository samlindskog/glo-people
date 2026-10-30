"""Credit discipline for the enrichment workflow: per-mode caps and what each step may spend."""

from app.workflows.enrich.models import EnrichOptions
from app.workflows.firecrawl import Budget

# Listed prices of the Alexandria tools the ladder uses (per record found; a miss costs nothing).
FULLENRICH_MATCH_CREDITS = 5      # fullenrich/people/match: exact LinkedIn URL
FULLENRICH_LOOKUP_CREDITS = 10    # fullenrich/people/lookup: name + company domain (thorough only)
GITHUB_USER_CREDITS = 5           # github-com/repositories/user

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
