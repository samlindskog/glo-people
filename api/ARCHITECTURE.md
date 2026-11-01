# Architecture: functionality levels

Every unit of functionality in `api/app` sits at one level. A level says who the unit is for and
what its result looks like; a module imports only lower levels (or listed peers at its own level),
and `tests/test_architecture.py` enforces it.

| Level | Name | Modules | Contract |
|---|---|---|---|
| 0 | Rules & adapters | `fields`, `history`, `normalize` (the parser), `property_specs`, `embedder`, `urls`, `config`, `db`, `mongo`, `auth`, `permissions`, `activity`, `observability`, `jobs`, `integrations/` | pure functions, data shapes, provider calls; no app-level decisions. `fields.MAX_TEXT_CHARS` (about 5000 words) is the only cap on a text value, stored or shown to an agent |
| 1 | Stores | `properties` (registry, enum sets, name vectors), `people` (documents; the only writer; `Transaction` stages one document's changes for one compare-and-set commit, replaying them on a concurrent write; `reparse`, `upsert_imported` are the batch APIs assistants drive), `embeddings` (passage vectors; `sync_people` per batch), `importer` (CSV parsing) | typed reads; writers return `WriteResult{person, changes}`; compare-and-set |
| 2 | Tools | `tools/search`, `tools/plan`, `tools/record`, `tools/store`, `tools/normalize`, `tools/repair`, `tools/site`, `tools/company`; `tools/invoke` (running one); `agents/common`, `agents/fakes` (model plumbing) | **one record, one shot**: a writing tool takes a `ToolContext` and only *stages* on its transaction; `tools.invoke` commits once after it returns, so a stopped tool has written nothing; activity through `ctx.emit`, Firecrawl spend on the context's budget; a Pydantic result for programs built on `history.Change` with `credits` and `calls`; no prose for humans; each tool's model agent is defined inside the tool |
| 3 | Assistants | per person: `assistants/enrich`, `assistants/normalize`; over batches: `assistants/import_csv`, `embed`, `reparse`; one item: `assistants/ask`; `assistants/base`, `runner`, `context`, `credits` | compose tools (per-person assistants change data only through `deps.tool`) or drive the level-1 batch APIs; return `text` for a human plus `changes`; every assistant is a run that persists its state, pauses, resumes, stops and survives a restart; the CLI executes its runs in its own process |
| 4 | Surfaces | `routers/`, `mcp_server`, `cli`, `main`, `web/` | adapt tools (structured, non-blocking invocations with an event stream) and assistants (runs with an event stream; text + changes) to REST, MCP, CLI, UI; no logic. The web has five pages: People (search, Ask as a run, the import dialog), a person (tools run as invocations with live activity and a Stop), Properties, Assistants (start any assistant; running runs with live activity, credits, Pause / Resume / Stop; history), History (searches, activity) and Accounts (users, my API keys); the top bar shows the Firecrawl balance |

## The shared vocabulary: `history.Change`

```
Change{key, action: set | append | removed | normalized | flagged | property_created | enum_value_added,
       value, previous, flag}
```
Writers produce it (`people.update`, `create`, `update_norm`), tools return it (`ToolResult.changes`),
assistants carry it beside their text (`AssistantResult.changes`), surfaces render it.

## Tools (level 2)

### The invocation contract

```
ctx = await invoke.make_context(person_id, source, principal, firecrawl=…, sink=…)   # None → 404
inv = invoke.invoke(tool, ctx, **kwargs)        # Invocation{id, status, events, result, credits, stop(), wait(), subscribe()}
result = await inv.wait()                        # or invoke.run(tool, ctx, **kwargs)
```

A writing tool is `tool(ctx: ToolContext, ...) -> ToolResult`. It reads `ctx.tx.view` (the record as
it will be), stages with `ctx.tx.set_values` / `set_norm`, reports registry changes it made with
`ctx.tx.record_catalog`, and narrates with `ctx.emit(kind, message, **data)` (`step`, `agent`,
`provider`, `write`, `note`, `error`; the invocation adds the final `result`). It never writes:
the invocation's driver commits `people.Transaction` once after the tool returns (rebasing on a
concurrent write), so `stop()` before that point leaves the record untouched and removes the
unused properties and enum values the tool created. A tool that calls another tool hands it
`ctx.child(agent)`: same transaction, same budget, same activity — one commit for `store →
normalize → repair`. The model agent's tool calls and retries become `agent` events
(`run_agent(..., on_event)`), each paid Firecrawl call a `provider` event (`Budget.on_charge`).

| Tool | Call | Result |
|---|---|---|
| search | `run_search(SearchRequest)` | `SearchResponse` (read-only; `record_search` keeps the history) |
| plan | `plan_search(question, tier, filters, Tuning)` | `PlanResult{request: SearchRequest, rationale}` |
| record | `write(ctx, values, tier)` | `ToolResult` — the smallest write; assistants write through it |
| store | `store(ctx, passage, StoreOptions{max_new, near_duplicate, overflow})` | `StoreResult{changes, confirmed, ignored, conflicts, created_properties, redirected, to_notes, dropped, evidence, normalization}` — facts beyond `max_new` go to notes or, with `overflow="ignore"`, are dropped and reported; `evidence` quotes the passage words each write rests on; audit properties (`enrichment_passage`) are never shown to the agent as current values |
| normalize | `normalize(ctx, NormalizeOptions)` | `NormalizeResult{changes, agent_called, unmapped, merged, allocated, rejected, repair}` — for enum properties the agent merges a text into an existing value (stored as an alias), allocates a new canonical value within `allocate` / `max_new_values`, or drops it |
| repair | `repair(ctx, RepairOptions)` | `RepairResult{kind, name_source, signals, mechanical, agent_called, confidence, rejected, changes}` — mechanical fixes (mojibake, Title Case), then the agent: `record_kind` (Person / Company / Unidentifiable, allocatable), a full name only from the record's own evidence, misfiled values moved; never a data provider (the normalise assistant enriches a nameless person before repair); a full name behind a LinkedIn profile URL is a Person |
| site | `read_site(deps, name, url, markdown, links, follow_cap)` | `SiteFindings` (read-only) |
| company | `research_company(deps, company, candidate, context, allow_search, pages_cap)` | `CompanySummary` (read-only) |

