"""Enrich from LinkedIn and the web: the first workflow (see workflow.py for the ladder)."""

from app.workflows import WorkflowSpec, register
from app.workflows.enrich.budget import budget_for
from app.workflows.enrich.models import EnrichOptions
from app.workflows.enrich.workflow import WORKFLOW, run_item

SPEC = register(WorkflowSpec(
    key=WORKFLOW,
    label="Enrich from LinkedIn and the web",
    description=("Resolves the person's LinkedIn profile through FullEnrich (Firecrawl "
                 "Alexandria, 5 credits per match), reads their personal site(s), summarises a "
                 "known GitHub account (github-com data tool, 5 credits), and records headline, "
                 "current company, websites, related links and a short summary of the company's "
                 "website. Fallbacks at every step; frugal mode never searches the web for "
                 "missing LinkedIn or GitHub links."),
    selector_field="linkedin",
    options_model=EnrichOptions,
    run_item=run_item,
    budget_for=budget_for,
))
