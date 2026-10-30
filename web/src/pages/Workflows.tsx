import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { ErrorNote, PageHeader } from '../components/Layout'
import { formatDate, formatValue } from '../lib'
import type { Person, RunItemStatus, RunStatus, WorkflowRun, WorkflowRunItem, WorkflowSpec } from '../types'
import { Pager } from './Searches'

const PAGE = 25
const ACTIVE: RunStatus[] = ['queued', 'running']

export default function WorkflowsPage() {
  const { can } = useAuth()
  const qc = useQueryClient()
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<number | null>(null)

  const specs = useQuery({ queryKey: ['workflows'], queryFn: api.workflows })
  const runs = useQuery({
    queryKey: ['workflow-runs', offset],
    queryFn: () => api.runs({ limit: PAGE, offset }),
    refetchInterval: (q) => (q.state.data?.items.some((r) => ACTIVE.includes(r.status)) ? 1500 : false),
  })
  const detail = useQuery({
    queryKey: ['workflow-run', selected],
    queryFn: () => api.run(selected!),
    enabled: selected !== null,
    refetchInterval: (q) => (q.state.data && ACTIVE.includes(q.state.data.status) ? 1500 : false),
  })
  const cancel = useMutation({
    mutationFn: (id: number) => api.cancelRun(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['workflow-runs'] })
      qc.invalidateQueries({ queryKey: ['workflow-run'] })
    },
  })

  return (
    <div className="space-y-6">
      <PageHeader title="Workflows" />
      <ErrorNote error={specs.error ?? runs.error ?? cancel.error} />
      <div className="grid gap-4 lg:grid-cols-2">
        {(specs.data ?? []).map((spec) => (
          <WorkflowCard
            key={spec.key}
            spec={spec}
            canRun={can('people:write')}
            onStarted={(run) => {
              setOffset(0)
              setSelected(run.id)
              qc.invalidateQueries({ queryKey: ['workflow-runs'] })
            }}
          />
        ))}
      </div>

      <section>
        <h2 className="mb-2 text-sm font-semibold text-slate-700">Runs</h2>
        <RunsTable
          runs={runs.data?.items ?? []}
          selected={selected}
          onSelect={setSelected}
          onCancel={can('people:write') ? (id) => cancel.mutate(id) : undefined}
        />
        <Pager offset={offset} size={PAGE} hasMore={runs.data?.has_more ?? false} onChange={setOffset} />
      </section>

      {selected !== null && (
        <section data-testid="run-detail">
          <h2 className="mb-2 text-sm font-semibold text-slate-700">Run #{selected}</h2>
          <ErrorNote error={detail.error} />
          {detail.data ? <RunDetail run={detail.data} items={detail.data.items} /> : <p className="text-sm text-slate-500">Loading…</p>}
        </section>
      )}
    </div>
  )
}

