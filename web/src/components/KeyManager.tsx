import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { formatDate } from '../lib'
import type { ApiKey } from '../types'
import { ErrorNote } from './Layout'

interface Props {
  queryKey: unknown[]
  list: () => Promise<ApiKey[]>
  issue: (name: string) => Promise<ApiKey>
  revoke: (id: number) => Promise<void>
}

export default function KeyManager({ queryKey, list, issue, revoke }: Props) {
  const qc = useQueryClient()
  const keys = useQuery({ queryKey, queryFn: list })
  const [name, setName] = useState('')
  const [fresh, setFresh] = useState<ApiKey | null>(null)
  const issueM = useMutation({
    mutationFn: issue,
    onSuccess: (k) => {
      setFresh(k)
      setName('')
      qc.invalidateQueries({ queryKey })
    },
  })
  const revokeM = useMutation({ mutationFn: revoke, onSuccess: () => qc.invalidateQueries({ queryKey }) })

  function submit(e: FormEvent) {
    e.preventDefault()
    if (name.trim()) issueM.mutate(name.trim())
  }

  return (
    <div className="space-y-4">
      <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
        <label className="space-y-1">
          <span className="label">Key name</span>
          <input className="input w-64" placeholder="e.g. Claude Desktop" aria-label="Key name" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <button className="btn-primary" disabled={!name.trim() || issueM.isPending}>
          Issue key
        </button>
      </form>
      <ErrorNote error={issueM.error ?? revokeM.error ?? keys.error} />

      {fresh?.key && (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm" data-testid="new-key">
          <p className="font-medium text-emerald-800">Copy this key now. It won’t be shown again.</p>
          <div className="mt-2 flex items-center gap-2">
            <code className="flex-1 break-all rounded bg-white px-2 py-1 font-mono text-xs" data-testid="new-key-value">
              {fresh.key}
            </code>
            <button type="button" className="btn-secondary" onClick={() => navigator.clipboard?.writeText(fresh.key!)}>
              Copy
            </button>
          </div>
          <McpSnippet apiKey={fresh.key} />
        </div>
      )}

      <div className="card overflow-x-auto">
        <table className="data-table" data-testid="keys-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Prefix</th>
              <th>Created</th>
              <th>Last used</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.data?.map((k) => (
              <tr key={k.id} className={k.revoked_at ? 'text-slate-400' : ''}>
                <td className="font-medium">{k.name}</td>
                <td className="font-mono text-xs">glo_{k.prefix}_…</td>
                <td className="whitespace-nowrap">{formatDate(k.created_at)}</td>
                <td className="whitespace-nowrap">{formatDate(k.last_used_at)}</td>
                <td>
                  {k.revoked_at ? (
                    <span className="badge bg-slate-100">revoked</span>
                  ) : (
                    <span className="badge bg-emerald-50 text-emerald-700">active</span>
                  )}
                </td>
                <td>
                  {!k.revoked_at && (
                    <button
                      className="btn-danger"
                      onClick={() => {
                        if (confirm(`Revoke “${k.name}”? Clients using it will stop working.`)) revokeM.mutate(k.id)
                      }}
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {keys.data?.length === 0 && (
              <tr>
                <td colSpan={6} className="py-6 text-center text-slate-500">
                  No keys yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function McpSnippet({ apiKey }: { apiKey?: string }) {
  const url = `${window.location.origin}/mcp`
  const key = apiKey ?? 'glo_…'
  const config = JSON.stringify(
    { mcpServers: { 'glo-people': { type: 'http', url, headers: { Authorization: `Bearer ${key}` } } } },
    null,
    2,
  )
  return (
    <div className="mt-3 space-y-2 text-xs">
      <p className="text-slate-600">
        MCP endpoint <code className="rounded bg-white px-1">{url}</code> (streamable HTTP, bearer auth). Claude Code:
      </p>
      <pre className="overflow-x-auto rounded bg-slate-900 p-2 text-slate-100">{`claude mcp add --transport http glo-people ${url} --header "Authorization: Bearer ${key}"`}</pre>
      <p className="text-slate-600">Or JSON config:</p>
      <pre className="overflow-x-auto rounded bg-slate-900 p-2 text-slate-100">{config}</pre>
    </div>
  )
}
