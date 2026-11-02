import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api'
import type { ImportColumn, ImportMatch, ImportPreview, ImportStarted, Tier } from '../types'
import { TIERS } from '../types'
import { ErrorNote } from './Layout'

/** Pick a CSV, preview what the merge would do (each column's plan, the match decisions of the
 * first rows), then merge it or dry-run it. Either is an assistant run, one item per person the
 * file describes; `onStarted` gets the run so the caller can follow it. */
export default function ImportDialog({ onStarted }: { onStarted?: (started: ImportStarted) => void }) {
  const qc = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [tier, setTier] = useState<Tier>('member')
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [started, setStarted] = useState<ImportStarted | null>(null)
  const [columns, setColumns] = useState<Record<string, string>>({})
  const properties = useQuery({ queryKey: ['properties'], queryFn: api.properties })

  const previewM = useMutation({
    mutationFn: ({ f, cols }: { f: File; cols: Record<string, string> }) => api.importPreview(f, cols),
    onSuccess: setPreview,
  })
  const remap = (header: string, target: string) => {
    const next = { ...columns, [header]: target }
    setColumns(next)
    if (file) previewM.mutate({ f: file, cols: next })
  }
  const mergeM = useMutation({
    mutationFn: (dryRun: boolean) => api.importCsv(file!, tier, dryRun, columns),
    onSuccess: (r) => {
      setStarted(r)
      qc.invalidateQueries({ queryKey: ['properties'] })
      qc.invalidateQueries({ queryKey: ['assistant-runs'] })
      onStarted?.(r)
    },
  })

  return (
    <div className="space-y-3" data-testid="import-dialog">
      <p className="text-xs text-slate-500">
        Any columns are accepted: each header becomes a property (new ones are created). Each row is matched to a person by a shared email, phone,
        LinkedIn or GitHub URL, the <code>ID</code> column, or a unique name; otherwise a person is created in the chosen tier. Known values are
        written as entered; long prose goes to the storage agent. A dry run shows the decisions without writing.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="space-y-1">
          <span className="label">CSV file</span>
          <input
            type="file"
            accept=".csv,text/csv"
            aria-label="CSV file"
            className="block text-sm"
            onChange={(e) => {
              const f = e.target.files?.[0] ?? null
              setFile(f)
              setPreview(null)
              setStarted(null)
              setColumns({})
              if (f) previewM.mutate({ f, cols: {} })
            }}
          />
        </label>
        <label className="space-y-1">
          <span className="label">Tier for new people</span>
          <select className="input w-36" aria-label="Import tier" value={tier} onChange={(e) => setTier(e.target.value as Tier)}>
            {TIERS.map((t) => (
              <option key={t.value} value={t.value}>
                {t.plural}
              </option>
            ))}
          </select>
        </label>
        <button type="button" className="btn-secondary" disabled={!file || !preview || mergeM.isPending} onClick={() => mergeM.mutate(true)}>
          Dry run
        </button>
        <button type="button" className="btn-primary" disabled={!file || !preview || mergeM.isPending} onClick={() => mergeM.mutate(false)}>
          {mergeM.isPending ? 'Starting…' : `Merge ${preview?.rows ?? ''} rows`}
        </button>
      </div>
      <ErrorNote error={previewM.error ?? mergeM.error} />
      {started && (
        <p className="rounded-md bg-brand-50 px-3 py-2 text-sm text-brand-800" data-testid="import-result">
          {started.dry_run ? 'Dry run of' : 'Merging'} {started.file}: {started.rows} rows as {started.items} {started.items === 1 ? 'record' : 'records'}
          {started.dry_run && ', nothing is written'}
          {started.skipped > 0 && `, ${started.skipped} skipped (no name)`}
          {started.new_properties.length > 0 && ` · new properties: ${started.new_properties.join(', ')}`} ·{' '}
          <Link to={`/assistants/${started.run_id}`} className="underline">
            follow run #{started.run_id}
          </Link>
        </p>
      )}
      {preview && !started && (
        <div className="grid gap-3 md:grid-cols-2">
          <div className="card max-h-60 overflow-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>CSV header</th>
                  <th>Property</th>
                  <th>Plan</th>
                </tr>
              </thead>
              <tbody>
                {preview.columns.map((c) => (
                  <tr key={c.header}>
                    <td>{c.header}</td>
                    <td>
                      <select
                        className="input w-44 font-mono text-xs"
                        aria-label={`Property for ${c.header}`}
                        value={c.role === 'skip' ? 'skip' : c.role === 'source_id' ? 'id' : c.key}
                        onChange={(e) => remap(c.header, e.target.value)}
                      >
                        {c.role === 'field' && (c.new || !properties.data?.some((p) => p.key === c.key)) && <option value={c.key}>{c.key} (new)</option>}
                        <option value="id">ID (match key)</option>
                        <option value="skip">skip</option>
                        {(properties.data ?? []).map((p) => (
                          <option key={p.key} value={p.key}>
                            {p.key}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <PlanBadge column={c} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="card max-h-60 overflow-auto" data-testid="import-matches">
            <p className="px-2 pt-1 text-xs text-slate-500">
              {preview.rows} rows, {preview.groups} people{preview.skipped > 0 && `, ${preview.skipped} without a name`}. The first{' '}
              {preview.matches.length} rows against the database now:
            </p>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Row</th>
                  <th>Name</th>
                  <th>Decision</th>
                </tr>
              </thead>
              <tbody>
                {preview.matches.map((m) => (
                  <tr key={m.row}>
                    <td className="text-xs text-slate-500">{m.row}</td>
                    <td>{m.name}</td>
                    <td>
                      <MatchBadge match={m} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

function PlanBadge({ column: c }: { column: ImportColumn }) {
  if (c.role === 'skip') return <span className="badge bg-slate-100 text-slate-400">skipped</span>
  if (c.role === 'source_id' || c.plan === 'identifier') return <span className="badge bg-slate-100 text-slate-600">identifier</span>
  if (c.new || c.plan === 'new') return <span className="badge bg-amber-50 text-amber-700">new property</span>
  if (c.plan === 'agent') return <span className="badge bg-violet-50 text-violet-700">storage agent</span>
  return <span className="badge bg-emerald-50 text-emerald-700">as entered</span>
}

function MatchBadge({ match: m }: { match: ImportMatch }) {
  if (m.decision === 'matched') {
    return (
      <span className="badge bg-emerald-50 text-emerald-700" title={m.person_id ?? undefined}>
        matched by {m.tier === 'identifier' ? 'identifier' : m.tier === 'name' ? 'name' : 'name and context'}
      </span>
    )
  }
  if (m.decision === 'ambiguous') {
    return (
      <span className="badge bg-amber-50 text-amber-700" title={m.candidates.join(', ')}>
        ambiguous: {m.candidates.length} people
      </span>
    )
  }
  return <span className="badge bg-slate-100 text-slate-600">new person</span>
}
