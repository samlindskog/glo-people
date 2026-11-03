# Architecture: functionality levels

Every unit of functionality in `api/app` sits at one level. A level says who the unit is for and
what its result looks like; a module imports only lower levels (or listed peers at its own level),
and `tests/test_architecture.py` enforces it.

| Level | Name | Modules | Contract |
|---|---|---|---|
| 0 | Rules & adapters | `fields` (text toolkit: `fold`, `clean`, `distinct`, `sentence`), `names` (what a human name looks like: `is_full_name`, `title_case`, `looks_broken`, the company/handle/URL patterns), `history`, `normalize` (the parser; canonical forms for keyword/url/contact lookups), `property_specs` (typed specs plus the shared constants `IDENTITY_KEYS`, `DESCRIBES_PERSON`, `AUDIT_KEYS`, `MAX_ENUM_VALUE_CHARS`), `embedder`, `urls`, `config`, `db`, `mongo`, `auth`, `permissions`, `activity`, `observability`, `jobs`, `integrations/` | pure functions, data shapes, provider calls; no app-level decisions. `fields.MAX_TEXT_CHARS` (about 5000 words) is the only cap on a text value, stored or shown to an agent |
| 1 | Stores | `properties` (registry, enum sets, name vectors), `people` (documents; the only writer; `Transaction` stages one document's changes for one compare-and-set commit, replaying them on a concurrent write; `reparse` is the batch API the reparse assistant drives; `find_candidates` finds the people sharing an identity key or name with a record; `merge_documents` folds a duplicate document into another, every entry kept with its own time and source; `duplicate_groups` lists documents sharing a LinkedIn URL or email), `embeddings` (passage vectors; `sync_people` per batch), `importer` (incoming records: CSV parsing — a preamble skipped, `fields.NULL_TOKENS` as empty cells, `fields.HEADER_ALIASES` and a caller's column mapping —, the plan for one record, the match of a record to a person; writes nothing) | typed reads; writers return `WriteResult{person, changes}`; compare-and-set |
| 2 | Tools | `tools/search`, `tools/plan`, `tools/record`, `tools/store`, `tools/normalize`, `tools/repair`, `tools/site`, `tools/company`; `tools/invoke` (running one); `agents/common`, `agents/fakes` (model plumbing) | **one record, one shot**: a writing tool takes a `ToolContext` and only *stages* on its transaction; `tools.invoke` commits once after it returns, so a stopped tool has written nothing; activity through `ctx.emit`, Firecrawl spend on the context's budget; a Pydantic result for programs built on `history.Change` with `credits` and `calls`; no prose for humans; each tool's model agent is defined inside the tool |
| 3 | Assistants | per person: `assistants/enrich`, `assistants/normalize`, `assistants/update`; over batches: `assistants/merge`, `embed`, `reparse`; one item: `assistants/ask`; `assistants/base`, `runner`, `context`, `credits`; `assistants/auto_review` (the write observer that starts a `normalize` run over people written by human or API sources, debounced) | compose tools (per-person assistants change data only through `deps.tool`) or drive the level-1 batch APIs; return `text` for a human plus `changes`; every assistant is a run that persists its state, pauses, resumes, stops and survives a restart; the CLI executes its runs in its own process |
| 4 | Surfaces | `routers/`, `mcp_server`, `cli`, `main`, `wiring` (the composition root both processes share: what reacts to a person write), `web/` | adapt tools (structured, non-blocking invocations with an event stream) and assistants (runs with an event stream; text + changes) to REST, MCP, CLI, UI; no logic. The web has five pages: People (search, Ask as a run, the import dialog: column plans, match sample, dry run or merge), a person (tools run as invocations with live activity and a Stop), Properties, Assistants (start any assistant; running runs with live activity, credits, Pause / Resume / Stop; history), History (searches, activity) and Accounts (users, my API keys); the top bar shows the Firecrawl balance |

## The shared vocabulary: `history.Change`

