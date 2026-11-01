import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import ActivityFeed from '../components/ActivityFeed'
import ImportDialog from '../components/ImportDialog'
import { ErrorNote, PageHeader } from '../components/Layout'
import Prose from '../components/Prose'
import { useRunEvents } from '../hooks/useEvents'
import { formatDate, formatValue } from '../lib'
import type { AssistantRun, AssistantRunItem, AssistantSpec, Change, Permission, Person, RunItemStatus, RunStatus } from '../types'
import { Pager } from './Searches'

const PAGE = 25
const ACTIVE: RunStatus[] = ['queued', 'running', 'pausing', 'paused']
const LIVE: RunStatus[] = ['queued', 'running', 'pausing']

/** One page for every assistant: start one, watch the running ones (live activity, credits,
 * pause / resume / stop), and browse the history. `/assistants/:runId` opens one run. */
export default function AssistantsPage() {
  const { can } = useAuth()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { runId } = useParams()
  const selected = runId ? Number(runId) : null
  const [offset, setOffset] = useState(0)

  const specs = useQuery({ queryKey: ['assistants'], queryFn: api.assistants })
  const active = useQuery({
    queryKey: ['assistant-runs', 'active'],
    queryFn: () => api.runs({ status: 'active', limit: 50 }),
    refetchInterval: (q) => (q.state.data?.items.some((r) => LIVE.includes(r.status)) ? 3000 : 15_000),
  })
  const history = useQuery({
    queryKey: ['assistant-runs', offset],
    queryFn: () => api.runs({ limit: PAGE, offset }),
    refetchInterval: (q) => (q.state.data?.items.some((r) => LIVE.includes(r.status)) ? 5000 : false),
  })
  const selectedRun = useQuery({
    queryKey: ['assistant-run', selected],
    queryFn: () => api.run(selected!),
    enabled: selected !== null && !active.data?.items.some((r) => r.id === selected),
  })
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['assistant-runs'] })
    qc.invalidateQueries({ queryKey: ['assistant-run'] })
    qc.invalidateQueries({ queryKey: ['credits'] })
  }
  const pause = useMutation({ mutationFn: (id: number) => api.pauseRun(id), onSuccess: refresh })
  const resume = useMutation({ mutationFn: ({ id, cap }: { id: number; cap?: number }) => api.resumeRun(id, cap ? { max_credits: cap } : {}), onSuccess: refresh })
  const stop = useMutation({ mutationFn: (id: number) => api.stopRun(id), onSuccess: refresh })

  const running = active.data?.items ?? []
  const shown: AssistantRun[] = [...running]
  if (selected !== null && selectedRun.data && !shown.some((r) => r.id === selected)) shown.unshift(selectedRun.data)
  const controls = can('people:write')

  return (
    <div className="space-y-6">
      <PageHeader title="Assistants" />
      <ErrorNote error={specs.error ?? active.error ?? history.error ?? pause.error ?? resume.error ?? stop.error} />

      <details className="card" open={selected === null} data-testid="start-panel">
        <summary className="cursor-pointer select-none px-4 py-2.5 text-sm font-semibold text-slate-700">Start an assistant</summary>
        <div className="grid gap-4 border-t border-slate-100 p-4 lg:grid-cols-2">
          {(specs.data ?? []).map((spec) => (
            <AssistantCard key={spec.key} spec={spec} canRun={can(spec.permission as Permission)} onStarted={(id) => navigate(`/assistants/${id}`)} />
          ))}
        </div>
      </details>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-slate-700">Running</h2>
        {shown.length === 0 && <p className="text-sm text-slate-500">Nothing is running.</p>}
        {shown.map((r) => (
          <RunCard
            key={r.id}
            run={r}
            expanded={r.id === selected}
            onToggle={() => navigate(r.id === selected ? '/assistants' : `/assistants/${r.id}`)}
            onPause={controls ? () => pause.mutate(r.id) : undefined}
            onResume={controls ? (cap) => resume.mutate({ id: r.id, cap }) : undefined}
            onStop={controls ? () => stop.mutate(r.id) : undefined}
          >
            {r.id === selected && <RunDetail run={r} onFinished={refresh} />}
          </RunCard>
        ))}
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold text-slate-700">History</h2>
        <RunsTable runs={history.data?.items ?? []} selected={selected} onSelect={(id) => navigate(`/assistants/${id}`)} />
        <Pager offset={offset} size={PAGE} hasMore={history.data?.has_more ?? false} onChange={setOffset} />
      </section>
    </div>
  )
}

