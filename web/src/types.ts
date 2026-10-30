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

export interface SemanticQuery {
  query: string
  fields: string[]
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

export type SourceKind = 'ui' | 'api' | 'mcp' | 'agent' | 'import' | 'legacy'

/** One history entry of a field: the value written, when, and by whom. */
export interface FieldEntry {
  v: FieldValue | null
  ts?: string
  src?: { kind: SourceKind; by?: string; ref?: string }
}

export interface Person {
  id: string
  tier: Tier
  name: string
  source?: { file: string; source_id: string; row: number }
  created_at?: string
  updated_at?: string
  schema_version?: number
  /** Present on GET /api/people/:id; every field's entries, oldest first. */
  history?: Record<string, FieldEntry[]>
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
  embeddable: boolean
  normalisable: boolean
  source: 'import' | 'agent' | 'spec'
  created_at: string | null
  count: number
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

export interface ImportResult {
  file: string
  tier: Tier
  rows: number
  inserted: number
  updated: number
  skipped: number
  new_properties: string[]
  job_id: number
}

export interface Job {
  id: number
  name: string
  status: 'running' | 'done' | 'failed'
  progress: number
  result: unknown
  error: string | null
  started_at: string
  finished_at: string | null
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
  params: SearchRequest
  result_count: number
  created_at: string
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

export interface WorkflowSpec {
  key: string
  label: string
  description: string
  selector_field: string
  options_schema: { properties?: Record<string, { default?: unknown; description?: string; enum?: string[]; type?: string }> }
}

export type RunStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
export type RunItemStatus = 'queued' | 'running' | 'done' | 'failed' | 'skipped'

export interface WorkflowRun {
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
  summary: { stopped_reason?: string; by_status?: Record<string, number> }
  error: string | null
}

export interface WorkflowRunItem {
  id: number
  run_id: number
  workflow: string
  person_id: string
  status: RunItemStatus
  result: {
    written?: Record<string, unknown>
    skipped?: { step: string; reason: string }[]
    not_found?: string[]
    linkedin?: string
    company?: string
    credits?: number
  }
  error: string | null
  credits: number
  trace_id: string | null
  logfire_url: string | null
  started_at: string | null
  finished_at: string | null
}

export interface WorkflowRunDetail extends WorkflowRun {
  items: WorkflowRunItem[]
}
