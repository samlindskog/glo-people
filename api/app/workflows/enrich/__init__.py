"""Enrich from LinkedIn and the web: the first workflow (see workflow.py for the ladder)."""

from app.workflows.base import WorkflowSpec, register
from app.workflows.enrich.budget import item_cap
from app.workflows.enrich.models import EnrichOptions
from app.workflows.enrich.workflow import WORKFLOW, make_deps, run_item

SPEC = register(WorkflowSpec(
    key=WORKFLOW,
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
