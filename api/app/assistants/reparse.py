"""Assistant (level 3): run the parser over the normalised values of every record (or the chosen
people), in batches. Free. With `force` (the default) the parser's own earlier results are redone,
as after a parser upgrade; what an agent decided is always kept. Drives `people.reparse`."""

from typing import Any

from pydantic import BaseModel, Field

from app import people, properties
from app.assistants.base import AssistantSpec, ItemSpec, register
from app.assistants.context import AssistantDeps
from app.assistants.embed import all_or_chosen, batches
from app.auth import Principal
from app.integrations.firecrawl import Budget

ASSISTANT = "reparse"


class ReparseOptions(BaseModel):
    force: bool = Field(True, description="Redo the parser's earlier results too")
    batch: int = Field(200, ge=1, le=2000, description="People per item")


async def prepare(body: dict[str, Any], options: ReparseOptions) -> list[ItemSpec]:
    ids = await all_or_chosen(body)
    await properties.sync_specs()
    return batches(ids, options.batch)


async def make_deps(run_id: int, item: dict, options: ReparseOptions,
                    principal: Principal | None) -> AssistantDeps:
    return AssistantDeps(assistant=ASSISTANT, run_id=run_id, item_id=item["id"],
                         payload=item["payload"], label=item.get("label"), principal=principal,
                         budget=Budget(max_scrapes=0, max_searches=0, max_credits=0),
                         options=options)


async def run_item(deps: AssistantDeps) -> dict[str, Any]:
    ids = deps.payload["ids"]
    n = await people.reparse(ids, force=deps.options.force)
    return {"text": f"{n} of {len(ids)} documents re-parsed.", "changes": [], "written": n}


DETAILS = """\
**What it does.** Runs the deterministic parser over the typed properties (number, date, boolean, enum, enum list) of every record, in batches, and writes only what changes. Free; no agents, no Firecrawl.

**Starting it.** Everyone, in batches of `batch` people (200). `force` (true) redoes the parser's own earlier results too, as after a parser upgrade or a new alias; what an agent or a person decided for the current text is always kept. The CLI's `python -m app.cli normalize --all` creates the same run (and `make prod-migrate` runs it after a deploy).

**Each batch.** For each person the parser is applied to every typed value whose text has not been normalised, or whose normalised value came from the parser when `force` is on. A compare-and-set write per changed document.

**Result.** Documents written per batch. The normalise assistant is the one that consults agents and repairs records; this one only re-reads texts.
"""

SPEC = register(AssistantSpec(
    key=ASSISTANT,
    label="Re-parse typed values",
    description=("Runs the parser over the typed properties (numbers, dates, booleans, enums) of "
                 "every record and writes only what changes; agents' decisions stay. Free."),
    options_model=ReparseOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=lambda options: 0,
    scope="batches",
    prepare=prepare,
    details=DETAILS,
))
