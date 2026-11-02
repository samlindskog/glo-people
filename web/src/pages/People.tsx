import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import Ask from '../components/Ask'
import ImportDialog from '../components/ImportDialog'
import { ErrorNote, PageHeader } from '../components/Layout'
import SearchForm from '../components/SearchForm'
import { decodeSearch, describeSearch, encodeSearch, formatValue } from '../lib'
import type { SearchRequest } from '../types'
import { TIERS } from '../types'

const COLUMNS_KEY = 'glo.people.columns'
const DEFAULT_COLUMNS = ['company', 'role', 'location']

function loadColumns(): string[] {
  try {
    return JSON.parse(localStorage.getItem(COLUMNS_KEY) ?? 'null') ?? DEFAULT_COLUMNS
  } catch {
    return DEFAULT_COLUMNS
  }
}

export default function PeoplePage() {
  const { can } = useAuth()
  const [params, setParams] = useSearchParams()
  const encoded = params.get('s')
  const req = decodeSearch(encoded)
  const [columns, setColumns] = useState<string[]>(loadColumns)
  const [importing, setImporting] = useState(false)

  useEffect(() => {
    try {
      localStorage.setItem(COLUMNS_KEY, JSON.stringify(columns))
    } catch {
      /* storage unavailable */
    }
  }, [columns])

  const propsQ = useQuery({ queryKey: ['properties'], queryFn: api.properties })
  const results = useQuery({
    queryKey: ['search', encoded ?? ''],
    queryFn: () => api.search(req),
    staleTime: Infinity,
  })

  const run = (next: SearchRequest) => setParams({ s: encodeSearch(next) })
  const properties = propsQ.data ?? []
  const visibleCols = columns.filter((c) => properties.some((p) => p.key === c))
  const data = results.data
  const semantic = !!req.semantic
  const pages = data ? Math.max(1, Math.ceil(data.total / data.limit)) : 1

  return (
    <div>
      <PageHeader title="People">
        {can('people:write') && (
          <>
            <button className="btn-secondary" onClick={() => setImporting((v) => !v)} aria-expanded={importing}>
              Import CSV
            </button>
            <Link to="/people/new" className="btn-primary">
              Add person
            </Link>
          </>
        )}
      </PageHeader>
      {importing && (
        <div className="card mb-4 p-4">
          <ImportDialog />
        </div>
      )}

      <div className="mb-4">
        <Ask tier={req.tier ?? null} />
      </div>
      {propsQ.isSuccess && <SearchForm key={encoded ?? ''} initial={req} properties={properties} onSearch={run} />}

      <div className="mt-5 flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-slate-600" data-testid="result-summary">
          {results.isFetching ? 'Searching…' : data ? `${data.total.toLocaleString()} ${data.total === 1 ? 'person' : 'people'}` : ''}
          <span className="ml-2 text-slate-400">{describeSearch(req)}</span>
        </p>
        <details className="relative text-sm">
          <summary className="btn-secondary cursor-pointer list-none">Columns</summary>
          <div className="card absolute right-0 z-10 mt-1 max-h-80 w-64 overflow-auto p-2">
            {properties.map((p) => (
              <label key={p.key} className="flex items-center gap-2 px-1 py-0.5">
                <input
                  type="checkbox"
                  checked={columns.includes(p.key)}
                  onChange={(e) => setColumns(e.target.checked ? [...columns, p.key] : columns.filter((c) => c !== p.key))}
                />
                {p.label}
              </label>
            ))}
          </div>
        </details>
      </div>

      <ErrorNote error={results.error} />

      <div className="card mt-3 overflow-x-auto">
        <table className="data-table" data-testid="results">
          <thead>
            <tr>
              <th>Name</th>
              <th>Tier</th>
              {semantic && <th>Score</th>}
              {semantic && <th>Field scores</th>}
              {visibleCols.map((c) => (
                <th key={c}>{properties.find((p) => p.key === c)?.label ?? c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data?.results.map((hit) => (
              <tr key={hit.id} className="hover:bg-slate-50">
                <td className="font-medium">
                  <Link className="text-brand-700 hover:underline" to={`/people/${hit.id}`}>
                    {hit.person.name}
                  </Link>
                  {typeof hit.person.record_kind === 'string' && hit.person.record_kind !== 'Person' && (
                    <span className={`badge ml-2 ${hit.person.record_kind === 'Unidentifiable' ? 'bg-red-50 text-red-700' : 'bg-slate-100 text-slate-600'}`} title="record kind">
                      {hit.person.record_kind}
                    </span>
                  )}
                </td>
                <td>
                  <TierBadge tier={hit.tier} />
                </td>
                {semantic && <td className="tabular-nums">{hit.score?.toFixed(3)}</td>}
                {semantic && (
                  <td className="text-xs text-slate-500">
                    {Object.entries(hit.field_scores).map(([k, v]) => (
                      <span key={k} className="mr-2 whitespace-nowrap">
                        {k} <span className="tabular-nums text-slate-700">{v.toFixed(3)}</span>
                      </span>
                    ))}
                  </td>
                )}
                {visibleCols.map((c) => (
                  <td key={c} className="max-w-xs truncate" title={formatValue(hit.person[c])}>
                    {formatValue(hit.person[c])}
                  </td>
                ))}
              </tr>
            ))}
            {data && data.results.length === 0 && (
              <tr>
                <td colSpan={99} className="py-8 text-center text-slate-500">
                  No one matches.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {data && pages > 1 && (
        <div className="mt-3 flex items-center gap-2 text-sm">
          <button className="btn-secondary" disabled={req.page <= 1} onClick={() => run({ ...req, page: req.page - 1 })}>
            Previous
          </button>
          <span>
            Page {req.page} of {pages}
          </span>
          <button className="btn-secondary" disabled={req.page >= pages} onClick={() => run({ ...req, page: req.page + 1 })}>
            Next
          </button>
        </div>
      )}
    </div>
  )
}

const TIER_BADGE: Record<string, string> = {
  member: 'bg-emerald-50 text-emerald-700',
  plus_one: 'bg-amber-50 text-amber-700',
  plus_two: 'bg-sky-50 text-sky-700',
}

export function TierBadge({ tier }: { tier: string }) {
  const label = TIERS.find((t) => t.value === tier)?.label ?? tier
  return <span className={`badge ${TIER_BADGE[tier] ?? 'bg-slate-100 text-slate-600'}`}>{label}</span>
}
