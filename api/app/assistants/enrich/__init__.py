"""Assistant (level 3): enrich a person from LinkedIn and the web (see assistant.py for the ladder)."""

from app.assistants.base import AssistantSpec, register
from app.assistants.enrich.assistant import ASSISTANT, make_deps, run_item
from app.assistants.enrich.budget import item_cap
from app.assistants.enrich.models import EnrichOptions

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
    options_model=EnrichOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=item_cap,
))
