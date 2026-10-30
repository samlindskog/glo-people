import { useState, type FormEvent } from 'react'
import type { Person, Property, Tier } from '../types'

const SYSTEM = new Set(['id', 'tier', 'source', 'created_at', 'updated_at', 'schema_version', 'history', 'norm', 'norm_flags', 'history_norm'])

interface Row {
  key: string
  text: string
  original?: unknown
  isNew: boolean
  removed: boolean
}

function toText(v: unknown): string {
  if (Array.isArray(v)) return v.join('; ')
  return v === null || v === undefined ? '' : String(v)
}

function fromText(text: string, type: Property['type'] | undefined, original: unknown): unknown {
  const t = text.trim()
  if (!t) return null
  if (type === 'list' || type === 'enum_list' || Array.isArray(original)) return t.split(';').map((s) => s.trim()).filter(Boolean)
  if ((type === 'number' || typeof original === 'number') && !Number.isNaN(Number(t))) return Number(t)
  return t
}

interface Props {
  person?: Person
  properties: Property[]
  onSave: (tier: Tier, changes: Record<string, unknown>) => Promise<void>
  onCancel?: () => void
}

/** Edits a person's values for existing properties (the catalog decides which exist); returns only
 * changed keys (null = remove). */
export default function PersonEditor({ person, properties, onSave, onCancel }: Props) {
  const types = Object.fromEntries(properties.map((p) => [p.key, p.type]))
  const [tier, setTier] = useState<Tier>(person?.tier ?? 'member')
  const [rows, setRows] = useState<Row[]>(() => {
    if (!person) return [{ key: 'name', text: '', isNew: true, removed: false }]
    const keys = Object.keys(person).filter((k) => !SYSTEM.has(k))
    keys.sort((a, b) => (a === 'name' ? -1 : b === 'name' ? 1 : a.localeCompare(b)))
    return keys.map((k) => ({ key: k, text: toText(person[k]), original: person[k], isNew: false, removed: false }))
  })
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const set = (i: number, patch: Partial<Row>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)))

  async function submit(e: FormEvent) {
    e.preventDefault()
    const changes: Record<string, unknown> = {}
    for (const r of rows) {
      if (!r.key) continue
      const value = r.removed ? null : fromText(r.text, types[r.key], r.original)
      if (r.isNew || JSON.stringify(value) !== JSON.stringify(r.original ?? null)) changes[r.key] = value
    }
    setBusy(true)
    setError(null)
    try {
      await onSave(tier, changes)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const taken = new Set(rows.map((r) => r.key))
  const available = properties.filter((p) => !taken.has(p.key))

  return (
    <form onSubmit={submit} className="card space-y-3 p-4" aria-label="Edit person">
      {error && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}
      <label className="flex items-center gap-3">
        <span className="label w-40">Tier</span>
        <select className="input w-40" value={tier} onChange={(e) => setTier(e.target.value as Tier)} aria-label="Tier">
          <option value="member">Member</option>
          <option value="plus_one">Plus one</option>
        </select>
      </label>
      {rows.map((r, i) => (
        <div key={i} className={`flex items-start gap-3 ${r.removed ? 'opacity-40' : ''}`}>
          {r.isNew && r.key !== 'name' ? (
            <select className="input w-40 text-xs" aria-label="New property" value={r.key} onChange={(e) => set(i, { key: e.target.value })}>
              <option value="">Property…</option>
              {[...(r.key ? properties.filter((p) => p.key === r.key) : []), ...available].map((p) => (
                <option key={p.key} value={p.key}>
                  {p.label} ({p.key})
                </option>
              ))}
            </select>
          ) : (
            <span className="label w-40 shrink-0 pt-2" title={r.key}>
              {properties.find((p) => p.key === r.key)?.label ?? r.key}
            </span>
          )}
          {r.text.length > 80 || r.key.includes('bio') || r.key.includes('note') ? (
            <textarea className="input min-h-24" aria-label={r.key || 'value'} value={r.text} disabled={r.removed} onChange={(e) => set(i, { text: e.target.value })} />
          ) : (
            <input className="input" aria-label={r.key || 'value'} value={r.text} disabled={r.removed} onChange={(e) => set(i, { text: e.target.value })} />
          )}
          {r.key !== 'name' && (
            <button
              type="button"
              className="btn-ghost shrink-0"
              aria-label={r.removed ? `Restore ${r.key}` : `Remove ${r.key}`}
              onClick={() => (r.isNew ? setRows(rows.filter((_, j) => j !== i)) : set(i, { removed: !r.removed }))}
            >
              {r.removed ? '↺' : '✕'}
            </button>
          )}
        </div>
      ))}
      <p className="text-xs text-slate-500">
        List properties: separate items with “;”. Saving records a new entry for each changed property; previous values
        are kept in its history. Only existing properties can be set (see Properties).
      </p>
      <div className="flex gap-2">
        <button type="button" className="btn-secondary" disabled={available.length === 0} onClick={() => setRows([...rows, { key: '', text: '', isNew: true, removed: false }])}>
          + Add property
        </button>
        <span className="flex-1" />
        {onCancel && (
          <button type="button" className="btn-ghost" onClick={onCancel}>
            Cancel
          </button>
        )}
        <button className="btn-primary" disabled={busy}>
          {busy ? 'Saving…' : 'Save'}
        </button>
      </div>
    </form>
  )
}