```
Change{key, action: set | append | removed | normalized | flagged | property_created | enum_value_added | note,
       value, previous, flag}
```
Writers produce it (`people.Transaction`, through `update`, `create`, `update_norm`), tools return it
(`ToolResult.changes`), assistants carry it beside their text (`AssistantResult.changes`), surfaces
render it. `append` is a merge into the current value (list items unioned by `fields.fold`, a text
line added unless already present). `kept` is a value recorded in the history that does not replace
the shown one. `note` is something that did *not* happen and why (a normalised value dropped on
replay because its text changed concurrently); `assistants/review.describe` shows both.

**Which entry is shown** (`history.pick`): `h` is append-only and `v` is the shown value chosen from it
by the trust of each entry's source — a person's own edit (`ui`, `api`, `mcp`) outranks an assistant's
finding (`agent`), which outranks a bulk upload (`import`, `legacy`) — newest first within a class. An
entry flagged `derived` was computed from the value shown at the time (a list merge, a mechanical fix,
the repair agent's correction of a broken name, enrich's completion of a bare name, a rule's correction
of the kind) and supersedes the shown entry whatever its source, inheriting its trust. A weaker later
write is recorded and reported as `kept`; writing an earlier value again from a human surface restores
it. `python -m app.cli recompute-shown [--yes]` is the backfill (dry run reports the flips).

## Tools (level 2)

### The invocation contract

```
ctx = await invoke.make_context(person_id | None, source, principal, tier=…, source_doc=…, firecrawl=…, sink=…)   # None → 404; person_id None = a person the first commit creates
inv = invoke.invoke(tool, ctx, **kwargs)        # Invocation{id, status, events, result, credits, stop(), wait(), subscribe()}
result = await inv.wait()                        # or invoke.run(tool, ctx, **kwargs)
```

A writing tool is `tool(ctx: ToolContext, ...) -> ToolResult`. It reads `ctx.tx.view` (the record as
it will be), stages with `ctx.stage(values, append=…, tier=…, src=…)` / `ctx.stage_norm(parsed)` (the
one write path: they stage on the transaction and emit the `write` events), reports registry changes
it made with `ctx.tx.record_catalog`, and narrates with `ctx.emit(kind, message, **data)` (`step`,
`agent`, `provider`, `write`, `note`, `error`; the invocation adds the final `result`). Attribution
rule: an agent's verdict is written under the context's source (`agent <assistant>/<agent>`),
deterministic code under `system/<tool>` (the parser `system/normalize`, mechanical fixes
`system/repair`), a value copied from a file under its provenance (`import <file> row n`); a merge or a
correction of the shown value is staged `derived`. It never
writes: the invocation's driver commits `people.Transaction` once after the tool returns (rebasing on
a concurrent write), so `stop()` before that point leaves the record untouched and removes the
unused properties and enum values the tool created. A tool that calls another tool hands it
`ctx.child(agent)`: same transaction, same budget, same activity, the assistant prefix kept
(`enrich/storage` → `enrich/normalizer` → `enrich/repair`) — one commit for `store → normalize →
repair`. The model agent's tool calls and retries become `agent` events
(`run_agent(..., on_event)`), each paid Firecrawl call a `provider` event (`Budget.on_charge`).

