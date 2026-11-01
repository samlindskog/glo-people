"""Assistant (level 3): import a CSV. The surface (REST or CLI) reads the file; `prepare_csv`
parses it, creates the new properties and cuts the rows into batches, one item each; an item
upserts its rows (`people.upsert_imported`, keyed by file and source id, so re-importing updates)
and embeds the people it touched. Resumable like any run: a batch interrupted by a restart is
redone, and unchanged values append nothing."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app import embeddings, importer, people, properties
from app.assistants.base import AssistantSpec, ItemSpec, register
from app.assistants.context import AssistantDeps
from app.auth import Principal
from app.integrations.firecrawl import Budget

ASSISTANT = "import"


class ImportOptions(BaseModel):
    tier: Literal["member", "plus_one"] = "member"
    batch: int = Field(200, ge=1, le=1000, description="Rows per item")
    embed: bool = Field(True, description="Embed the imported passages as each batch lands")


async def prepare_csv(text: str, filename: str,
                      options: ImportOptions) -> tuple[importer.ParsedCsv, list[str], list[ItemSpec]]:
    """Parse, create the properties the rows use, and cut the rows into items."""
    parsed = importer.parse_csv(text, filename)
    created = await properties.ensure(parsed.used_keys, "import")
    items: list[ItemSpec] = []
    for start in range(0, len(parsed.rows), options.batch):
        chunk = parsed.rows[start:start + options.batch]
        items.append({"person_id": None,
                      "payload": {"file": parsed.file, "tier": options.tier,
                                  "rows": [[sid, row_no, values] for sid, row_no, values in chunk]},
                      "label": f"rows {chunk[0][1]}–{chunk[-1][1]}"})
    return parsed, created, items


async def make_deps(run_id: int, item: dict, options: ImportOptions,
                    principal: Principal | None) -> AssistantDeps:
    return AssistantDeps(assistant=ASSISTANT, run_id=run_id, item_id=item["id"],
                         payload=item["payload"], label=item.get("label"), principal=principal,
                         budget=Budget(max_scrapes=0, max_searches=0, max_credits=0),
                         options=options)


async def run_item(deps: AssistantDeps) -> dict[str, Any]:
    rows = [(sid, row_no, values) for sid, row_no, values in deps.payload["rows"]]
    file, tier = deps.payload["file"], deps.payload["tier"]
    deps.emit("step", f"{len(rows)} rows of {file} → {tier}")
    inserted, updated, ids = await people.upsert_imported(rows, file, tier, datetime.now(UTC))
    embedded = 0
    if deps.options.embed and ids:
        embedded = await embeddings.sync_people(ids)
        deps.emit("step", f"{embedded} vectors for {len(ids)} people")
    return {"text": f"{inserted} inserted, {updated} updated, {embedded} vectors.",
            "changes": [], "inserted": inserted, "updated": updated, "embedded": embedded,
            "people": ids}


DETAILS = """\
**What it does.** Loads a CSV into people records as a run of row batches, so a large file is resumable and visible like any other run. The file is parsed when the run is created; each item then writes one batch of rows and embeds the people it touched.

**Starting it.** Upload a CSV on the People page or in this card, choose the tier (members or plus ones). Options: `batch` (200 rows per item), `embed` (true). The CLI does the same with `python -m app.cli import`. Free apart from the embedder.

**Parsing.** Every header becomes a property key (lowercase, underscores; a header that collides with a system key is prefixed `col_`). An `ID` column is the match key; otherwise a row is identified by name plus email, so re-importing the same file updates people instead of duplicating them. A row without a name (or first and last name) is skipped. Each column's cells are stored as entered, split into list items when the column is a list property or clearly holds lists; typed properties get their normalised value at write time. Properties the file introduces are created as free text, embeddable.

**Writing a batch.** Rows are upserted by (file, source id): a new id inserts, an existing one appends the changed cells to each property's history; unchanged cells append nothing and leave the update time alone. Rows sharing an id fold in file order so no entry is lost. Every write is compare-and-set, so an edit made while the import runs is never overwritten; a lost race is redone for that row alone. Then the passages of the touched people are embedded.

**Result.** Per batch: inserted, updated, vectors computed, and the ids written. An interrupted batch is redone on resume or after a restart without duplicating anything.
"""

SPEC = register(AssistantSpec(
    key=ASSISTANT,
    label="Import a CSV",
    description=("Loads a CSV into people records in batches: every column becomes a property "
                 "(new ones are created), rows are matched to existing records by file and id so "
                 "re-importing updates them, values are appended to each property's history, and "
                 "the passages are embedded as each batch lands."),
    options_model=ImportOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=lambda options: 0,
    scope="batches",
    details=DETAILS,
))
