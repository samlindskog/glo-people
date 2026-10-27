import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate, useParams } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { ErrorNote, PageHeader } from '../components/Layout'
import PersonEditor from '../components/PersonEditor'
import { formatDate } from '../lib'
import { TierBadge } from './People'

const SYSTEM = new Set(['id', 'tier', 'name', 'source', 'created_at', 'updated_at'])

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
          {keys.map((k) => (
            <div key={k} className="grid grid-cols-[12rem_1fr] gap-4 px-4 py-2.5 text-sm">
              <div className="text-slate-500" title={k}>
                {labels[k]?.label ?? k}
                {labels[k]?.embed && <span className="ml-1 text-xs text-brand-500" title="Embedded for semantic search">◆</span>}
              </div>
              <FieldValue value={p[k]} type={labels[k]?.type} />
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
