import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useSearchParams } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { ErrorNote, PageHeader } from '../components/Layout'
import { formatDate } from '../lib'
import ActivityPanel from './Activity'
import SearchesPanel from './Searches'

type Tab = 'searches' | 'activity' | 'agents'

/** What happened: the searches (yours, or everyone's) and the activity log, as two tabs. */
export default function HistoryPage() {
  const { can } = useAuth()
  const [params, setParams] = useSearchParams()
  const tabs: { key: Tab; label: string }[] = [{ key: 'searches', label: 'Searches' }]
  if (can('activity:read')) tabs.push({ key: 'activity', label: 'Activity' }, { key: 'agents', label: 'Agents' })
  const current = (params.get('tab') as Tab) ?? 'searches'
  const tab = tabs.some((t) => t.key === current) ? current : 'searches'

  return (
    <div>
      <PageHeader title="History">
        <div className="flex gap-1 rounded-md bg-slate-100 p-1" role="tablist" aria-label="History">
          {tabs.map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={tab === t.key}
              className={`rounded px-3 py-1 text-sm ${tab === t.key ? 'bg-white font-medium text-slate-900 shadow-xs' : 'text-slate-600 hover:text-slate-900'}`}
              onClick={() => setParams(t.key === 'searches' ? {} : { tab: t.key })}
            >
              {t.label}
            </button>
          ))}
        </div>
      </PageHeader>
      {tab === 'searches' ? <SearchesPanel /> : tab === 'agents' ? <AgentsPanel /> : <ActivityPanel />}
    </div>
  )
}


/** How the agents behind each API key use the application over MCP: the report that points at
 * product fixes, the keys, and one key's conversations with their calls in order. */
function AgentsPanel() {
  const [days, setDays] = useState(30)
  const [keyId, setKeyId] = useState<number | null>(null)
  const report = useQuery({ queryKey: ['agent-report', days], queryFn: () => api.agentReport(days) })
  const keys = useQuery({ queryKey: ['agent-keys', days], queryFn: () => api.agents(days) })
  const sessions = useQuery({
    queryKey: ['agent-sessions', keyId, days],
    queryFn: () => api.agentSessions(keyId!, days),
    enabled: keyId !== null,
  })
  const rep = report.data
  return (
    <div className="space-y-4" data-testid="agents-panel">
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-slate-600">
          Last
          <select className="input w-24" aria-label="Days" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[7, 30, 90].map((d) => (
              <option key={d} value={d}>
                {d} days
              </option>
            ))}
          </select>
        </label>
        <ErrorNote error={report.error ?? keys.error ?? sessions.error} />
      </div>
      {rep && (
        <div className="grid gap-3 md:grid-cols-2">
          <div className="card p-4 text-sm">
            <p className="font-semibold">
              {rep.calls} calls · {rep.errors} errors · {rep.keys} keys · {rep.runs_started} runs started
            </p>
            <p className="mt-1 text-slate-600">
              {rep.retries} retries · {rep.dry_runs} dry runs, {rep.writes_after_dry_run} followed by a write · catalog re-read {rep.list_properties_rereads}×
            </p>
            {rep.unused_tools.length > 0 && <p className="mt-1 text-xs text-slate-500">never used: {rep.unused_tools.join(', ')}</p>}
            <table className="data-table mt-3">
              <thead>
                <tr>
                  <th>Tool</th>
                  <th>Calls</th>
                  <th>Errors</th>
                  <th>p50</th>
                </tr>
              </thead>
              <tbody>
                {rep.by_tool.map((t) => (
                  <tr key={t.tool}>
                    <td className="font-mono text-xs">{t.tool}</td>
                    <td>{t.calls}</td>
                    <td className={t.errors ? 'text-red-700' : ''}>{t.errors}</td>
                    <td>{t.p50_ms} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card p-4 text-sm">
            <p className="font-semibold">What agents trip on</p>
            {rep.top_errors.length === 0 ? (
              <p className="mt-1 text-slate-500">No errors in this period.</p>
            ) : (
              <ul className="mt-2 space-y-1">
                {rep.top_errors.map((e, i) => (
                  <li key={i} className="text-xs">
                    <span className="badge bg-red-50 text-red-700">{e.n}×</span> <span className="font-mono">{e.tool}</span>: {e.error}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
      <div className="card overflow-x-auto">
        <table className="data-table" data-testid="agent-keys">
          <thead>
            <tr>
              <th>Key</th>
              <th>Owner</th>
              <th>Calls</th>
              <th>Errors</th>
              <th>Conversations</th>
              <th>Tools</th>
              <th>Last seen</th>
            </tr>
          </thead>
          <tbody>
            {(keys.data?.keys ?? []).map((k) => (
              <tr key={k.api_key_id ?? 'none'} className={k.api_key_id === keyId ? 'bg-brand-50' : ''}>
                <td>
                  <button type="button" className="text-brand-700 hover:underline" onClick={() => setKeyId(k.api_key_id)}>
                    {k.key_name ?? '(no key)'}
                  </button>
                </td>
                <td>{k.username ?? '—'}</td>
                <td>{k.calls}</td>
                <td className={k.errors ? 'text-red-700' : ''}>{k.errors}</td>
                <td>{k.sessions}</td>
                <td className="text-xs text-slate-600">
                  {Object.entries(k.tools)
                    .slice(0, 6)
                    .map(([t, n]) => `${t} ${n}`)
                    .join(' · ')}
                </td>
                <td className="text-xs text-slate-500">{formatDate(k.last_seen)}</td>
              </tr>
            ))}
            {keys.data && keys.data.keys.length === 0 && (
              <tr>
                <td colSpan={7} className="text-slate-500">
                  No MCP calls in this period.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {keyId !== null && sessions.data && (
        <div className="space-y-3" data-testid="agent-sessions">
          {sessions.data.sessions.map((s, i) => (
            <details key={i} className="card p-3 text-sm" open={i === 0}>
              <summary className="cursor-pointer select-none">
                {formatDate(s.started_at)} · {s.calls_n} calls{s.errors > 0 && <span className="ml-1 text-red-700">· {s.errors} errors</span>} ·{' '}
                <span className="font-mono text-xs text-slate-600">{s.tools.slice(0, 8).join(' → ')}{s.tools.length > 8 && ' → …'}</span>
              </summary>
              <ol className="mt-2 space-y-1 border-l border-slate-200 pl-3 text-xs">
                {s.calls.map((c) => (
                  <li key={c.id} className="flex flex-wrap gap-x-2">
                    <span className="text-slate-400">{formatDate(c.created_at)}</span>
                    <span className={`badge ${c.ok ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700'}`}>{c.tool}</span>
                    <span className="text-slate-500">{c.duration_ms} ms</span>
                    <span className="break-all font-mono text-slate-600">{JSON.stringify(c.args)}</span>
                    {c.error && <span className="text-red-700">{c.error}</span>}
                  </li>
                ))}
              </ol>
            </details>
          ))}
        </div>
      )}
    </div>
  )
}