| Tool | Call | Result |
|---|---|---|
| search | `run_search(SearchRequest)` | `SearchResponse` (read-only; `record_search` keeps the history) |
| plan | `plan_search(question, tier, filters, Tuning)` | `PlanResult{request: SearchRequest, rationale}` |
| record | `write(ctx, values, tier, append)` | `ToolResult` — the smallest write (keys in `append` merge into the current value); assistants and the merge's verbatim copies write through it |
| store | `store(ctx, passage, StoreOptions{max_new, near_duplicate, overflow, locked, audit_key})` | `StoreResult{changes, confirmed, ignored, conflicts, created_properties, redirected, to_notes, dropped, evidence, normalization}` — the agent's proposal is checked before anything is staged (evidence quoted from the passage, fold-compared; `locked` keys may only be confirmed; a confirmation needs a current value; every labelled line accounted for; a new-property proposal naming an existing key is redirected); list and `notes` writes append; facts beyond `max_new` are appended to notes or, with `overflow="ignore"`, dropped and reported; `audit_key` stages the passage itself in the same commit when anything was filed or confirmed; audit properties (`property_specs.AUDIT_KEYS`) are never shown to the agent as current values |
| normalize | `normalize(ctx, NormalizeOptions{allocate, max_new_values, agent, repair, force})` | `NormalizeResult{changes, agent_called, unmapped, merged, allocated, rejected, repair}` — the parser first (`system/normalize`; keyword, url and contact properties get their canonical lookup form here and are never asked of the agent), then, with `agent`, the normaliser for number / boolean / date / enum values the parser could not map (an enum text is merged into an existing value, stored as an alias, allocated as a new canonical value within `allocate` / `max_new_values`, or dropped), then, with `repair`, the repair tool when the record has no kind (or `force`) |
| repair | `repair(ctx, RepairOptions)` | `RepairResult{kind, name_source, signals, mechanical, agent_called, confidence, note, rejected, changes}` — mechanical fixes (`names.looks_broken`: mojibake, Title Case; `system/repair`), then — unless a full name behind a LinkedIn profile URL with nothing looking wrong settles it as a Person without a model call — the agent: `record_kind` (Person / Company / Unidentifiable, allocatable), a full name only from the record's own evidence (`property_specs.DESCRIBES_PERSON`, clipped by `DESCRIBE_CLIP`), misfiled values moved (an append on the target); never a data provider (the normalise assistant enriches a nameless person before repair); the kind is common sense (a human first name alone is a Person; a full name with a LinkedIn profile URL is a Person whatever the agent says; Unidentifiable only with nothing identifying) |
| site | `read_site(deps, name, url, markdown, links, follow_cap)` | `SiteFindings` (read-only; takes the agent plumbing's `AgentDeps`, not a `ToolContext`, because it stages nothing) |
| company | `research_company(deps, company, candidate, context, allow_search, pages_cap)` | `CompanySummary` (read-only; `AgentDeps` likewise) |

Tools may use other tools (`store → normalize`, `normalize → repair`, `plan → search`) and the agent plumbing; never
assistants or surfaces.

### The closing review (level 3, shared)

`assistants/review.review(deps, repair, force, allocate, max_new_values)` is the one closing step of
every per-record item: the normalize tool with repair on, under the agent name `normalizer` (so the
entries read `<assistant>/normalizer` and `<assistant>/repair`), returning the dict every item
carries as `result["review"]` (`kind, name, name_source, mechanical, note, rejected, repaired,
unmapped, merged, allocated, rejected_values, agent_called`); `describe(review, changes)` is its one
line for a human. Enrich, normalise and merge all end with it; nothing else re-judges a record.

## Assistants (level 3)

Each spec carries a one-line `description` and a long-form `details` text (the full functionality
spec), both returned by `GET /api/assistants` and MCP `list_assistants`; the Assistants page shows
the details in a "How it works" toggle on the card.

| Assistant | Scope | Items | Item result |
|---|---|---|---|
| ask | single (`people:read`) | the question (`options.question`, tuning, tier, filters) | `{text, citations, request, rationale, total, results, search_id}`; MCP `ask_people` calls `ask()` directly for a synchronous answer |
| enrich | people (selector `linkedin`) | one per person | `{text, changes, skipped, not_found, storage, passage, passage_hash, stored, confirmed, providers, review, credits, …}`; the data providers (`enrich/providers.py`: People Data Labs then FullEnrich by default, `options.providers`, `provider_policy="gaps"` asks the next one only for what is still missing) are asked in order and `budget.worst_case` must fit the item cap; identifiers are written directly (the employer's site as `company_website`, the person's own as `website`), everything prose- or tag-like from the providers and the site reader is one labelled passage the storage step files through the store tool (`max_new` = the run's `max_new_properties`, default 0; `overflow="ignore"`; `locked` = every key the ladder wrote itself; `audit_key="enrichment_passage"` keeps the passage in the same commit), then the shared closing review; a redo skips a paid step whose entry is already on the record (TTL), a refresh whose passage is unchanged makes no storage call |
| normalize | people (selector `name`) | one per person | `{text, changes, skipped, enrich, review, agent_called, unmapped, merged, allocated, rejected}`; the parser runs first (free, `agent=False`), then a person-looking record with a LinkedIn profile URL and still no full name is enriched (the enrich ladder on the same item and budget, `refresh` on; not again within the enrich TTL), then the shared closing review judges the completed record (forced when the record was Unidentifiable; the enrichment's own review counts when it settled a kind) |
| merge | batches | `MergeOptions{tier (created people only), max_new_properties, agent, review, dry_run}`; one item per incoming record: the surface parses a CSV (`importer.parse_csv`), creates its columns (`properties.ensure`) and groups the rows of one person (`merge.items_for_csv`), or `prepare` takes `records` (dicts; MCP `merge_person` merges one synchronously via `merge_one`) | `{text, changes, skipped, person_id, created, match{decision, tier, keys, candidates}, plan{direct, agent, unknown}, written, filed, review}`; `importer.match_record` decides by a shared identifier (email, phone, LinkedIn, GitHub, the file's ID, on canonical forms), else a unique or corroborated name, else `new`; several plausible people → item `skipped` with the candidates, nothing written; known values that are not long prose go verbatim through the record tool (`Source("import", file, "row n")` or the surface's), long prose and unknown keys to the store tool as labelled lines (`overflow="notes"`, `locked` = the verbatim keys), then the review; a dry run reports match and plan only. The runner takes `person_id` from the item result when the item had none |
| update | people | one per person; `UpdateOptions{values, append, tier, review, dry_run}` | `{text, written, kept, review, changes, skipped}` — the same edit on many people through the record tool under the starter's source, then the review when a property changed; `dry_run` reports `would`. The bulk edit behind "mark everyone from that upload as plus two" |
| embed | batches | `EmbedOptions{fields, batch}`; everyone or `person_ids`; property-name vectors synced in `prepare` | `{text, vectors}` per batch |
| reparse | batches | `ReparseOptions{force, batch}` | `{text, written}` per batch (`people.reparse`) |

`python -m app.cli import [--dry-run --no-agent --no-review --map HEADER=KEY]|embed|normalize --all|recompute-shown [--yes]` create these runs (import = a merge run) and execute them in the CLI
process (`worker = cli:<pid>`), printing the events; the API's scheduler leaves a run alone while
its owner heartbeats. Other CLI commands: `dedupe [--yes]` (documents sharing a LinkedIn URL or email: same name merged, oldest kept; else listed), `properties` (the catalog with coverage), `drop-property
<key>… [--yes]` (removes test leftovers: documents, vectors, registry; refuses spec keys),
`providers [--alexandria]` (the enrich ladder's provider table and, free, the Firecrawl catalogue's
people-data tools with live prices and inputs).

### Runs

The runner persists runs, items and events (`workflow_runs`, `workflow_run_items`,
`workflow_run_events`; the `workflow` column holds the assistant key). One scheduler per process
(`worker` = `api:<pid>` or `cli:<pid>`) dispatches the queued items of the runs it owns
round-robin, up to `assistant_concurrency` items in flight for the whole process, each item in
its own Logfire trace and inside the starter's context. Items are people (`person_id`), batches
or rows (`payload`), or the single item of a one-shot assistant (`scope`).

```
queued ──start──▶ running ──pause / credit cap / account credits──▶ pausing ──in-flight done──▶ paused ──resume(options)──▶ running
running ──all items done──▶ done        running | pausing | paused ──stop──▶ cancelled (in-flight tools stopped, queue skipped)
```

Credits: each in-flight item reserves its worst case (`spec.item_cap`; `spec.describe_cost`
tells a plan what the options cost and warns about a dear choice); when the run's
`max_credits` is reached the run pauses with `pause_reason = credit cap` and a resume with a
higher cap continues it; a Firecrawl 402 requeues the item and pauses every run that spends
(`account credits`). `done`, `failed` and `credits_used` are kept per item end. The owner
heartbeats every `run_heartbeat_s`; the API adopts a run whose worker is gone (none, stale beyond
`run_stale_s`, or any `api:` worker at startup), requeueing its running items (`attempts`; an
item interrupted `item_max_attempts` times fails). An item still running after `item_timeout_s` fails as `timed out`
(nothing written: tools stop on cancellation), and every model and embedding request has
its own deadline and retry count (`model_timeout_s`, `model_max_retries`), so one hung
request cannot hold a run. Shutdown cancels in-flight items (their tool
invocations roll back), requeues them and releases the runs, so a deploy loses nothing. Every
tool event during an item is persisted with the item and published on the in-process hub;
`runner.subscribe` replays then follows (polling the table for a run another process owns).
Events of runs finished more than `run_events_retention_days` ago are pruned.

## Surfaces (level 4)

REST: `POST /api/search` (tool), `POST /api/search/plan` (tool), `POST /api/search/ask`
(assistant, synchronous), `POST /api/people/import` (multipart CSV → a merge run; form `dry_run`, `agent`, `review`), `POST /api/people/import/preview` (column plans, match decisions of the first rows), `POST /api/people/merge` (one record now). `GET /api/people/find?name|email|phone|linkedin|github` (identity lookup: the merge assistant's match decision), `POST /api/people/{keep}/merge/{drop}` (fold a duplicate). The upload endpoints take `columns` (JSON header → key | id | skip). Runs: `GET /api/assistants`, `POST /api/assistants/{key}/plan` (the estimate before a run), `POST /api/assistants/{key}/runs` (a people assistant takes exactly one selector: `person_ids`, `from_run` — the people another run touched —, `where` filters with `tier`, or `all_with_field`; merge takes `records`), `GET /runs?status=active`,
`GET /runs/{id}` (+ `/items?status&after&limit`, `/events` as Server-Sent Events replayable with
`Last-Event-ID`), `POST /runs/{id}/pause | resume {options} | stop` (`cancel` is an alias); `GET
/api/credits` is the Firecrawl balance with the credits the running assistants reserve. A writing tool on one record is non-blocking: `POST
/api/people/{id}/store`, `/store/file` and `/normalize` answer 202 with the invocation (`?wait=true`
blocks for the result), `GET /api/invocations/{id}` has its state and result, `GET
/api/invocations/{id}/events` streams its activity (Server-Sent Events, replayable with
`Last-Event-ID`), `POST /api/invocations/{id}/stop` stops it before the commit (409 when too late).
MCP mirrors them: `search_people`, `plan_search`, `store_about_person`, `normalize_person` return
tool results and forward the activity as progress notifications while they run; `ask_people`
returns text; `find_people` is the identity lookup; `merge_people` folds a duplicate; `merge_person` merges one record and `enrich_person` enriches one (each a one-item run waited for, `runner.run_one`); `list_assistants`,
`start_assistant` (the same selectors, `records` for merge, `wait_seconds` to return the run finished), `plan_run` (the same selectors: people selected, what an enrichment can work on and would skip, the recent cost per item from `runner.history_averages`, estimated credits/tokens/minutes, the balance, a recommended `max_credits`), `get_credits`, `list_runs`, `get_run` (items with a compact result summary and `progress`: percent, items per minute, ETA, credits per done item, model tokens), `pause_run`, `resume_run`,
`stop_run` drive runs. Every item result carries `tokens` (the model usage of its agent calls, summed by `agents/common.USAGE`). The server instructions are a playbook for an agent acting for a non-technical person. `POST /api/assistants/{key}/runs` ignores empty ids in `person_ids` and
answers 422 when none is left.
Every REST handler and MCP tool docstring starts with its kind ("Tool:" / "Assistant:").
