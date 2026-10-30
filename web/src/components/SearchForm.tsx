import { useState, type FormEvent } from 'react'
import { FILTER_OPS } from '../lib'
import type { FieldFilter, Property, SearchRequest, SemanticQuery } from '../types'

const DEFAULT_SEMANTIC: SemanticQuery = {
  query: '',
  fields: [],
  mode: 'avg',
  threshold: 0.4,
  require_all_fields: false,
}

interface Props {
  initial: SearchRequest
  properties: Property[]
  onSearch: (req: SearchRequest) => void
}

/** Literal per-property filter builder + semantic panel. Emits a SearchRequest. */
export default function SearchForm({ initial, properties, onSearch }: Props) {
  const [tier, setTier] = useState(initial.tier ?? '')
  const [filters, setFilters] = useState<FieldFilter[]>(initial.filters)
  const [semOn, setSemOn] = useState(!!initial.semantic)
  const [sem, setSem] = useState<SemanticQuery>({ ...DEFAULT_SEMANTIC, ...initial.semantic })
  const [sort, setSort] = useState(initial.sort)
  const [limit, setLimit] = useState(initial.limit)

  const embeddable = properties.filter((p) => p.embeddable)
  const fieldKeys = properties.map((p) => p.key)

  function submit(e?: FormEvent) {
    e?.preventDefault()
    const perField = sem.queries?.length ? sem.queries : undefined
    const single = sem.query.trim() && sem.fields.length
    const semantic = semOn && (single || perField) ? { ...sem, query: sem.query.trim(), queries: perField } : null
    onSearch({
      tier: tier ? (tier as SearchRequest['tier']) : null,
      filters: filters.filter((f) => f.field).map(normalizeFilter),
      semantic,
      sort: !semantic && sort === 'score' ? 'score' : sort,
      limit,
      page: 1,
    })
  }

  const updateFilter = (i: number, patch: Partial<FieldFilter>) =>
    setFilters(filters.map((f, j) => (j === i ? { ...f, ...patch } : f)))

  return (
    <form onSubmit={submit} className="card space-y-4 p-4" aria-label="Search people">
      <div className="flex flex-wrap items-end gap-3">
        <label className="space-y-1">
          <span className="label">Tier</span>
          <select className="input w-36" value={tier} onChange={(e) => setTier(e.target.value)} aria-label="Tier">
            <option value="">All tiers</option>
            <option value="member">Members</option>
            <option value="plus_one">Plus ones</option>
          </select>
        </label>
        <label className="space-y-1">
          <span className="label">Sort</span>
          <select
            className="input w-48"
            aria-label="Sort"
            value={sort === 'score' ? 'score' : `${sort.field}:${sort.dir}`}
            onChange={(e) => {
              const v = e.target.value
              if (v === 'score') setSort('score')
              else {
                const [field, dir] = v.split(':')
                setSort({ field, dir: dir as 'asc' | 'desc' })
              }
            }}
          >
            <option value="score">Relevance (or name)</option>
            {fieldKeys.flatMap((k) => [
              <option key={`${k}:asc`} value={`${k}:asc`}>
                {k} ↑
              </option>,
              <option key={`${k}:desc`} value={`${k}:desc`}>
                {k} ↓
              </option>,
            ])}
          </select>
        </label>
        <label className="space-y-1">
          <span className="label">Per page</span>
          <select className="input w-20" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {[10, 25, 50, 100, 200].map((n) => (
              <option key={n}>{n}</option>
            ))}
          </select>
        </label>
      </div>

      <fieldset className="space-y-2">
        <legend className="label mb-1">Property filters (all must match)</legend>
        {filters.map((f, i) => (
          <div key={i} className="flex flex-wrap items-center gap-2" data-testid="filter-row">
            <select
              className="input w-52"
              aria-label="Filter field"
              value={f.field}
              onChange={(e) => updateFilter(i, { field: e.target.value })}
            >
              <option value="">Property…</option>
              {properties.map((p) => (
                <option key={p.key} value={p.key}>
                  {p.label} ({p.key})
                </option>
              ))}
            </select>
            <select
              className="input w-40"
              aria-label="Filter operator"
              value={f.op}
              onChange={(e) => updateFilter(i, { op: e.target.value as FieldFilter['op'], value: e.target.value === 'exists' ? true : f.value })}
            >
              {FILTER_OPS.map((o) => (
                <option key={o.op} value={o.op}>
                  {o.label}
                </option>
              ))}
            </select>
            {f.op === 'exists' ? (
              <select
                className="input w-32"
                aria-label="Filter value"
                value={f.value === false ? 'no' : 'yes'}
                onChange={(e) => updateFilter(i, { value: e.target.value === 'yes' })}
              >
                <option value="yes">yes</option>
                <option value="no">no (empty)</option>
              </select>
            ) : (
              <input
                className="input w-64"
                aria-label="Filter value"
                placeholder={f.op === 'in' ? 'a, b, c' : 'value'}
                value={Array.isArray(f.value) ? f.value.join(', ') : ((f.value as string) ?? '')}
                onChange={(e) => updateFilter(i, { value: e.target.value })}
              />
            )}
            <button type="button" className="btn-ghost" aria-label="Remove filter" onClick={() => setFilters(filters.filter((_, j) => j !== i))}>
              ✕
            </button>
          </div>
        ))}
        <button type="button" className="btn-secondary" onClick={() => setFilters([...filters, { field: '', op: 'contains', value: '' }])}>
          + Add filter
        </button>
      </fieldset>

      <fieldset className="space-y-3 rounded-md border border-slate-200 p-3">
        <legend className="px-1">
          <label className="flex items-center gap-2 text-sm font-medium">
            <input type="checkbox" checked={semOn} onChange={(e) => setSemOn(e.target.checked)} />
            Semantic search
          </label>
        </legend>
        {semOn && (
          <>
            {sem.queries && sem.queries.length > 0 && (
              <ul className="space-y-0.5 text-sm text-slate-600" aria-label="Per-property passages">
                {sem.queries.map((q) => (
                  <li key={q.field}>
                    <span className="font-mono text-xs">{q.field}</span>: “{q.query}”
                  </li>
                ))}
              </ul>
            )}
            <label className="block space-y-1">
              <span className="label">Query</span>
              <input
                className="input"
                aria-label="Semantic query"
                placeholder="e.g. early-stage fintech founder in West Africa"
                value={sem.query}
                onChange={(e) => setSem({ ...sem, query: e.target.value })}
              />
            </label>
            <div className="space-y-1">
              <span className="label">Compare against properties</span>
              {embeddable.length === 0 ? (
                <p className="text-sm text-slate-500">No embeddable properties yet.</p>
              ) : (
                <div className="flex flex-wrap gap-1.5" role="group" aria-label="Semantic fields">
                  {embeddable.map((f) => {
                    const on = sem.fields.includes(f.key)
                    return (
                      <button
                        type="button"
                        key={f.key}
                        aria-pressed={on}
                        onClick={() => setSem({ ...sem, fields: on ? sem.fields.filter((k) => k !== f.key) : [...sem.fields, f.key] })}
                        className={`badge cursor-pointer border ${on ? 'border-brand-500 bg-brand-50 text-brand-700' : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50'}`}
                      >
                        {f.label}
                        <span className="ml-1 text-slate-400">{f.count}</span>
                      </button>
                    )
                  })}
                </div>
              )}
            </div>
            <div className="flex flex-wrap items-end gap-5">
              <div className="space-y-1">
                <span className="label">Combine fields by</span>
                <div className="flex overflow-hidden rounded-md border border-slate-300 text-sm" role="radiogroup" aria-label="Mode">
                  {(['avg', 'max'] as const).map((m) => (
                    <button
                      type="button"
                      key={m}
                      role="radio"
                      aria-checked={sem.mode === m}
                      onClick={() => setSem({ ...sem, mode: m })}
                      className={`px-3 py-1.5 ${sem.mode === m ? 'bg-brand-600 text-white' : 'bg-white text-slate-700'}`}
                    >
                      {m === 'avg' ? 'Average' : 'Best field'}
                    </button>
                  ))}
                </div>
              </div>
              <label className="space-y-1">
                <span className="label" title="A field's score is its best-matching history entry">
                  Threshold ≥ {sem.threshold.toFixed(2)}
                </span>
                <div className="flex items-center gap-2">
                  <input
                    type="range"
                    min={0}
                    max={1}
                    step={0.01}
                    aria-label="Threshold slider"
                    value={sem.threshold}
                    onChange={(e) => setSem({ ...sem, threshold: Number(e.target.value) })}
                  />
                  <input
                    type="number"
                    className="input w-20"
                    min={-1}
                    max={1}
                    step={0.01}
                    aria-label="Threshold"
                    value={sem.threshold}
                    onChange={(e) => setSem({ ...sem, threshold: Number(e.target.value) })}
                  />
                </div>
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={sem.require_all_fields}
                  onChange={(e) => setSem({ ...sem, require_all_fields: e.target.checked })}
                />
                Require every selected property
              </label>
            </div>
          </>
        )}
      </fieldset>

      <div className="flex gap-2">
        <button className="btn-primary">Search</button>
        <button
          type="button"
          className="btn-ghost"
          onClick={() => {
            setTier('')
            setFilters([])
            setSemOn(false)
            setSem(DEFAULT_SEMANTIC)
            setSort('score')
          }}
        >
          Reset
        </button>
      </div>
    </form>
  )
}

function normalizeFilter(f: FieldFilter): FieldFilter {
  if (f.op === 'in' && typeof f.value === 'string') {
    return { ...f, value: f.value.split(',').map((s) => s.trim()).filter(Boolean) }
  }
  if (['gt', 'gte', 'lt', 'lte'].includes(f.op) && typeof f.value === 'string' && f.value.trim() !== '' && !Number.isNaN(Number(f.value))) {
    return { ...f, value: Number(f.value) }
  }
  return f
}
