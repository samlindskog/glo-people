import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { ErrorNote } from '../components/Layout'
import { formatDate } from '../lib'
import type { ActivityItem } from '../types'
import { Pager } from './Searches'

const PAGE = 100
const ACTIONS = ['', 'auth', 'search', 'ask', 'mcp', 'person', 'people.import', 'assistant', 'field', 'user', 'apikey']

export default function ActivityPanel() {
  const { can } = useAuth()
  const [userId, setUserId] = useState('')
  const [action, setAction] = useState('')
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')
  const [offset, setOffset] = useState(0)

  const users = useQuery({ queryKey: ['users'], queryFn: api.users, enabled: can('accounts:manage') })
  const q = useQuery({
    queryKey: ['activity', userId, action, since, until, offset],
    queryFn: () =>
      api.activity({
        user_id: userId ? Number(userId) : undefined,
        action: action || undefined,
        since: since ? new Date(since).toISOString() : undefined,
        until: until ? new Date(until).toISOString() : undefined,
        limit: PAGE,
        offset,
      }),
  })
  const reset = (fn: () => void) => {
    fn()
    setOffset(0)
  }

  return (
    <div>
      <div className="card mb-4 flex flex-wrap items-end gap-3 p-3">
        {users.data && (
          <label className="space-y-1">
            <span className="label">User</span>
            <select className="input w-44" value={userId} aria-label="User" onChange={(e) => reset(() => setUserId(e.target.value))}>
              <option value="">Everyone</option>
              {users.data.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.username}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="space-y-1">
          <span className="label">Action</span>
          <select className="input w-44" value={action} aria-label="Action" onChange={(e) => reset(() => setAction(e.target.value))}>
            {ACTIONS.map((a) => (
              <option key={a} value={a}>
                {a || 'All'}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1">
          <span className="label">From</span>
          <input type="datetime-local" className="input" value={since} onChange={(e) => reset(() => setSince(e.target.value))} />
        </label>
        <label className="space-y-1">
          <span className="label">To</span>
          <input type="datetime-local" className="input" value={until} onChange={(e) => reset(() => setUntil(e.target.value))} />
        </label>
      </div>
      <ErrorNote error={q.error} />
      <div className="card overflow-x-auto">
        <table className="data-table" data-testid="activity-table">
          <thead>
            <tr>
              <th>When</th>
              <th>User</th>
              <th>Action</th>
              <th>Details</th>
              <th>IP</th>
            </tr>
          </thead>
          <tbody>
            {q.data?.items.map((a) => (
              <tr key={a.id}>
                <td className="whitespace-nowrap text-slate-500">{formatDate(a.created_at)}</td>
                <td>
                  {a.username ?? '—'}
                  {a.api_key_id && <span className="badge ml-1 bg-violet-50 text-violet-700">key</span>}
                </td>
                <td className="font-mono text-xs">{a.action}</td>
                <td className="max-w-xl text-xs text-slate-600">{details(a)}</td>
                <td className="text-xs text-slate-400">{a.ip}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pager offset={offset} size={PAGE} hasMore={!!q.data?.has_more} onChange={setOffset} />
    </div>
  )
}

function details(a: ActivityItem): string {
  const m = a.meta as Record<string, unknown>
  if (a.action === 'search' || a.action === 'mcp.search') {
    const parts: string[] = []
    if (m.tier) parts.push(`tier=${m.tier}`)
    if (Array.isArray(m.filters)) parts.push(...(m.filters as string[]))
    const s = m.semantic as { query: string; fields: string[]; mode: string; threshold: number } | undefined
    if (s) parts.push(`“${s.query}” in ${s.fields.join(',')} ${s.mode}≥${s.threshold}`)
    return `${parts.join(' · ') || 'everyone'} → ${m.total} results`
  }
  const rest = Object.keys(m).length ? JSON.stringify(m) : ''
  return [a.target, rest].filter(Boolean).join(' ')
}
