"""Assistant (level 3): merge incoming records into the people database. A record is a CSV row or
a dict handed in over the API or MCP. Per record: match it to a person (a shared identifier, else
a unique or corroborated name) or create one; write the values that map to a known property
verbatim through the record tool (list items appended, typed values normalised by the parser);
hand long prose and columns the catalog does not know to the storage agent as labelled lines;
then the closing review every assistant shares. Several plausible people for one record is
`ambiguous`: the item is skipped with the candidates listed, a human decides, the run is redone.
Replaces the CSV import: the same endpoint, every column still a property, but matched by
identity rather than by a file-scoped id, and with history entries for every write."""

from typing import Any

from pydantic import BaseModel, Field

from app import fields, importer, people, properties
from app.assistants import runner
from app.assistants.base import AssistantSpec, ItemSpec, register
from app.assistants.context import AssistantDeps
from app.assistants.review import review
from app.auth import Principal
from app.history import Source
from app.integrations.firecrawl import Budget
from app.tools import record
from app.tools import store as store_tool

ASSISTANT = "merge"


class MergeOptions(BaseModel):
    tier: fields.Tier = Field("member", description="Tier for the people the run "
                                                                     "creates; a matched person "
                                                                     "keeps theirs")
    max_new_properties: int = Field(3, ge=0, le=5,
                                    description="Properties the storage agent may create per "
                                                "record for facts the catalog has no home for")
    agent: bool = Field(True, description="Hand long prose and keys with no property to the "
                                          "storage agent; off, every known value is written as "
                                          "entered and unknown keys are left out")
    review: bool = Field(True, description="Close each record with the review (normaliser and "
                                           "repair agents) when something was written")
    dry_run: bool = Field(False, description="Match and plan every record, write nothing")


def clean_values(values: dict[str, Any]) -> dict[str, Any]:
    """Cell text as the record gives it, minus invisible characters, whitespace noise and
    empties; a value's line breaks stay."""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, list):
            items = [fields.clean(v, keep_lines=True) if isinstance(v, str) else v for v in value]
            items = [v for v in items if v not in (None, "")]
            if items:
                out[key] = items
        elif isinstance(value, str):
            if value.strip():
                out[key] = fields.clean(value, keep_lines=True)
        elif value is not None:
            out[key] = value
    return out


async def prepare_csv(text: str, filename: str, *, dry_run: bool = False,
                      columns: dict[str, str] | None = None
                      ) -> tuple[importer.ParsedCsv, list[str], list[ItemSpec]]:
    """Parse a CSV (with the caller's column mapping), create the properties its columns need (not
    on a dry run) and cut it into items: the step the upload surfaces (REST, CLI) share. Returns
    (parsed, created keys, items)."""
    parsed = importer.parse_csv(text, filename, columns)
    created = [] if dry_run else await properties.ensure(parsed.used_keys, "import")
    return parsed, created, items_for_csv(parsed)


def items_for_csv(parsed: importer.ParsedCsv) -> list[ItemSpec]:
    """One item per person the file describes (rows sharing an ID, or a name and email, fold into
    one item in file order)."""
    items: list[ItemSpec] = []
    for rows in importer.group_rows(parsed.rows):
        first = rows[0]
        label = str(first.values.get("name") or f"row {first.row_no}")
        if len(rows) > 1:
            label += f" (rows {rows[0].row_no}–{rows[-1].row_no})"
        items.append({"person_id": None, "label": label,
                      "payload": {"file": parsed.file, "source_id": first.source_id,
                                  "rows": [{"row": r.row_no, "values": r.values} for r in rows]}})
    return items


async def prepare(body: dict[str, Any], options: BaseModel) -> list[ItemSpec]:
    """Items from `records` (dicts of property → value) handed in over the API or MCP."""
    records = body.get("records") or []
    if not records:
        raise ValueError("records is required: a list of {property: value} dicts")
    for n, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            raise TypeError(f"record {n} is not an object")
    items: list[ItemSpec] = []
    named = [(n, rec) for n, rec in enumerate(records, start=1)
             if isinstance(rec.get("person_id"), str)]
    for n, rec in named:                                  # a record naming its person: its own item
        items.append({"person_id": rec["person_id"], "label": str(rec.get("name") or f"record {n}"),
                      "payload": {"file": None, "source_id": None,
                                  "rows": [{"row": None, "values": {k: v for k, v in rec.items()
                                                                    if k != "person_id"}}]}})
    rows = [importer.Row(n, rec) for n, rec in enumerate(records, start=1)
            if not isinstance(rec.get("person_id"), str)]
    for group in importer.group_rows(rows):              # records of one person: one item, in order
        first = group[0]
        items.append({"person_id": None,
                      "label": str(first.values.get("name") or f"record {first.row_no}"),
                      "payload": {"file": None, "source_id": None,
                                  "rows": [{"row": None, "values": r.values} for r in group]}})
    return items


