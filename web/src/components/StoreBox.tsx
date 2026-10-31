import { useMutation } from '@tanstack/react-query'
import { useRef, useState, type FormEvent } from 'react'
import { api } from '../api'
import { formatValue } from '../lib'
import type { NormalizeReport, Person, StoreAction, StoreReport } from '../types'
import { ErrorNote } from './Layout'

const DEFAULTS = { near_duplicate: 0.8, max_new: 2 }
const ACTION_CLASS: Record<StoreAction, string> = {
  set: 'bg-emerald-50 text-emerald-700',
  append: 'bg-emerald-50 text-emerald-700',
  created: 'bg-brand-50 text-brand-700',
  redirected: 'bg-sky-50 text-sky-700',
  to_notes: 'bg-sky-50 text-sky-700',
  confirmed: 'bg-slate-100 text-slate-600',
  ignored: 'bg-slate-100 text-slate-400',
}
const ACTION_LABEL: Record<StoreAction, string> = { set: 'set', append: 'added to', created: 'new property', redirected: 'redirected', to_notes: 'to notes', confirmed: 'confirmed', ignored: 'ignored' }

/** Add free text about a person: an agent splits it into statements and files each under the
 * best-matching property (or a new free-text one, within the caps). */
export default function StoreBox({ personId, onStored }: { personId: string; onStored: (person: Person) => void }) {
  const [passage, setPassage] = useState('')
  const [nearDuplicate, setNearDuplicate] = useState(DEFAULTS.near_duplicate)
  const [maxNew, setMaxNew] = useState(DEFAULTS.max_new)
  const fileRef = useRef<HTMLInputElement>(null)
  const store = useMutation({
    mutationFn: (file?: File) => (file ? api.storeFile(personId, file) : api.store(personId, passage.trim(), { near_duplicate: nearDuplicate, max_new: maxNew })),
    onSuccess: (report) => {
      if (report.person) onStored(report.person)
      setPassage('')
      if (fileRef.current) fileRef.current.value = ''
    },
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    if (passage.trim()) store.mutate(undefined)
  }

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
          <button className="btn-primary" disabled={store.isPending || !passage.trim()}>
            {store.isPending ? 'Storing…' : 'Store'}
          </button>
          <label className="text-sm text-slate-600">
            or a file:{' '}
            <input
              ref={fileRef}
              type="file"
              accept=".txt,.md,text/plain,text/markdown"
              aria-label="Passage file"
              className="text-sm"
              disabled={store.isPending}
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) store.mutate(f)
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
      <ErrorNote error={store.error} />
      {store.data && <Report report={store.data} />}
    </section>
  )
}

function Report({ report }: { report: StoreReport }) {
  if (report.items.length === 0) return <p className="text-sm text-slate-500">Nothing about the person was found in the passage.</p>
  return (
    <div className="space-y-2">
      <table className="data-table" data-testid="store-report">
        <thead>
          <tr>
            <th>Property</th>
            <th>Value</th>
            <th>Why</th>
          </tr>
        </thead>
        <tbody>
          {report.items.map((it, i) => (
            <tr key={i} title={it.evidence ? `“${it.evidence}”` : undefined}>
              <td className="whitespace-nowrap">
                <span className={`badge ${ACTION_CLASS[it.action]}`}>{ACTION_LABEL[it.action]}</span>
                {it.key && <span className="ml-1 font-mono text-xs">{it.key}</span>}
                {it.conflict && (
                  <span className="badge ml-1 bg-amber-50 text-amber-700" title="Differs from the value it replaces (kept in history)">
                    conflict
                  </span>
                )}
              </td>
              <td className="max-w-md">{formatValue(it.value)}</td>
              <td className="max-w-xs text-xs text-slate-500">{it.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {report.normalized && <NormalizedLine report={report.normalized} />}
    </div>
  )
}

function NormalizedLine({ report }: { report: NormalizeReport }) {
  const parts = [
    ...Object.entries(report.mapped).map(([k, v]) => `${k} → ${formatValue(v)}`),
    ...report.non_normalizable.map((k) => `${k}: no value`),
    ...report.unmapped.map((k) => `${k}: still unmapped`),
  ]
  if (parts.length === 0) return null
  return (
    <p className="text-xs text-slate-500" data-testid="store-normalized">
      Normalised: {parts.join(' · ')}
    </p>
  )
}
