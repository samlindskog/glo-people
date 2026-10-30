import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { ErrorNote, PageHeader } from '../components/Layout'
import PersonEditor from '../components/PersonEditor'
import { formatDate, formatValue } from '../lib'
import type { FieldEntry } from '../types'
import { TierBadge } from './People'

const SYSTEM = new Set(['id', 'tier', 'name', 'source', 'created_at', 'updated_at', 'schema_version', 'history'])

export default function PersonPage() {
  const { id = '' } = useParams()
  const { can } = useAuth()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [editing, setEditing] = useState(false)
  const person = useQuery({ queryKey: ['person', id], queryFn: () => api.person(id) })
  const fields = useQuery({ queryKey: ['fields'], queryFn: api.fields })
  const remove = useMutation({
    mutationFn: () => api.deletePerson(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['search'] })
      navigate('/people')
    },
  })

  if (person.error) return <ErrorNote error={person.error} />
  if (!person.data || !fields.data) return <p className="text-sm text-slate-500">Loading…</p>
  const p = person.data
  const labels = Object.fromEntries(fields.data.map((f) => [f.key, f]))
  const keys = Object.keys(p).filter((k) => !SYSTEM.has(k)).sort()
  const history = p.history ?? {}
  const nameHistory = history.name ?? []

  return (
    <div className="max-w-4xl">
      <button className="text-sm text-slate-500 hover:underline" onClick={() => navigate(-1)}>
        ← Back
      </button>
      <PageHeader title={p.name}>
        <TierBadge tier={p.tier} />
        {can('people:write') && !editing && (
          <>
            <button className="btn-secondary" onClick={() => setEditing(true)}>
              Edit
            </button>
            <button
              className="btn-danger"
              onClick={() => {
                if (confirm(`Delete ${p.name}? This cannot be undone.`)) remove.mutate()
              }}
            >
              Delete
            </button>
          </>
        )}
      </PageHeader>
      <ErrorNote error={remove.error} />

      {editing ? (
        <PersonEditor
          person={p}
          fields={fields.data}
          onCancel={() => setEditing(false)}
          onSave={async (tier, changes) => {
            const updated = await api.updatePerson(id, changes, tier !== p.tier ? tier : undefined)
            qc.setQueryData(['person', id], updated)
            qc.invalidateQueries({ queryKey: ['search'] })
            qc.invalidateQueries({ queryKey: ['fields'] })
            setEditing(false)
          }}
        />
      ) : (
        <div className="card divide-y divide-slate-100">
          {nameHistory.length > 1 && (
            <div className="grid grid-cols-[12rem_1fr] gap-4 px-4 py-2.5 text-sm">
              <div className="text-slate-500">Name</div>
              <div>
                <div>{p.name}</div>
                <History entries={nameHistory} />
              </div>
            </div>
          )}
          {keys.map((k) => (
            <div key={k} className="grid grid-cols-[12rem_1fr] gap-4 px-4 py-2.5 text-sm">
              <div className="text-slate-500" title={k}>
                {labels[k]?.label ?? k}
                {labels[k]?.embed && <span className="ml-1 text-xs text-brand-500" title="Embedded for semantic search">◆</span>}
              </div>
              <div>
                <FieldValue value={p[k]} type={labels[k]?.type} />
                <History entries={history[k] ?? []} />
              </div>
            </div>
          ))}
        </div>
      )}

      <p className="mt-4 text-xs text-slate-400">
        {p.source && `Imported from ${p.source.file} (row ${p.source.row}) · `}
        Created {formatDate(p.created_at)} · Updated {formatDate(p.updated_at)} · id {p.id}
      </p>
    </div>
  )
}

function FieldValue({ value, type }: { value: unknown; type?: string }) {
  if (Array.isArray(value))
    return (
      <div className="flex flex-wrap gap-1">
        {value.map((v, i) => (
          <span key={i} className="badge bg-slate-100 text-slate-700">
            {String(v)}
          </span>
        ))}
      </div>
    )
  const s = String(value)
  if (type === 'url' || /^https?:\/\//.test(s)) {
    const href = s.startsWith('http') ? s : `https://${s}`
    return (
      <a href={href} target="_blank" rel="noreferrer noopener" className="break-all text-brand-700 hover:underline">
        {s}
      </a>
    )
  }
  return <div className="whitespace-pre-wrap break-words">{s}</div>
}

/** Who wrote a value and when: shown when a field has more than one entry or came from an agent. */
export function History({ entries }: { entries: FieldEntry[] }) {
  const show = entries.length > 1 || entries.some((e) => e.src?.kind === 'agent')
  if (!show) return null
  const newestFirst = [...entries].reverse()
  return (
    <details className="mt-1 text-xs text-slate-500">
      <summary className="cursor-pointer select-none">history ({entries.length})</summary>
      <ul className="mt-1 space-y-1 border-l border-slate-200 pl-3">
        {newestFirst.map((e, i) => (
          <li key={i} className="flex flex-wrap gap-x-2">
            <span className="text-slate-400">{e.ts ? formatDate(e.ts) : '—'}</span>
            <span className="badge bg-slate-100 text-slate-600">{describeSource(e)}</span>
            <span className={`break-all ${e.v === null ? 'italic' : 'text-slate-700'}`}>
              {e.v === null ? '(removed)' : formatValue(e.v)}
            </span>
          </li>
        ))}
      </ul>
    </details>
  )
}

function describeSource(e: FieldEntry): string {
  if (!e.src) return 'legacy'
  const who = e.src.by ? `${e.src.kind} · ${e.src.by}` : e.src.kind
  // Workflow writes carry run:N/item:M; MCP writes carry key:N (plus the caller's agent name).
  return e.src.ref && e.src.kind !== 'import' ? `${who} (${e.src.ref})` : who
}