async def make_deps(run_id: int, item: dict, options: MergeOptions,
                    principal: Principal | None) -> AssistantDeps:
    return AssistantDeps(assistant=ASSISTANT, run_id=run_id, item_id=item["id"],
                         person=item.get("person"), payload=item["payload"],
                         label=item.get("label"), principal=principal,
                         budget=Budget(max_scrapes=0, max_searches=0, max_credits=0),
                         options=options)


def _source(deps: AssistantDeps, file: str | None, row: int | None) -> Source:
    """A CSV cell keeps its provenance (file and row); a record handed in directly is attributed
    to whoever handed it in (the surface's source when it gave one, else the run's starter)."""
    if file:
        return Source("import", file, f"row {row}" if row is not None else None)
    given = deps.payload.get("source")
    if isinstance(given, dict) and given.get("kind"):
        return Source(**given)
    if deps.principal is not None:
        return Source.for_rest(deps.principal)
    return deps.source("record")


async def run_item(deps: AssistantDeps) -> dict[str, Any]:
    options: MergeOptions = deps.options
    payload = deps.payload
    rows = payload["rows"]
    file, source_id = payload.get("file"), payload.get("source_id")
    catalog = {p.key: p for p in await properties.catalog()}
    cleaned = [clean_values(r["values"]) for r in rows]
    first = cleaned[0]
    source_doc = {"file": file, "source_id": source_id} if file and source_id else None
    if deps.person is not None:                                  # a record naming its person
        match = importer.Match("matched", deps.person["id"], "person_id", ["person_id"])
    else:
        match = await importer.match_record(first, source_doc)
    plans = [importer.plan_row(values, catalog, agent=options.agent) for values in cleaned]
    for plan in plans:
        for key in plan.unknown if not options.agent else ():
            deps.skip(key, "no such property")
    base: dict[str, Any] = {"match": match.public(), "plan": plans[0].public(),
                            "created": False, "person_id": match.person_id,
                            "written": [], "filed": [], "kept": [], "changes": [], "skipped": deps.skipped}
    name = first.get("name") or (deps.person or {}).get("name") or "the record"
    if match.decision == "ambiguous":
        who = ", ".join(c["name"] or c["person_id"] for c in match.candidates[:4])
        deps.emit("note", f"ambiguous: {len(match.candidates)} people could be {name}: {who}")
        return {**base, "status": "skipped",
                "text": f"Ambiguous: {len(match.candidates)} people could be {name} ({who}); "
                        "nothing written."}
    if match.decision == "new" and not first.get("name"):
        return {**base, "status": "skipped", "text": "No name: nothing to create."}
    if options.dry_run:
        verb = "would create" if match.decision == "new" else f"would match ({match.tier})"
        return {**base, "dry_run": True,
                "text": f"{name}: {verb}; verbatim {', '.join(base['plan']['direct']) or 'nothing'}"
                        f"; to the storage agent {', '.join(base['plan']['agent']) or 'nothing'}."}

    if match.decision == "matched" and deps.person is None:
        deps.person = await people.get(match.person_id)       # type: ignore[arg-type]
        if deps.person is None:
            return {**base, "status": "failed", "error": "person not found", "text": "Person not found."}
    elif match.decision == "new":
        deps.new_tier = options.tier
        if file:
            deps.new_source_doc = {"file": file, "source_id": source_id, "row": rows[0]["row"]}
        deps.emit("step", f"creating {name} ({options.tier})")
    else:
        deps.emit("step", f"matched {name} by {match.tier}: {', '.join(match.keys)}")

    written: set[str] = set()
    filed: set[str] = set()
    kept: set[str] = set()
    for row, values, plan in zip(rows, cleaned, plans, strict=True):
        src = _source(deps, file, row.get("row"))
        skip = importer.restated(plan.direct, deps.person, catalog) if deps.person else set()
        direct = {k: v for k, v in plan.direct.items() if k not in skip}
        if direct:
            res = await deps.tool(record.write, "record", source=src, values=direct,
                                  append=[k for k in plan.appends if k in direct])
            if res is None:
                return {**base, "status": "failed", "error": "person vanished", "text": "Person vanished."}
            written |= {c.key for c in res.changes if c.action in ("set", "append")}
            kept |= {c.key for c in res.changes if c.action == "kept"}
        if plan.lines and deps.person is not None:
            opts = store_tool.StoreOptions(max_new=options.max_new_properties, overflow="notes",
                                           locked=sorted(plan.direct))
            res = await deps.tool(store_tool.store, "storage",          # the agent's own writes
                                  passage=importer.render_lines(plan.lines), opts=opts)
            if isinstance(res, store_tool.StoreResult):
                filed |= {c.key for c in res.changes if c.action in ("set", "append")}
                kept |= set(res.kept)
    assert deps.person is not None
    rev = None
    if options.review and (deps.changes or match.decision == "new" or not deps.current("record_kind")):
        rev = await review(deps)

    parts = [f"created {name} ({options.tier})" if match.decision == "new"
             else f"matched {name} by {match.tier}"]
    if written:
        parts.append(f"wrote {', '.join(sorted(written))}")
    if filed:
        parts.append(f"filed {', '.join(sorted(filed))}")
    if kept:
        parts.append(f"recorded but not shown: {', '.join(sorted(kept))}")
    if not written and not filed and not kept:
        parts.append("nothing new")
    if rev and rev.get("kind"):
        parts.append(f"record kind {rev['kind']}")
    return {**base, "person_id": deps.person["id"], "created": match.decision == "new",
            "written": sorted(written), "filed": sorted(filed), "kept": sorted(kept), "review": rev,
            "changes": [c.model_dump() for c in deps.changes], "skipped": deps.skipped,
            "text": fields.sentence("; ".join(parts)) + "."}


