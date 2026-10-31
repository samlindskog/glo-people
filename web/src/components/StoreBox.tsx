import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api } from '../api'
import { formatValue } from '../lib'
import { describeNormalization } from '../pages/Person'
import { CHANGE_LABEL } from '../pages/Assistants'
import type { Change, StoreResult } from '../types'
import { InvocationPanel, useInvocation } from './Invocation'
import { ErrorNote } from './Layout'

const DEFAULTS = { near_duplicate: 0.8, max_new: 2 }
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

/** Add free text about a person: the store tool splits it into statements and files each under
 * the best-matching property (or a new free-text one, within the caps). It runs as an invocation:
 * its activity streams in and it can be stopped before anything is written. */
export default function StoreBox({ personId, onStored }: { personId: string; onStored: () => void }) {
  const [passage, setPassage] = useState('')
  const [nearDuplicate, setNearDuplicate] = useState(DEFAULTS.near_duplicate)
  const [maxNew, setMaxNew] = useState(DEFAULTS.max_new)
  const fileRef = useRef<HTMLInputElement>(null)
  const inv = useInvocation<StoreResult>()

  useEffect(() => {
    if (inv.status === 'done') {
      onStored()
      setPassage('')
      if (fileRef.current) fileRef.current.value = ''
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inv.status])

  function submit(e: FormEvent) {
    e.preventDefault()
    if (passage.trim()) void inv.start(() => api.store(personId, passage.trim(), { near_duplicate: nearDuplicate, max_new: maxNew }))
  }
  const busy = inv.status === 'running'

  return (
    <section className="card space-y-3 p-4" aria-label="Add information">
      <form onSubmit={submit} className="space-y-2">
        <label className="block space-y-1">
          <span className="label">Add information</span>
          <textarea
            className="input min-h-20"
            aria-label="Passage"
            placeholder="Notes from a call, a message, a profile… Each fact is filed under the property it belongs to."
            value={passage}
            onChange={(e) => setPassage(e.target.value)}
          />
        </label>
        <div className="flex flex-wrap items-center gap-3">
          <button className="btn-primary" disabled={busy || !passage.trim()}>
            {busy ? 'Storing…' : 'Store'}
          </button>
          <label className="text-sm text-slate-600">
            or a file:{' '}
            <input
              ref={fileRef}
              type="file"
              accept=".txt,.md,text/plain,text/markdown"
              aria-label="Passage file"
              className="text-sm"
              disabled={busy}
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) void inv.start(() => api.storeFile(personId, f))
              }}
            />
          </label>
          <details className="text-sm">
            <summary className="cursor-pointer select-none text-slate-600">Advanced</summary>
            <div className="mt-2 flex flex-wrap items-end gap-4">
              <label className="space-y-1">
                <span className="label">Near-duplicate</span>
                <input type="number" className="input w-24" aria-label="Near-duplicate threshold" min={0} max={1} step={0.05} value={nearDuplicate} onChange={(e) => setNearDuplicate(Number(e.target.value))} />
              </label>
              <label className="space-y-1">
                <span className="label">New properties (max)</span>
                <input type="number" className="input w-24" aria-label="Max new properties" min={0} max={5} value={maxNew} onChange={(e) => setMaxNew(Number(e.target.value))} />
              </label>
            </div>
          </details>
        </div>
      </form>
      <ErrorNote error={inv.error} />
      {inv.status === 'running' && <InvocationPanel events={inv.events} status={inv.status} onStop={() => void inv.stop()} label="Storing" />}
      {inv.result && <Report result={inv.result} />}
    </section>
  )
}

function Report({ result }: { result: StoreResult }) {
  const lines: string[] = []
  if (result.confirmed.length) lines.push(`Confirmed (already there): ${result.confirmed.join(', ')}`)
  if (result.conflicts.length) lines.push(`Replaced a different value (kept in history): ${result.conflicts.join(', ')}`)
  if (Object.keys(result.redirected).length) lines.push(`Redirected to existing properties: ${Object.entries(result.redirected).map(([a, b]) => `${a} → ${b}`).join(', ')}`)
  if (result.to_notes.length) lines.push(`Filed under notes (new-property cap): ${result.to_notes.join(', ')}`)
  if (result.ignored.length) lines.push(`Ignored: ${result.ignored.join(' · ')}`)
  if (result.credits) lines.push(`${result.credits} Firecrawl credits`)
  if (result.changes.length === 0 && lines.length === 0) return <p className="text-sm text-slate-500">Nothing about the person was found in the passage.</p>
  return (
    <div className="space-y-2">
      {result.changes.length > 0 && (
        <table className="data-table" data-testid="store-report">
          <thead>
            <tr>
              <th>Property</th>
              <th>Change</th>
              <th>Value</th>
            </tr>
          </thead>
          <tbody>
            {result.changes.map((c, i) => (
              <tr key={i}>
                <td className="font-mono text-xs">{c.key}</td>
                <td>
                  <span className={`badge ${CHANGE_CLASS[c.action]}`}>{CHANGE_LABEL[c.action]}</span>
                  {c.flag && <span className="ml-1 text-xs text-slate-500">{c.flag === 'non_normalizable' ? 'no value' : 'not recognised'}</span>}
                </td>
                <td className="max-w-md">
                  {c.previous !== null && c.previous !== undefined && c.action === 'set' && <span className="text-slate-400 line-through">{formatValue(c.previous)} </span>}
                  {formatValue(c.value)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {lines.map((l) => (
        <p key={l} className="text-xs text-slate-500">
          {l}
        </p>
      ))}
      {result.normalization && (
        <p className="text-xs text-slate-500" data-testid="store-normalized">
          {describeNormalization(result.normalization)}
        </p>
      )}
    </div>
  )
}
