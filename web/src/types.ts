import type contract from '@shared/permissions.json'

export type Permission = keyof typeof contract.permissions
export type Tier = 'member' | 'plus_one'

export interface Me {
  id: number
  username: string
  role: string
  permissions: Permission[]
  via_api_key: boolean
}

export type FilterOp =
  | 'eq'
  | 'ne'
  | 'contains'
  | 'starts_with'
  | 'regex'
  | 'in'
  | 'exists'
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'

export interface FieldFilter {
  field: string
  op: FilterOp
  value?: unknown
}

/** A passage compared with one embeddable property (the planner agent writes these). */
export interface FieldQuery {
  field: string
  query: string
}

export interface SemanticQuery {
  /** One passage compared with every property in `fields`. */
  query: string
  fields: string[]
  /** A passage per property; may be combined with query + fields. */
  queries?: FieldQuery[]
  mode: 'avg' | 'max'
  threshold: number
  require_all_fields: boolean
}

export interface SearchRequest {
  tier?: Tier | null
  filters: FieldFilter[]
  semantic?: SemanticQuery | null
  sort: 'score' | { field: string; dir: 'asc' | 'desc' }
  limit: number
  page: number
}

export type FieldValue = string | number | boolean | Array<string | number | boolean>

export type SourceKind = 'ui' | 'api' | 'mcp' | 'agent' | 'import' | 'legacy' | 'system'
export type NormFlag = 'unmapped' | 'non_normalizable'

/** One history entry of a field: the value written, when, and by whom. */
export interface FieldEntry {
  v: FieldValue | null
  ts?: string
  src?: { kind: SourceKind; by?: string; ref?: string }
}

/** One normalised-value entry: the typed value, when, by whom, and `of`, a hash of the text it derives from. */
export interface NormEntry extends FieldEntry {
  of: string
  flag?: NormFlag
}

export interface Person {
  id: string
  tier: Tier
  name: string
  source?: { file: string; source_id: string; row: number }
  created_at?: string
  updated_at?: string
  schema_version?: number
  /** Normalised values of typed properties (number, boolean, ISO date, canonical enum values). */
  norm: Record<string, unknown>
  /** Typed properties whose text could not be (fully) normalised. */
  norm_flags: Record<string, NormFlag>
  /** Present on GET /api/people/:id; every field's entries, oldest first. */
  history?: Record<string, FieldEntry[]>
  history_norm?: Record<string, NormEntry[]>
  [key: string]: unknown
}

export interface SearchHit {
  id: string
  tier: Tier
  score: number | null
  field_scores: Record<string, number>
  person: Person
}

export interface SearchResponse {
  total: number
  page: number
  limit: number
  results: SearchHit[]
  search_id: number | null
}

export type PropertyType = 'text' | 'keyword' | 'url' | 'contact' | 'number' | 'boolean' | 'date' | 'enum' | 'enum_list' | 'list'

/** One entry of the property catalog (GET /api/properties). Read-only in the UI. */
export interface Property {
  key: string
  label: string
  type: PropertyType
  /** Canonical values of enum / enum_list properties. */
  values: string[]
  /** What values the property holds (from source code, or given when an agent created it). */
  description: string | null
  embeddable: boolean
  normalisable: boolean
  filter_ops: FilterOp[]
  source: 'import' | 'agent' | 'spec'
  created_at: string | null
  /** People holding a current value, and how many of those could not be normalised. */
  count: number
  unmapped: number
  non_normalizable: number
}

export interface ImportColumn {
  header: string
  key: string
  role: 'field' | 'source_id'
  new?: boolean
}

export interface ImportPreview {
  rows: number
  columns: ImportColumn[]
}

/** What POST /api/people/import answers: the import runs as an assistant run of row batches. */
export interface ImportStarted {
  file: string
  tier: Tier
  rows: number
  skipped: number
  new_properties: string[]
  run_id: number
  items: number
}

// --- tool invocations (level 2): one tool on one record, non-blocking -------------------------------

export type EventKind = 'step' | 'agent' | 'provider' | 'write' | 'note' | 'error' | 'result' | 'item' | 'run'

/** One line of a tool's or a run's activity. */
export interface ToolEvent {
  seq: number
  ts: string
  kind: EventKind
  message: string
  data: Record<string, unknown>
}

export type InvocationStatus = 'running' | 'done' | 'failed' | 'stopped'

/** The handle a surface gets back from starting a tool: follow its events, stop it, fetch its result. */
export interface Invocation {
  id: string
  tool: string
  person_id: string
  status: InvocationStatus
  started_at: string
  finished_at: string | null
  credits: number
  events: number
  error: string | null
  events_url: string
  result?: unknown
}

/** The Firecrawl balance for the top bar. */
export interface Credits {
  remaining: number | null
  plan: number | null
  period_start: string | null
  period_end: string | null
  reserved: number
  error: string | null
}

export interface Page<T> {
  items: T[]
  has_more: boolean
}

export interface SearchRecord {
  id: number
  user_id: number
  username: string
  api_key_id: number | null
  source: 'ui' | 'api' | 'mcp'
  /** The executed request; `ask` is the question when the ask agent planned it. */
  params: SearchRequest & { ask?: string }
  result_count: number
  created_at: string
}

