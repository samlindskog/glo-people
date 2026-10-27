import type { FieldFilter, SearchRequest } from './types'

export const DEFAULT_SEARCH: SearchRequest = {
  tier: null,
  filters: [],
  semantic: null,
  sort: 'score',
  limit: 25,
  page: 1,
}

/** Search state lives in the URL (?s=…) so searches can be bookmarked and re-run. */
export function encodeSearch(req: SearchRequest): string {
  const json = JSON.stringify(req)
  return btoa(String.fromCharCode(...new TextEncoder().encode(json)))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
}

export function decodeSearch(s: string | null): SearchRequest {
  if (!s) return DEFAULT_SEARCH
  try {
    const b64 = s.replace(/-/g, '+').replace(/_/g, '/')
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
    const parsed = JSON.parse(new TextDecoder().decode(bytes))
    return { ...DEFAULT_SEARCH, ...parsed, filters: parsed.filters ?? [] }
  } catch {
    return DEFAULT_SEARCH
  }
}

export function formatValue(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (Array.isArray(v)) return v.map(formatValue).join(', ')
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function describeFilter(f: FieldFilter): string {
  if (f.op === 'exists') return `${f.field} ${f.value === false ? 'is empty' : 'exists'}`
  const v = Array.isArray(f.value) ? f.value.join(' | ') : formatValue(f.value)
  const op = { eq: '=', ne: '≠', contains: 'contains', starts_with: 'starts with', regex: '~', in: 'in', gt: '>', gte: '≥', lt: '<', lte: '≤' }[f.op]
  return `${f.field} ${op} ${v}`
}

export function describeSearch(req: Partial<SearchRequest>): string {
  const parts: string[] = []
  if (req.tier) parts.push(`tier = ${req.tier}`)
  for (const f of req.filters ?? []) parts.push(describeFilter(f))
  if (req.semantic) {
    const s = req.semantic
    parts.push(`“${s.query}” in ${s.fields.join(', ')} (${s.mode ?? 'avg'} ≥ ${s.threshold ?? 0.2}${s.require_all_fields ? ', all fields' : ''})`)
  }
  return parts.join(' · ') || 'Everyone'
}

export const FILTER_OPS: { op: FieldFilter['op']; label: string }[] = [
  { op: 'contains', label: 'contains' },
  { op: 'eq', label: 'equals' },
  { op: 'ne', label: 'not equal' },
  { op: 'starts_with', label: 'starts with' },
  { op: 'regex', label: 'matches regex' },
  { op: 'in', label: 'is one of' },
  { op: 'exists', label: 'has value' },
  { op: 'gt', label: '>' },
  { op: 'gte', label: '≥' },
  { op: 'lt', label: '<' },
  { op: 'lte', label: '≤' },
]