async def merge_one(record: dict[str, Any], options: MergeOptions, principal: Principal | None,
                    source: Source | None = None, timeout: float = 120) -> dict[str, Any]:
    """Merge one record now (the REST and MCP surfaces): a one-item run, waited for. Its verbatim
    writes are attributed to `source` (the surface's). Returns the item's result with the run and
    item ids and the item's status."""
    items = await prepare({"records": [record]}, options)
    if source is not None:
        items[0]["payload"]["source"] = source.doc()
    return await runner.run_one(SPEC, principal, items, {"options": options.model_dump(mode="json")},
                                timeout=timeout)


DETAILS = """\
**What it does.** Takes incoming records (the rows of a CSV, or records handed in over the API or MCP) and merges each into the people database: it finds the person the record is about or creates one, writes the values that belong to a known property verbatim, hands long prose and unknown columns to the storage agent, and closes with the review every assistant shares. It replaces the CSV import: the same upload, every column still a property, but matched by identity instead of a file-scoped id, and with a history entry for every write.

**Starting it.** Upload a CSV on the People page or in this card (preview first: each column's plan and the match decisions of the first rows; a dry run writes nothing), or start a run over `records` through the API or MCP (`merge_person` merges one record synchronously). Options: `tier` (member: for the people the run creates; a matched person keeps theirs), `max_new_properties` (3: properties the storage agent may create per record), `agent` (true: long prose and unknown keys go to the storage agent; off, every known value is written as entered), `review` (true), `dry_run` (false). Free apart from the model calls of the storage agent and the review.

**Parsing a CSV.** Every header becomes a property key (lowercase, underscores; a header that collides with a system key is prefixed `col_`); columns the catalog lacks are created as free text before the run. Cells keep their text as entered and list columns are split into items. A row without a name (or first and last name) is skipped. Rows that describe the same person (the same `ID`, the same name, or a shared email, phone, LinkedIn or GitHub URL, transitively) fold into one item in file order, so two items never create the same person at once.

**Matching.** A record names its person when it carries an identifier the database already holds: an email or phone, the LinkedIn or GitHub URL, or the file's `ID`, compared on canonical forms (so a LinkedIn URL matches whatever its spelling). Without one, a name that exactly one person has decides; a name several people share is decided by company, location or email domain when exactly one of them agrees. Otherwise the record is ambiguous: the item is skipped with the candidates listed, nothing is written, a human decides and the run is redone. A record no one matches creates a person in the run's tier.

**Writing.** Values for known properties that are not long prose are written as entered through the record tool, attributed to the file and row (`import` entries) or to whoever handed the record in; list properties append their items, typed properties get their normalised value from the parser, and an identifier the record restates in another spelling (an email's case, a URL variant) adds nothing. A value that does not outrank the one shown (an upload against a person's own edit or an assistant's finding) is recorded in the history and reported as kept, not shown. Long prose and unknown columns go to the storage agent as labelled lines (one per property), which files each into its property in the type's form, confirms what the record already has, marks conflicts (the old value stays in history), and may create up to `max_new_properties` properties; facts with no home go to notes. Blank cells never clear a value; re-merging the same file appends nothing.

**Review.** When anything was written or the person is new, the closing review runs: the parser confirms the typed values, the normaliser agent reads what it cannot, and the repair tool judges the record as it now stands (kind by common sense, a full name the record's own data states, misfiled values).

**Result.** Per record: matched (by what) or created, the properties written verbatim and filed by the agent, the review, every change, and for an ambiguous record the candidates. A dry run reports the match decision and the plan instead.
"""

SPEC = register(AssistantSpec(
    key=ASSISTANT,
    label="Merge incoming records",
    description=("Matches each incoming record (CSV row or a record over the API) to a person by "
                 "identifier or name, or creates one; writes known values verbatim, hands prose "
                 "and unknown columns to the storage agent, and closes with the review. Ambiguous "
                 "records are skipped with their candidates listed."),
    options_model=MergeOptions,
    make_deps=make_deps,
    run_item=run_item,
    item_cap=lambda options: 0,
    scope="batches",
    prepare=prepare,
    details=DETAILS,
))
