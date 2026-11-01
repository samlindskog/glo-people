"""Assistant (level 3): enrich a person from LinkedIn and the web (see assistant.py for the ladder)."""

from app.assistants.base import AssistantSpec, register
from app.assistants.enrich.assistant import ASSISTANT, make_deps, run_item
from app.assistants.enrich.budget import item_cap
from app.assistants.enrich.models import EnrichOptions

DETAILS = """\
**What it does.** For each person, in a fixed order with free fallbacks: resolve the LinkedIn profile, read the person's own website(s), summarise a known GitHub account, resolve and describe the current company. Every write is an appended history entry attributed to the step that made it (enrich/linkedin_reader, enrich/site_reader, enrich/github, enrich/company).

**Starting it.** One person, or everyone with a `linkedin` value. Options: `refresh` (false: re-run people enriched recently) and `max_credits` (1500: the Firecrawl cap for the whole run).

**Budget per person.** At most 4 page scrapes, 1 web search and 60 credits. Each item reserves that worst case while it runs, so the run can overshoot its cap by at most one item; when the cap is reached the run pauses (reason "credit cap") and Resume with a higher cap continues it. An empty Firecrawl account pauses the run with reason "account credits".

**Step 0, the TTL.** A person with a finished enrichment in the last 90 days is skipped for free unless `refresh` is set. Steps that found nothing last time (no provider record, company unresolved) are not retried within the TTL either.

**Step 1, LinkedIn (FullEnrich via Firecrawl Alexandria, 5 credits per matched profile, a miss is free).** Needs a linkedin.com/in URL on the record. The profile's headline becomes `linkedin_headline`; its GitHub link becomes `github`; `location` is filled when the record has none; `company` is appended when the profile names a different current employer (the old one stays in history). An item redone after an interruption skips this step when an entry by it is already on the record.

**Step 2, the person's sites (1 credit per page).** Candidates are the profile's personal sites plus every website ever on the record, minus social networks. Up to 2 are scraped (Firecrawl reuses a page cached within 2 days); the site reader agent decides whether the page is the person's own site, and may follow one link. A personal site is appended to `website` when new; its GitHub and related links are collected. A company or unrelated site is skipped and its links ignored.

**Step 3, GitHub and links (github-com data tool, 5 credits).** The GitHub account from the profile, the pages or the record is summarised into `github_summary` (repos, followers, languages); the account's blog field becomes `website` when it is a personal site not yet on the record. Already-summarised accounts are not paid for again unless `refresh`. Related links (newsletters, Twitter and the like, never LinkedIn) are merged into `related_links`, at most 50.

**Step 4, the company (up to 2 pages at 1 credit, one web search at 2 credits).** The current company comes from the profile or the record. The cheapest plausible website is tried first: the provider's, else a website on the record whose host names the company; without one the company agent may search once. Low confidence writes nothing. Otherwise the website is appended to `website` when new, `company_description` is written when the record has none or the website is new, and `company` is filled when the record had none. The evidence URL and confidence stay on the run item, not on the person.

**Result.** One line per person ("LinkedIn profile matched; GitHub account summarised; company website resolved and summarised; wrote github, company_description"), every change, the skipped steps with their reasons, the credits and the paid calls, and a Logfire trace per person.
"""

SPEC = register(AssistantSpec(
    key=ASSISTANT,
    label="Enrich from LinkedIn and the web",
    description=("Resolves the person's LinkedIn profile through FullEnrich (Firecrawl "
                 "Alexandria, 5 credits per match), reads their personal site(s), summarises a "
                 "known GitHub account (github-com data tool, 5 credits), and records headline, "
                 "current company, websites, related links and a short description of the company "
                 "from its website. Every step has a free fallback; nothing is searched for when "
                 "the record gives no lead."),
    selector_field="linkedin",
    details=DETAILS,
    options_model=EnrichOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=item_cap,
))
