# Architecture: functionality levels

Every unit of functionality in `api/app` sits at one level. A level says who the unit is for and
what its result looks like; a module imports only lower levels (or listed peers at its own level),
and `tests/test_architecture.py` enforces it.

| Level | Name | Modules | Contract |
|---|---|---|---|
| 0 | Rules & adapters | `fields`, `history`, `normalize` (the parser), `property_specs`, `embedder`, `urls`, `config`, `db`, `mongo`, `auth`, `permissions`, `activity`, `observability`, `jobs`, `integrations/` | pure functions, data shapes, provider calls; no app-level decisions |
| 1 | Stores | `properties` (registry, enum sets, name vectors), `people` (documents; the only writer), `embeddings` (passage vectors) | typed reads; writers return `WriteResult{person, changes}`; compare-and-set |
| 2 | Tools | `tools/search`, `tools/plan`, `tools/store`, `tools/normalize`, `tools/site`, `tools/company`; `agents/common`, `agents/fakes` (model plumbing) | **one shot**; side effects only through level-1 writers; a Pydantic result for programs built on `history.Change`; no prose for humans; each tool's model agent is defined inside the tool |
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

| Tool | Call | Result |
|---|---|---|
| search | `run_search(SearchRequest)` | `SearchResponse` (read-only; `record_search` keeps the history) |
| plan | `plan_search(question, tier, filters, Tuning)` | `PlanResult{request: SearchRequest, rationale}` |
| store | `store(person_id, passage, StoreOptions, principal, source)` | `StoreResult{changes, confirmed, ignored, conflicts, created_properties, redirected, to_notes, normalization}` |
| normalize | `normalize(person_id, principal, source, NormalizeOptions, firecrawl)` | `NormalizeResult{changes, agent_called, unmapped, merged, allocated, rejected, repair}` — for enum properties the agent merges a text into an existing value (stored as an alias), allocates a new canonical value within `allocate` / `max_new_values`, or drops it |
| repair | `repair(person_id, principal, source, RepairOptions, firecrawl)` | `RepairResult{kind, name_source, signals, mechanical, agent_called, provider_called, credits, confidence, rejected, changes}` — mechanical fixes (mojibake, Title Case), then the agent: `record_kind` (Person / Company / Unidentifiable, allocatable), a full name only from the record's own evidence or the FullEnrich profile behind the LinkedIn URL, misfiled values moved; a Person without a full name is Unidentifiable |
| site | `read_site(deps, name, url, markdown, links, follow_cap)` | `SiteFindings` |
| company | `research_company(deps, company, candidate, context, allow_search, pages_cap)` | `CompanySummary` |

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
(assistant), `POST /api/people/{id}/store` and `/store/file` (tool), `POST /api/people/{id}/normalize`
(tool), `/api/assistants…` (runs). MCP mirrors them: `search_people`, `plan_search`,
`store_about_person`, `normalize_person` return tool results; `ask_people` returns text.
Every REST handler and MCP tool docstring starts with its kind ("Tool:" / "Assistant:").