// --- levels: what a writer, tool or assistant did to a document ------------------------------

export type ChangeAction = 'set' | 'append' | 'removed' | 'normalized' | 'flagged' | 'property_created' | 'enum_value_added' | 'note'

/** One thing that happened to a person's document (api/ARCHITECTURE.md). */
export interface Change {
  key: string
  action: ChangeAction
  value: unknown
  previous: unknown
  flag: NormFlag | null
}

// --- tools (level 2): structured, one shot ----------------------------------------------------

export interface Tuning {
  k: number
  mode: 'avg' | 'max'
  threshold: number
}

export interface PlanResult {
  request: SearchRequest
  rationale: string
}

export interface StoreOptions {
  max_new: number
  near_duplicate: number
  /** Where facts beyond max_new go: filed under notes (default) or dropped and reported. */
  overflow?: 'notes' | 'ignore'
}

/** The identity repair inside a normalize result. */
export interface RepairResult {
  person_id: string | null
  changes: Change[]
  kind: string | null
  name_source: 'evidence' | 'provider' | 'unchanged' | null
  signals: string[]
  mechanical: string[]
  agent_called: boolean
  provider_called: boolean
  credits: number
  confidence: string | null
  note: string
  rejected: string[]
}

export interface NormalizeResult {
  person_id: string | null
  changes: Change[]
  agent_called: boolean
  unmapped: string[]
  repair: RepairResult | null
  /** Property → canonical value its text was merged into (the text became an alias). */
  merged: Record<string, string>
  /** Property → new canonical values the agent allocated. */
  allocated: Record<string, string[]>
  /** Property → values the agent proposed that the caps did not allow. */
  rejected: Record<string, string[]>
}

export interface StoreResult {
  person_id: string | null
  changes: Change[]
  confirmed: string[]
  ignored: string[]
  conflicts: string[]
  created_properties: string[]
  redirected: Record<string, string>
  to_notes: string[]
  /** Facts dropped because max_new was reached (overflow "ignore"). */
  dropped: string[]
  /** Property → the passage words its write rests on. */
  evidence: Record<string, string>
  normalization: NormalizeResult | null
}

// --- assistants (level 3): text for a human plus the changes ---------------------------------

export interface Citation {
  person_id: string
  field: string
  value: string
}

export interface AskResult {
  text: string
  changes: Change[]
  citations: Citation[]
  /** The search that was run; re-runnable as an ordinary search. */
  request: SearchRequest
  rationale: string
  total: number
  results: SearchHit[]
  search_id: number | null
}

export interface ActivityItem {
  id: number
  user_id: number | null
  username: string | null
  api_key_id: number | null
  action: string
  target: string | null
  meta: Record<string, unknown>
  ip: string | null
  created_at: string
}

export interface User {
  id: number
  username: string
  role: string
  is_active: boolean
  created_at: string
  last_seen?: string | null
}

export interface ApiKey {
  id: number
  user_id: number
  name: string
  prefix: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
  key?: string
}

export type AssistantScope = 'people' | 'batches' | 'single'

export interface AssistantSpec {
  key: string
  label: string
  description: string
  /** The full functionality spec, shown in the card's "How it works" toggle. */
  details: string
  scope: AssistantScope
  permission: string
  selector_field: string | null
  options_schema: { properties?: Record<string, { default?: unknown; description?: string; enum?: string[]; type?: string }> }
}

export type RunStatus = 'queued' | 'running' | 'pausing' | 'paused' | 'done' | 'failed' | 'cancelled'
export type RunItemStatus = 'queued' | 'running' | 'done' | 'failed' | 'skipped'

export interface AssistantRun {
  id: number
  workflow: string
  status: RunStatus
  params: { options?: Record<string, unknown>; selector?: string | null; tier?: Tier | null }
  user_id: number | null
  username?: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  total: number
  done: number
  failed: number
  credits_used: number
  summary: { by_status?: Record<string, number> }
  error: string | null
  worker: string | null
  heartbeat_at: string | null
  label: string | null
  pause_reason: string | null
}

/** One persisted line of a run's activity (a tool event during an item, or a run-level note). */
export interface RunEvent {
  id: number
  run_id: number
  item_id: number | null
  ts: string
  kind: EventKind
  message: string
  data: Record<string, unknown>
}

export interface AssistantRunItem {
  id: number
  run_id: number
  workflow: string
  /** A person, or null for a row batch or a one-shot item. */
  person_id: string | null
  payload: Record<string, unknown>
  label: string | null
  attempts: number
  status: RunItemStatus
  /** The assistant's result for this item: text for a human, the changes, and its own details. */
  result: {
    text?: string
    changes?: Change[]
    skipped?: { step: string; reason: string }[]
    not_found?: string[]
    linkedin?: string
    company?: string
    credits?: number
    [key: string]: unknown
  }
  error: string | null
  credits: number
  trace_id: string | null
  logfire_url: string | null
  started_at: string | null
  finished_at: string | null
}

export interface AssistantRunDetail extends AssistantRun {
  items: AssistantRunItem[]
}
