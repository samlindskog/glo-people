"""Normalise: run the normalise-person operation (parser, then the normaliser agent for what is
left) for many people from the Workflows or Properties page."""

from app.workflows.base import WorkflowSpec, register
from app.workflows.normalize.workflow import (
    WORKFLOW,
    NormalizeOptions,
    item_cap,
    make_deps,
    run_item,
)

SPEC = register(WorkflowSpec(
    key=WORKFLOW,
    label="Normalise typed properties",
    description=("Re-parses every typed property (number, date, boolean, enum) of each person "
                 "and asks the normaliser agent about the values the parser cannot map, so "
                 "filters on normalised values see them. No external data is fetched; the "
                 "only cost is the model call for people with unmapped values."),
    selector_field="name",
    options_model=NormalizeOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=item_cap,
))
