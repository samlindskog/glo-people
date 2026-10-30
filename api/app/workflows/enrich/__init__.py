"""Enrich from LinkedIn and the web: the first workflow (see workflow.py for the ladder)."""

from app.workflows import WorkflowSpec, register
from app.workflows.enrich.budget import budget_for
from app.workflows.enrich.models import EnrichOptions
from app.workflows.enrich.workflow import WORKFLOW, run_item

SPEC = register(WorkflowSpec(
    key=WORKFLOW,
    label="Enrich from LinkedIn and the web",
    description=("Reads the person's LinkedIn profile through Firecrawl's data providers, then "
                 "their personal site(s), and records headline, GitHub, websites, related links, "
                 "and a short summary of their current company. Fallbacks at every step; frugal "
                 "mode never searches the web for missing LinkedIn or GitHub links."),
    selector_field="linkedin",
    options_model=EnrichOptions,
    run_item=run_item,
    budget_for=budget_for,
))