// --- starting ---------------------------------------------------------------------------------------

const HIDDEN_OPTIONS = new Set(['question', 'tier', 'filters', 'fields'])

function AssistantCard({ spec, canRun, onStarted }: { spec: AssistantSpec; canRun: boolean; onStarted: (runId: number) => void }) {
  const [target, setTarget] = useState<'one' | 'all'>(spec.scope === 'people' ? 'one' : 'all')
  const [query, setQuery] = useState('')
  const [person, setPerson] = useState<Person | null>(null)
  const [question, setQuestion] = useState('')
  const props = Object.fromEntries(Object.entries(spec.options_schema.properties ?? {}).filter(([k, p]) => !HIDDEN_OPTIONS.has(k) && (p.enum || p.type === 'boolean' || p.type === 'number' || p.type === 'integer')))
  const [options, setOptions] = useState<Record<string, unknown>>(() => Object.fromEntries(Object.entries(props).map(([k, p]) => [k, p.default])))

  const lookup = useQuery({
    queryKey: ['assistant-person-lookup', query],
    queryFn: () => api.search({ tier: null, filters: [{ field: 'name', op: 'contains', value: query }], sort: 'score', limit: 8, page: 1 }),
    enabled: spec.scope === 'people' && target === 'one' && query.trim().length >= 2 && !person,
  })
  const count = useQuery({
    queryKey: ['assistant-selector-count', spec.selector_field],
    queryFn: () => api.search({ tier: null, filters: [{ field: spec.selector_field!, op: 'exists', value: true }], sort: 'score', limit: 1, page: 1 }),
    enabled: spec.scope === 'people' && target === 'all' && !!spec.selector_field,
  })
  const start = useMutation({
    mutationFn: () => {
      if (spec.scope === 'single') return api.startRun(spec.key, { options: { ...options, question: question.trim() } })
      if (spec.scope === 'batches') return api.startRun(spec.key, { options })
      return api.startRun(spec.key, target === 'one' ? { person_ids: [person!.id], options } : { all_with_field: spec.selector_field!, options })
    },
    onSuccess: (run) => onStarted(run.id),
  })
  const ready = spec.scope === 'single' ? question.trim().length > 0 : spec.scope === 'people' ? target === 'all' || !!person : true

  return (
    <div className="card space-y-3 p-4" data-testid={`assistant-${spec.key}`}>
      <div>
        <h3 className="font-semibold">{spec.label}</h3>
        <p className="mt-1 text-sm text-slate-600">{spec.description}</p>
        {spec.details && (
          <details className="mt-2" data-testid={`how-${spec.key}`}>
            <summary className="cursor-pointer select-none text-sm text-brand-700">How it works</summary>
            <div className="mt-2 rounded-md border border-slate-100 bg-slate-50 p-3">
              <Prose text={spec.details} />
            </div>
          </details>
        )}
      </div>
      {canRun && spec.key === 'import' && <ImportDialog onStarted={(r) => onStarted(r.run_id)} />}
      {canRun && spec.key !== 'import' && (
        <form
          className="space-y-3"
          aria-label={`Run ${spec.label}`}
          onSubmit={(e) => {
            e.preventDefault()
            start.mutate()
          }}
        >
          {spec.scope === 'single' && (
            <label className="block space-y-1">
              <span className="label">Question</span>
              <input className="input" aria-label={`${spec.label} question`} value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="e.g. fintech founders in Lagos with MRR over 10k" />
            </label>
          )}
          {spec.scope === 'people' && (
            <div className="flex flex-wrap gap-4 text-sm" role="radiogroup" aria-label="Run for">
              <label className="flex items-center gap-1.5">
                <input type="radio" name={`target-${spec.key}`} checked={target === 'one'} onChange={() => setTarget('one')} /> One person
              </label>
              <label className="flex items-center gap-1.5">
                <input type="radio" name={`target-${spec.key}`} checked={target === 'all'} onChange={() => setTarget('all')} /> Everyone with <code>{spec.selector_field}</code>
                {target === 'all' && count.data && <span className="text-slate-500">({count.data.total} people)</span>}
              </label>
            </div>
          )}
          {spec.scope === 'batches' && <p className="text-sm text-slate-600">Runs over everyone, in batches.</p>}
          {spec.scope === 'people' &&
            target === 'one' &&
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
                    step="any"
                    value={String(options[key] ?? '')}
                    onChange={(e) => setOptions({ ...options, [key]: e.target.value === '' ? undefined : Number(e.target.value) })}
                  />
                )}
              </label>
            ))}
            <span className="flex-1" />
            <button className="btn-primary" disabled={start.isPending || !ready}>
              {start.isPending ? 'Starting…' : 'Run'}
            </button>
          </div>
          <ErrorNote error={start.error} />
        </form>
      )}
    </div>
  )
}

