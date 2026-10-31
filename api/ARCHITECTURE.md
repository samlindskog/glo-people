# Architecture: functionality levels

Every unit of functionality in `api/app` sits at one level. A level says who the unit is for and
what its result looks like; a module imports only lower levels (or listed peers at its own level),
and `tests/test_architecture.py` enforces it.

| Level | Name | Modules | Contract |
|---|---|---|---|
| 0 | Rules & adapters | `fields`, `history`, `normalize` (the parser), `property_specs`, `embedder`, `urls`, `config`, `db`, `mongo`, `auth`, `permissions`, `activity`, `observability`, `jobs`, `integrations/` | pure functions, data shapes, provider calls; no app-level decisions. `fields.MAX_TEXT_CHARS` (about 5000 words) is the only cap on a text value, stored or shown to an agent |
| 1 | Stores | `properties` (registry, enum sets, name vectors), `people` (documents; the only writer; `Transaction` stages one document's changes for one compare-and-set commit, replaying them on a concurrent write), `embeddings` (passage vectors) | typed reads; writers return `WriteResult{person, changes}`; compare-and-set |
| 2 | Tools | `tools/search`, `tools/plan`, `tools/record`, `tools/store`, `tools/normalize`, `tools/repair`, `tools/site`, `tools/company`; `tools/invoke` (running one); `agents/common`, `agents/fakes` (model plumbing) | **one record, one shot**: a writing tool takes a `ToolContext` and only *stages* on its transaction; `tools.invoke` commits once after it returns, so a stopped tool has written nothing; activity through `ctx.emit`, Firecrawl spend on the context's budget; a Pydantic result for programs built on `history.Change` with `credits` and `calls`; no prose for humans; each tool's model agent is defined inside the tool |
| 3 | Assistants | `assistants/ask`; the per-person assistants `assistants/enrich`, `assistants/normalize`; `assistants/base`, `runner`, `context` (runs over many people) | compose tools; may change data through them; return `text` for a human plus `changes` |
| 4 | Surfaces | `routers/`, `mcp_server`, `cli`, `importer`, `main`, `web/` | adapt tools (structured) and assistants (text + changes) to REST, MCP, CLI, CSV, UI; no logic |

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
| store | `store(ctx, passage, StoreOptions)` | `StoreResult{changes, confirmed, ignored, conflicts, created_properties, redirected, to_notes, normalization}` |
| normalize | `normalize(ctx, NormalizeOptions)` | `NormalizeResult{changes, agent_called, unmapped, merged, allocated, rejected, repair}` — for enum properties the agent merges a text into an existing value (stored as an alias), allocates a new canonical value within `allocate` / `max_new_values`, or drops it |
| repair | `repair(ctx, RepairOptions)` | `RepairResult{kind, name_source, signals, mechanical, agent_called, provider_called, credits, confidence, rejected, changes}` — mechanical fixes (mojibake, Title Case), then the agent: `record_kind` (Person / Company / Unidentifiable, allocatable), a full name only from the record's own evidence or the FullEnrich profile behind the LinkedIn URL (`ctx.firecrawl`), misfiled values moved; a Person without a full name is Unidentifiable |
| site | `read_site(deps, name, url, markdown, links, follow_cap)` | `SiteFindings` (read-only) |
| company | `research_company(deps, company, candidate, context, allow_search, pages_cap)` | `CompanySummary` (read-only) |

Tools may use other tools (`store → normalize`, `normalize → repair`, `plan → search`) and the agent plumbing; never
assistants or surfaces.

## Assistants (level 3)

| Assistant | Call | Result |
|---|---|---|
| ask | `ask(question, Tuning, principal, tier, filters, source)` | `AskResult{text, citations, request, rationale, total, results, search_id}` |
| enrich | per person via the runner (`POST /api/assistants/enrich/runs`) | item result `{text, changes, skipped, not_found, credits, …}` |
| normalize | per person via the runner | item result `{text, changes, agent_called, unmapped}` |

The runner persists runs and items (`workflow_runs`, `workflow_run_items`; the `workflow` column
holds the assistant key), caps credits per item and per run, and gives every item its own trace.

## Surfaces (level 4)

REST: `POST /api/search` (tool), `POST /api/search/plan` (tool), `POST /api/search/ask`
(assistant), `/api/assistants…` (runs). A writing tool on one record is non-blocking: `POST
/api/people/{id}/store`, `/store/file` and `/normalize` answer 202 with the invocation (`?wait=true`
blocks for the result), `GET /api/invocations/{id}` has its state and result, `GET
/api/invocations/{id}/events` streams its activity (Server-Sent Events, replayable with
`Last-Event-ID`), `POST /api/invocations/{id}/stop` stops it before the commit (409 when too late).
MCP mirrors them: `search_people`, `plan_search`, `store_about_person`, `normalize_person` return
tool results and forward the activity as log messages while they run; `ask_people` returns text.
Every REST handler and MCP tool docstring starts with its kind ("Tool:" / "Assistant:").
