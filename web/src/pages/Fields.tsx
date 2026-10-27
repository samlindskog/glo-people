import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api'
import { useAuth } from '../auth'
import { ErrorNote, PageHeader } from '../components/Layout'

export default function FieldsPage() {
  const { can } = useAuth()
  const qc = useQueryClient()
  const fields = useQuery({ queryKey: ['fields'], queryFn: api.fields })
  const toggle = useMutation({
    mutationFn: ({ key, embed }: { key: string; embed: boolean }) => api.updateField(key, { embed }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['fields'] }),
  })
  const total = Math.max(1, ...(fields.data ?? []).map((f) => f.count))

  return (
    <div>
      <PageHeader title="Fields" />
      <p className="mb-4 max-w-3xl text-sm text-slate-600">
        Every key that appears on any person. Embedded fields (◆) get one vector per person and can be used for semantic
        search. Turning embedding on computes vectors for that field in the background; turning it off deletes them.
      </p>
      <ErrorNote error={fields.error ?? toggle.error} />
      <div className="card overflow-x-auto">
        <table className="data-table" data-testid="fields-table">
          <thead>
            <tr>
              <th>Label</th>
              <th>Key</th>
              <th>Type</th>
              <th>Coverage</th>
              <th>Embedded</th>
            </tr>
          </thead>
          <tbody>
            {fields.data?.map((f) => (
              <tr key={f.key}>
                <td className="font-medium">{f.label}</td>
                <td className="font-mono text-xs">{f.key}</td>
                <td>{f.type}</td>
                <td className="w-56">
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 flex-1 rounded bg-slate-100">
                      <div className="h-1.5 rounded bg-brand-500" style={{ width: `${(100 * f.count) / total}%` }} />
                    </div>
                    <span className="w-14 text-right tabular-nums text-xs text-slate-500">{f.count.toLocaleString()}</span>
                  </div>
                </td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Embed ${f.key}`}
                    checked={f.embed}
                    disabled={!can('people:write') || toggle.isPending}
                    onChange={(e) => toggle.mutate({ key: f.key, embed: e.target.checked })}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