// --- running ----------------------------------------------------------------------------------------

const STATUS_CLASS: Record<string, string> = {
  queued: 'bg-slate-100 text-slate-600',
  running: 'bg-amber-50 text-amber-700',
  pausing: 'bg-amber-50 text-amber-700',
  paused: 'bg-sky-50 text-sky-700',
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

export function runTitle(run: AssistantRun): string {
  return run.label ? `${run.workflow}: ${run.label}` : run.workflow
}

/** One run: progress, credits, the controls its status allows, and (expanded) its detail. */
export function RunCard({
  run,
  expanded,
  onToggle,
  onPause,
  onResume,
  onStop,
  children,
}: {
  run: AssistantRun
  expanded: boolean
  onToggle: () => void
  onPause?: () => void
  onResume?: (cap?: number) => void
  onStop?: () => void
  children?: ReactNode
}) {
  const cap = Number(run.params.options?.max_credits ?? 0) || null
  const [newCap, setNewCap] = useState<string>(cap ? String(cap * 2) : '')
  const finished = run.done + run.failed
  const pct = run.total ? Math.round((finished / run.total) * 100) : 0
  return (
    <div className="card p-4" data-testid={`run-${run.id}`}>
      <div className="flex flex-wrap items-center gap-3">
        <button className="text-left font-semibold text-brand-700 hover:underline" onClick={onToggle} aria-expanded={expanded}>
          #{run.id} {runTitle(run)}
        </button>
        <StatusBadge status={run.status} />
        {run.pause_reason && <span className="text-xs text-slate-500">({run.pause_reason})</span>}
        <span className="text-xs text-slate-500">{run.username ?? '—'} · {formatDate(run.started_at ?? run.created_at)} · {duration(run.started_at, run.finished_at)}</span>
        <span className="flex-1" />
        {onPause && run.status === 'running' && (
          <button className="btn-secondary" onClick={onPause}>
            Pause
          </button>
        )}
        {onResume && run.status === 'paused' && (
          <span className="flex items-center gap-1">
            {cap !== null && <input type="number" className="input w-28" aria-label="New credit cap" title="Raise the run's credit cap before resuming" value={newCap} onChange={(e) => setNewCap(e.target.value)} />}
            <button className="btn-primary" onClick={() => onResume(newCap ? Number(newCap) : undefined)}>
              Resume
            </button>
          </span>
        )}
        {onStop && ACTIVE.includes(run.status) && (
          <button className="btn-danger" onClick={onStop}>
            Stop
          </button>
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-4 text-sm text-slate-600">
        <div className="h-2 w-48 overflow-hidden rounded bg-slate-100" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <div className={`h-full ${run.failed ? 'bg-amber-400' : 'bg-brand-500'}`} style={{ width: `${pct}%` }} />
        </div>
        <span className="tabular-nums">
          {finished}/{run.total} items
        </span>
        {run.failed > 0 && <span className="tabular-nums text-red-700">{run.failed} failed</span>}
        <span className="tabular-nums" data-testid="run-credits">
          {run.credits_used} credits{cap ? ` of ${cap}` : ''}
        </span>
        {run.error && <span className="text-red-700">{run.error}</span>}
      </div>
      {expanded && <div className="mt-3 border-t border-slate-100 pt-3">{children}</div>}
    </div>
  )
}

/** The expanded run: its live activity and its items. */
function RunDetail({ run, onFinished }: { run: AssistantRun; onFinished: () => void }) {
  const live = LIVE.includes(run.status)
  const { events, done } = useRunEvents(run.id)
  const items = useQuery({
    queryKey: ['assistant-run-items', run.id],
    queryFn: () => api.runItems(run.id, { limit: 500 }),
    refetchInterval: live ? 3000 : false,
  })
  useEffect(() => {
    if (done) {
      onFinished()
      void items.refetch()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [done])
  return (
    <div className="space-y-3" data-testid="run-detail">
      {run.params.options && (
        <p className="text-xs text-slate-500">
          {Object.entries(run.params.options)
            .filter(([, v]) => v !== null && v !== undefined && v !== '')
            .map(([k, v]) => `${k}=${formatValue(v)}`)
            .join(' · ')}
        </p>
      )}
      <ActivityFeed events={events} empty={live ? 'Waiting for activity…' : 'No activity recorded.'} />
      <RunItemsTable items={items.data ?? []} />
    </div>
  )
}

// --- history ----------------------------------------------------------------------------------------

/** Presentational: the runs list. */
export function RunsTable({ runs, selected, onSelect }: { runs: AssistantRun[]; selected: number | null; onSelect: (id: number) => void }) {
  if (runs.length === 0) return <p className="text-sm text-slate-500">No runs yet.</p>
  return (
    <div className="card overflow-x-auto">
      <table className="data-table" data-testid="runs-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Assistant</th>
            <th>Status</th>
            <th>Progress</th>
            <th>Failed</th>
            <th>Credits</th>
            <th>By</th>
            <th>Started</th>
            <th>Took</th>
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
              <td>
                {r.workflow}
                {r.label && <span className="ml-1 text-slate-500">· {r.label}</span>}
              </td>
              <td>
                <StatusBadge status={r.status} />
                {r.pause_reason && <span className="ml-1 text-xs text-slate-500">({r.pause_reason})</span>}
              </td>
              <td className="tabular-nums" title="finished (done, skipped or failed) / total">
                {r.done + r.failed}/{r.total}
              </td>
              <td className="tabular-nums">{r.failed}</td>
              <td className="tabular-nums">{r.credits_used}</td>
              <td>{r.username ?? '—'}</td>
              <td className="whitespace-nowrap text-slate-500">{formatDate(r.started_at ?? r.created_at)}</td>
              <td className="tabular-nums text-slate-500">{duration(r.started_at, r.finished_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Presentational: a run's items. */
export function RunItemsTable({ items }: { items: AssistantRunItem[] }) {
  return (
    <div className="card overflow-x-auto">
      <table className="data-table" data-testid="run-items">
        <thead>
          <tr>
            <th>Item</th>
            <th>Status</th>
            <th>Outcome</th>
            <th>Skipped</th>
            <th>Credits</th>
            <th>Trace</th>
          </tr>
        </thead>
        <tbody>
          {items.map((it) => (
            <tr key={it.id}>
              <td>
                {it.person_id ? (
                  <Link to={`/people/${it.person_id}`} className="text-brand-700 hover:underline">
                    {it.label ?? it.person_id}
                  </Link>
                ) : (
                  <span>{it.label ?? it.id}</span>
                )}
              </td>
              <td>
                <StatusBadge status={it.status} />
                {it.error && <div className="mt-1 max-w-sm text-xs text-red-700">{it.error}</div>}
              </td>
              <td className="max-w-md">
                {it.result.text && <p className="text-sm">{it.result.text}</p>}
                <ChangeBadges changes={it.result.changes ?? []} />
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
          {items.length === 0 && (
            <tr>
              <td colSpan={6} className="py-4 text-center text-slate-500">
                No items yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

const CHANGE_CLASS: Record<Change['action'], string> = {
  set: 'bg-emerald-50 text-emerald-700',
  append: 'bg-emerald-50 text-emerald-700',
  removed: 'bg-red-50 text-red-700',
  normalized: 'bg-sky-50 text-sky-700',
  flagged: 'bg-amber-50 text-amber-700',
  property_created: 'bg-brand-50 text-brand-700',
  enum_value_added: 'bg-brand-50 text-brand-700',
  note: 'bg-slate-100 text-slate-600',
}

export const CHANGE_LABEL: Record<Change['action'], string> = {
  set: 'set',
  append: 'added to',
  removed: 'removed',
  normalized: 'normalised',
  flagged: 'flagged',
  property_created: 'new property',
  enum_value_added: 'new value',
  note: 'note',
}

/** One badge per change: the property key, coloured by what happened; the value on hover. */
export function ChangeBadges({ changes }: { changes: Change[] }) {
  if (changes.length === 0) return null
  return (
    <div className="mt-1 flex flex-wrap gap-1" data-testid="change-badges">
      {changes.map((c, i) => (
        <span key={i} className={`badge ${CHANGE_CLASS[c.action]}`} title={`${CHANGE_LABEL[c.action]}${c.value !== null && c.value !== undefined ? `: ${formatValue(c.value)}` : ''}${c.flag ? ` (${c.flag})` : ''}`}>
          {c.key}
          {c.action !== 'set' && <span className="ml-1 opacity-70">{CHANGE_LABEL[c.action]}</span>}
        </span>
      ))}
    </div>
  )
}
