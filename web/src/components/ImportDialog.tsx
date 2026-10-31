import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api'
import type { ImportPreview, ImportStarted, Tier } from '../types'
import { ErrorNote } from './Layout'

/** Pick a CSV, preview its columns, start the import run. The import is an assistant run of row
 * batches; `onStarted` gets the run so the caller can follow it. */
export default function ImportDialog({ onStarted }: { onStarted?: (started: ImportStarted) => void }) {
  const qc = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [tier, setTier] = useState<Tier>('member')
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [started, setStarted] = useState<ImportStarted | null>(null)

  const previewM = useMutation({ mutationFn: (f: File) => api.importPreview(f), onSuccess: setPreview })
  const importM = useMutation({
    mutationFn: () => api.importCsv(file!, tier),
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
        Any columns are accepted: each header becomes a property (new ones are created). Rows are matched on the <code>ID</code> column if present
        (otherwise name + email), so re-importing updates people instead of duplicating them.
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
              if (f) previewM.mutate(f)
            }}
          />
        </label>
        <label className="space-y-1">
          <span className="label">Tier</span>
          <select className="input w-36" aria-label="Import tier" value={tier} onChange={(e) => setTier(e.target.value as Tier)}>
            <option value="member">Members</option>
            <option value="plus_one">Plus ones</option>
          </select>
        </label>
        <button type="button" className="btn-primary" disabled={!file || !preview || importM.isPending} onClick={() => importM.mutate()}>
          {importM.isPending ? 'Starting…' : `Import ${preview?.rows ?? ''} rows`}
        </button>
      </div>
      <ErrorNote error={previewM.error ?? importM.error} />
      {started && (
        <p className="rounded-md bg-brand-50 px-3 py-2 text-sm text-brand-800" data-testid="import-result">
          Importing {started.file}: {started.rows} rows in {started.items} {started.items === 1 ? 'batch' : 'batches'}
          {started.skipped > 0 && `, ${started.skipped} skipped (no name)`}
          {started.new_properties.length > 0 && ` · new properties: ${started.new_properties.join(', ')}`} ·{' '}
          <Link to={`/assistants/${started.run_id}`} className="underline">
            follow run #{started.run_id}
          </Link>
        </p>
      )}
      {preview && !started && (
        <div className="card max-h-60 overflow-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>CSV header</th>
                <th>Field key</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {preview.columns.map((c) => (
                <tr key={c.header + c.key}>
                  <td>{c.header}</td>
                  <td className="font-mono text-xs">{c.key}</td>
                  <td>
                    {c.role === 'source_id' ? (
                      <span className="badge bg-slate-100 text-slate-600">match key</span>
                    ) : c.new ? (
                      <span className="badge bg-amber-50 text-amber-700">new property</span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
