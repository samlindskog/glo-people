import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api'
import { ErrorNote, PageHeader } from '../components/Layout'
import type { ImportPreview, ImportResult, Tier } from '../types'

export default function ImportPage() {
  const qc = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [tier, setTier] = useState<Tier>('member')
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [result, setResult] = useState<ImportResult | null>(null)

  const previewM = useMutation({ mutationFn: (f: File) => api.importPreview(f), onSuccess: setPreview })
  const importM = useMutation({
    mutationFn: () => api.importCsv(file!, tier),
    onSuccess: (r) => {
      setResult(r)
      qc.invalidateQueries({ queryKey: ['fields'] })
      qc.invalidateQueries({ queryKey: ['search'] })
    },
  })
  const jobs = useQuery({
    queryKey: ['jobs'],
    queryFn: api.jobs,
    enabled: !!result,
    refetchInterval: (q) => (q.state.data?.find((j) => j.id === result?.job_id)?.status === 'running' ? 1500 : false),
  })
  const job = jobs.data?.find((j) => j.id === result?.job_id)

  return (
    <div className="max-w-4xl space-y-4">
      <PageHeader title="Import CSV" />
      <div className="card space-y-4 p-4">
        <p className="text-sm text-slate-600">
          Any columns are accepted. Each header becomes a field key; new keys are added to the field registry. Rows are
          matched on the <code>ID</code> column if present (otherwise name + email), so re-importing the same file updates
          people instead of duplicating them. Empty cells are skipped.
        </p>
        <div className="flex flex-wrap items-end gap-4">
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
                setResult(null)
                if (f) previewM.mutate(f)
              }}
            />
          </label>
          <label className="space-y-1">
            <span className="label">Tier</span>
            <select className="input w-40" aria-label="Import tier" value={tier} onChange={(e) => setTier(e.target.value as Tier)}>
              <option value="member">Members</option>
              <option value="plus_one">Plus ones</option>
            </select>
          </label>
          <button className="btn-primary" disabled={!file || !preview || importM.isPending} onClick={() => importM.mutate()}>
            {importM.isPending ? 'Importing…' : `Import ${preview?.rows ?? ''} rows`}
          </button>
        </div>
        <ErrorNote error={previewM.error ?? importM.error} />
      </div>

      {result && (
        <div className="card p-4 text-sm" data-testid="import-result">
          <p className="font-medium">
            Imported {result.file}: {result.inserted} new, {result.updated} updated, {result.skipped} skipped (no name).
          </p>
          {result.new_fields.length > 0 && <p className="mt-1">New fields: {result.new_fields.join(', ')}</p>}
          <p className="mt-1 text-slate-500">
            Embeddings:{' '}
            {job ? (job.status === 'running' ? `computing… ${job.progress} people` : job.status === 'done' ? `done (${String(job.result)} vectors)` : `failed: ${job.error}`) : '…'}
          </p>
        </div>
      )}

      {preview && !result && (
        <div className="card overflow-x-auto">
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
                      <span className="badge bg-amber-50 text-amber-700">new field</span>
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