function WorkflowCard({ spec, canRun, onStarted }: { spec: WorkflowSpec; canRun: boolean; onStarted: (run: WorkflowRun) => void }) {
  const [target, setTarget] = useState<'one' | 'all'>('one')
  const [query, setQuery] = useState('')
  const [person, setPerson] = useState<Person | null>(null)
  const props = spec.options_schema.properties ?? {}
  const [options, setOptions] = useState<Record<string, unknown>>(() =>
    Object.fromEntries(Object.entries(props).map(([k, p]) => [k, p.default])),
  )

  const lookup = useQuery({
    queryKey: ['workflow-person-lookup', query],
    queryFn: () => api.search({ tier: null, filters: [{ field: 'name', op: 'contains', value: query }], sort: 'score', limit: 8, page: 1 }),
    enabled: target === 'one' && query.trim().length >= 2 && !person,
  })
  const count = useQuery({
    queryKey: ['workflow-selector-count', spec.selector_field],
    queryFn: () => api.search({ tier: null, filters: [{ field: spec.selector_field, op: 'exists', value: true }], sort: 'score', limit: 1, page: 1 }),
    enabled: target === 'all',
  })
  const start = useMutation({
    mutationFn: () =>
      api.startRun(spec.key, target === 'one' ? { person_ids: [person!.id], options } : { all_with_field: spec.selector_field, options }),
    onSuccess: onStarted,
  })

  return (
    <div className="card space-y-3 p-4" data-testid={`workflow-${spec.key}`}>
      <div>
        <h2 className="font-semibold">{spec.label}</h2>
        <p className="mt-1 text-sm text-slate-600">{spec.description}</p>
      </div>
      {canRun && (
        <form
          className="space-y-3"
          aria-label={`Run ${spec.label}`}
          onSubmit={(e) => {
            e.preventDefault()
            start.mutate()
          }}
        >
          <div className="flex flex-wrap gap-4 text-sm" role="radiogroup" aria-label="Run for">
            <label className="flex items-center gap-1.5">
              <input type="radio" name={`target-${spec.key}`} checked={target === 'one'} onChange={() => setTarget('one')} /> One person
            </label>
            <label className="flex items-center gap-1.5">
              <input type="radio" name={`target-${spec.key}`} checked={target === 'all'} onChange={() => setTarget('all')} /> Everyone with{' '}
              <code>{spec.selector_field}</code>
              {target === 'all' && count.data && <span className="text-slate-500">({count.data.total} people)</span>}
            </label>
          </div>
          {target === 'one' &&
            (person ? (
              <div className="flex items-center gap-2 text-sm">
                <span className="badge bg-brand-50 text-brand-700">{person.name}</span>
                <button type="button" className="btn-ghost" onClick={() => setPerson(null)}>
                  change
                </button>
              </div>
            ) : (
              <div className="relative">
                <input className="input" aria-label="Person name" placeholder="Type a name…" value={query} onChange={(e) => setQuery(e.target.value)} />
                {lookup.data && lookup.data.results.length > 0 && (
                  <ul className="absolute z-10 mt-1 w-full rounded-md border border-slate-200 bg-white shadow" role="listbox">
                    {lookup.data.results.map((h) => (
                      <li key={h.id}>
                        <button type="button" role="option" aria-selected={false} className="w-full px-3 py-1.5 text-left text-sm hover:bg-slate-50" onClick={() => setPerson(h.person)}>
                          {h.person.name} <span className="text-slate-400">{formatValue(h.person.company)}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          <div className="flex flex-wrap items-end gap-3">
            {Object.entries(props).map(([key, p]) => (
              <label key={key} className="space-y-1 text-sm" title={p.description}>
                <span className="label">{key.replace(/_/g, ' ')}</span>
                {p.enum ? (
                  <select className="input w-36" aria-label={key} value={String(options[key] ?? '')} onChange={(e) => setOptions({ ...options, [key]: e.target.value })}>
                    {p.enum.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                ) : p.type === 'boolean' ? (
                  <input type="checkbox" aria-label={key} className="block" checked={!!options[key]} onChange={(e) => setOptions({ ...options, [key]: e.target.checked })} />
                ) : (
                  <input
                    type="number"
                    className="input w-28"
                    aria-label={key}
                    value={String(options[key] ?? '')}
                    onChange={(e) => setOptions({ ...options, [key]: e.target.value === '' ? undefined : Number(e.target.value) })}
                  />
                )}
              </label>
            ))}
            <span className="flex-1" />
            <button className="btn-primary" disabled={start.isPending || (target === 'one' && !person)}>
              {start.isPending ? 'Starting…' : 'Run'}
            </button>
          </div>
          <ErrorNote error={start.error} />
        </form>
      )}
    </div>
  )
}

const STATUS_CLASS: Record<string, string> = {
  queued: 'bg-slate-100 text-slate-600',
  running: 'bg-amber-50 text-amber-700',
  done: 'bg-emerald-50 text-emerald-700',
  failed: 'bg-red-50 text-red-700',
  cancelled: 'bg-slate-100 text-slate-500',
  skipped: 'bg-slate-100 text-slate-500',
}

export function StatusBadge({ status }: { status: RunStatus | RunItemStatus }) {
  return <span className={`badge ${STATUS_CLASS[status] ?? ''}`}>{status}</span>
}

function duration(a: string | null, b: string | null): string {
  if (!a) return '—'
  const ms = (b ? new Date(b).getTime() : Date.now()) - new Date(a).getTime()
  return ms < 60_000 ? `${Math.round(ms / 1000)}s` : `${Math.round(ms / 60_000)}m`
}

/** Presentational: the runs list. */
export function RunsTable({
  runs,
  selected,
  onSelect,
  onCancel,
}: {
  runs: WorkflowRun[]
  selected: number | null
  onSelect: (id: number) => void
  onCancel?: (id: number) => void
}) {
  if (runs.length === 0) return <p className="text-sm text-slate-500">No runs yet.</p>
  return (
    <div className="card overflow-x-auto">
      <table className="data-table" data-testid="runs-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Workflow</th>
            <th>Status</th>
            <th>Progress</th>
            <th>Failed</th>
            <th>Credits</th>
            <th>By</th>
            <th>Started</th>
            <th>Took</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className={r.id === selected ? 'bg-brand-50/40' : ''}>
              <td>
                <button className="text-brand-700 hover:underline" onClick={() => onSelect(r.id)}>
                  {r.id}
                </button>
              </td>
              <td>{r.workflow}</td>
              <td>
                <StatusBadge status={r.status} />
                {r.summary?.stopped_reason && <span className="ml-1 text-xs text-slate-500">({r.summary.stopped_reason})</span>}
              </td>
              <td className="tabular-nums" title="finished (done, skipped or failed) / total">
                {r.done + r.failed}/{r.total}
              </td>
              <td className="tabular-nums">{r.failed}</td>
              <td className="tabular-nums">{r.credits_used}</td>
              <td>{r.username ?? '—'}</td>
              <td className="whitespace-nowrap text-slate-500">{formatDate(r.started_at ?? r.created_at)}</td>
              <td className="tabular-nums text-slate-500">{duration(r.started_at, r.finished_at)}</td>
              <td>
                {onCancel && ACTIVE.includes(r.status) && (
                  <button className="btn-ghost" onClick={() => onCancel(r.id)}>
                    Cancel
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Presentational: one run with its per-person items. */
export function RunDetail({ run, items }: { run: WorkflowRun; items: WorkflowRunItem[] }) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-600">
        <StatusBadge status={run.status} /> {run.done + run.failed}/{run.total} people · {run.failed} failed · {run.credits_used} credits
        {run.params.options && (
          <>
            {' · '}
            {Object.entries(run.params.options)
              .map(([k, v]) => `${k}=${formatValue(v)}`)
              .join(', ')}
          </>
        )}
        {run.error && <span className="ml-2 text-red-700">{run.error}</span>}
      </p>
      <div className="card overflow-x-auto">
        <table className="data-table" data-testid="run-items">
          <thead>
            <tr>
              <th>Person</th>
              <th>Status</th>
              <th>Written</th>
              <th>Skipped</th>
              <th>Credits</th>
              <th>Trace</th>
            </tr>
          </thead>
          <tbody>
            {items.map((it) => (
              <tr key={it.id}>
                <td>
                  <Link to={`/people/${it.person_id}`} className="font-mono text-xs text-brand-700 hover:underline">
                    {it.person_id}
                  </Link>
                </td>
                <td>
                  <StatusBadge status={it.status} />
                  {it.error && <div className="mt-1 max-w-sm text-xs text-red-700">{it.error}</div>}
                </td>
                <td>
                  <div className="flex max-w-md flex-wrap gap-1">
                    {Object.entries(it.result.written ?? {}).map(([k, v]) => (
                      <span key={k} className="badge bg-emerald-50 text-emerald-700" title={formatValue(v)}>
                        {k}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="max-w-sm text-xs text-slate-500">
                  {(it.result.skipped ?? []).map((s, i) => (
                    <div key={i}>
                      <span className="font-medium text-slate-600">{s.step}</span>: {s.reason}
                    </div>
                  ))}
                </td>
                <td className="tabular-nums">{it.credits}</td>
                <td>
                  {it.logfire_url ? (
                    <a href={it.logfire_url} target="_blank" rel="noreferrer noopener" className="text-brand-700 hover:underline">
                      Logfire
                    </a>
                  ) : (
                    <span className="font-mono text-xs text-slate-400">{it.trace_id?.slice(0, 8) ?? '—'}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