Tools may use other tools (`store → normalize`, `normalize → repair`, `plan → search`) and the agent plumbing; never
assistants or surfaces.

## Assistants (level 3)

Each spec carries a one-line `description` and a long-form `details` text (the full functionality
spec), both returned by `GET /api/assistants` and MCP `list_assistants`; the Assistants page shows
the details in a "How it works" toggle on the card.

| Assistant | Scope | Items | Item result |
|---|---|---|---|
| ask | single (`people:read`) | the question (`options.question`, tuning, tier, filters) | `{text, citations, request, rationale, total, results, search_id}`; MCP `ask_people` calls `ask()` directly for a synchronous answer |
| enrich | people (selector `linkedin`) | one per person | `{text, changes, skipped, not_found, storage, passage, passage_hash, stored, confirmed, providers, credits, …}`; the data providers (`enrich/providers.py`: FullEnrich, People Data Labs opt-in; `options.providers`) are asked in order and `budget.worst_case` must fit the item cap; identifiers are written directly, everything prose- or tag-like from the providers and the site reader is one labelled passage the last step files through the store tool (`max_new` = the run's `max_new_properties`, default 0; `overflow="ignore"`) and keeps on the record as `enrichment_passage`; a redo skips a paid step whose entry is already on the record (TTL), a refresh whose passage is unchanged makes no storage call |
| normalize | people (selector `name`) | one per person | `{text, changes, skipped, enrich, agent_called, unmapped, merged, allocated, rejected, repair}`; the free stages run first (parser, normaliser agent), then a person-looking record with a LinkedIn URL and still no full name is enriched (the enrich ladder on the same item and budget, `refresh` on), then repair judges the completed record |
| import | batches | `ImportOptions{tier, batch, embed}`; the surface parses the CSV (`importer.parse_csv`), creates the properties and cuts the rows into items (`import_csv.prepare_csv`) | `{text, inserted, updated, embedded, people}` per batch (`people.upsert_imported` + `embeddings.sync_people`) |
| embed | batches | `EmbedOptions{fields, batch}`; everyone or `person_ids`; property-name vectors synced in `prepare` | `{text, vectors}` per batch |
| reparse | batches | `ReparseOptions{force, batch}` | `{text, written}` per batch (`people.reparse`) |

`python -m app.cli import|embed|normalize --all` create these runs and execute them in the CLI
process (`worker = cli:<pid>`), printing the events; the API's scheduler leaves a run alone while
its owner heartbeats. Other CLI commands: `properties` (the catalog with coverage), `drop-property
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

Credits: each in-flight item reserves its worst case (`spec.item_cap`); when the run's
`max_credits` is reached the run pauses with `pause_reason = credit cap` and a resume with a
higher cap continues it; a Firecrawl 402 requeues the item and pauses every run that spends
(`account credits`). `done`, `failed` and `credits_used` are kept per item end. The owner
heartbeats every `run_heartbeat_s`; the API adopts a run whose worker is gone (none, stale beyond
`run_stale_s`, or any `api:` worker at startup), requeueing its running items (`attempts`; an
item interrupted `item_max_attempts` times fails). Shutdown cancels in-flight items (their tool
invocations roll back), requeues them and releases the runs, so a deploy loses nothing. Every
tool event during an item is persisted with the item and published on the in-process hub;
`runner.subscribe` replays then follows (polling the table for a run another process owns).
Events of runs finished more than `run_events_retention_days` ago are pruned.

## Surfaces (level 4)

REST: `POST /api/search` (tool), `POST /api/search/plan` (tool), `POST /api/search/ask`
(assistant, synchronous), `POST /api/people/import` (multipart CSV → an import run). Runs: `GET /api/assistants`, `POST /api/assistants/{key}/runs`, `GET /runs?status=active`,
`GET /runs/{id}` (+ `/items?status&after&limit`, `/events` as Server-Sent Events replayable with
`Last-Event-ID`), `POST /runs/{id}/pause | resume {options} | stop` (`cancel` is an alias); `GET
/api/credits` is the Firecrawl balance with the credits the running assistants reserve. A writing tool on one record is non-blocking: `POST
/api/people/{id}/store`, `/store/file` and `/normalize` answer 202 with the invocation (`?wait=true`
blocks for the result), `GET /api/invocations/{id}` has its state and result, `GET
/api/invocations/{id}/events` streams its activity (Server-Sent Events, replayable with
`Last-Event-ID`), `POST /api/invocations/{id}/stop` stops it before the commit (409 when too late).
MCP mirrors them: `search_people`, `plan_search`, `store_about_person`, `normalize_person` return
tool results and forward the activity as progress notifications while they run; `ask_people`
returns text; `list_assistants`, `start_assistant`, `get_run`, `pause_run`, `resume_run`,
`stop_run` drive runs.
Every REST handler and MCP tool docstring starts with its kind ("Tool:" / "Assistant:").
