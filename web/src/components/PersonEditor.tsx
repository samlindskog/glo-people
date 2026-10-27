import { useState, type FormEvent } from 'react'
import type { FieldInfo, Person, Tier } from '../types'

const SYSTEM = new Set(['id', 'tier', 'source', 'created_at', 'updated_at'])
const KEY_RE = /^[a-z][a-z0-9_]{0,63}$/

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

function fromText(text: string, type: FieldInfo['type'] | undefined, original: unknown): unknown {
  const t = text.trim()
  if (!t) return null
  if (type === 'list' || Array.isArray(original)) return t.split(';').map((s) => s.trim()).filter(Boolean)
  if ((type === 'number' || typeof original === 'number') && !Number.isNaN(Number(t))) return Number(t)
  return t
}

interface Props {
  person?: Person
  fields: FieldInfo[]
  onSave: (tier: Tier, changes: Record<string, unknown>) => Promise<void>
  onCancel?: () => void
}

/** Edits any field on a schema-less person; returns only changed keys (null = remove). */
export default function PersonEditor({ person, fields, onSave, onCancel }: Props) {
  const types = Object.fromEntries(fields.map((f) => [f.key, f.type]))
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
      if (!KEY_RE.test(r.key)) {
        setError(`“${r.key}” is not a valid field key (lowercase letters, digits, underscores)`)
        return
      }
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
            <input
              className="input w-40 font-mono text-xs"
              placeholder="field_key"
              aria-label="New field key"
              list="field-keys"
              value={r.key}
              onChange={(e) => set(i, { key: e.target.value.trim().toLowerCase() })}
            />
          ) : (
            <span className="label w-40 shrink-0 pt-2" title={r.key}>
              {fields.find((f) => f.key === r.key)?.label ?? r.key}
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
      <datalist id="field-keys">
        {fields.map((f) => (
          <option key={f.key} value={f.key} />
        ))}
      </datalist>
      <p className="text-xs text-slate-500">List fields: separate items with “;”.</p>
      <div className="flex gap-2">
        <button type="button" className="btn-secondary" onClick={() => setRows([...rows, { key: '', text: '', isNew: true, removed: false }])}>
          + Add field
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
