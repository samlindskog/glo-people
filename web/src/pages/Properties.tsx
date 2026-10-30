import { useMutation, useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { ErrorNote, PageHeader } from '../components/Layout'
import type { Property, WorkflowRun } from '../types'

const SHOWN_VALUES = 6

export default function PropertiesPage() {
  const { can } = useAuth()
  const props = useQuery({ queryKey: ['properties'], queryFn: api.properties })
  const total = Math.max(1, ...(props.data ?? []).map((p) => p.count))
  const normalise = useMutation({
    mutationFn: (key: string) => api.startRun('normalize', { all_with_field: key }),
  })
  const started: WorkflowRun | undefined = normalise.data

  return (
    <div>
      <PageHeader title="Properties">
        {can('people:write') && (
          <button className="btn-secondary" disabled={normalise.isPending} onClick={() => normalise.mutate('name')} title="Run the normalise workflow for everyone">
            Normalise all
          </button>
        )}
      </PageHeader>
      <p className="mb-4 max-w-3xl text-sm text-slate-600">
        Every property a person can have. Properties come into existence through CSV imports and the storage agent;
        their type and canonical values are defined in the source code. Text and list properties (◆) are embedded for
        semantic search; typed properties (number, date, boolean, enum) get a normalised value for filters.
      </p>
      <ErrorNote error={props.error ?? normalise.error} />
      {started && (
        <p className="mb-3 rounded-md bg-brand-50 px-3 py-2 text-sm text-brand-800" data-testid="normalise-started">
          Normalise run #{started.id} started for {started.total} {started.total === 1 ? 'person' : 'people'} ·{' '}
          <Link to="/workflows" className="underline">
            follow it on Workflows
          </Link>
        </p>
      )}
      <div className="card overflow-x-auto">
        <table className="data-table" data-testid="properties-table">
          <thead>
            <tr>
              <th>Label</th>
              <th>Key</th>
              <th>Type</th>
              <th>Values</th>
              <th>Coverage</th>
              <th>Not normalised</th>
              <th>Origin</th>
              {can('people:write') && <th />}
            </tr>
          </thead>
          <tbody>
            {props.data?.map((p) => (
              <tr key={p.key}>
                <td className="font-medium">
                  {p.label}
                  {p.embeddable && (
                    <span className="ml-1 text-xs text-brand-500" title="Embedded for semantic search">
                      ◆
                    </span>
                  )}
                </td>
                <td className="font-mono text-xs">{p.key}</td>
                <td>
                  <TypeBadge type={p.type} normalisable={p.normalisable} />
                </td>
                <td className="max-w-md text-xs text-slate-600">
                  <Values values={p.values} />
                </td>
                <td className="w-56">
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 flex-1 rounded bg-slate-100">
                      <div className="h-1.5 rounded bg-brand-500" style={{ width: `${(100 * p.count) / total}%` }} />
                    </div>
                    <span className="w-14 text-right tabular-nums text-xs text-slate-500">{p.count.toLocaleString()}</span>
                  </div>
                </td>
                <td className="text-xs text-slate-500">
                  {p.normalisable && (p.unmapped > 0 || p.non_normalizable > 0) ? (
                    <span title={`${p.unmapped} not recognised · ${p.non_normalizable} say there is no value`}>
                      {p.unmapped > 0 && <span className="text-amber-700">{p.unmapped} unmapped</span>}
                      {p.unmapped > 0 && p.non_normalizable > 0 && ' · '}
                      {p.non_normalizable > 0 && <span>{p.non_normalizable} n/a</span>}
                    </span>
                  ) : (
                    <span className="text-slate-300">—</span>
                  )}
                </td>
                <td className="text-xs text-slate-500">{ORIGIN[p.source]}</td>
                {can('people:write') && (
                  <td>
                    {p.normalisable && (
                      <button className="btn-ghost" aria-label={`Normalise ${p.key}`} disabled={normalise.isPending || p.count === 0} onClick={() => normalise.mutate(p.key)} title={`Re-parse ${p.key} for everyone who has it; an agent decides the unmapped values`}>
                        Normalise
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const ORIGIN: Record<Property['source'], string> = { spec: 'source code', import: 'CSV import', agent: 'agent' }

function TypeBadge({ type, normalisable }: { type: Property['type']; normalisable: boolean }) {
  return (
    <span className={`badge ${normalisable ? 'bg-sky-50 text-sky-700' : 'bg-slate-100 text-slate-600'}`} title={normalisable ? 'Normalised for filters' : undefined}>
      {type.replace('_', ' ')}
    </span>
  )
}

function Values({ values }: { values: string[] }) {
  if (values.length === 0) return <span className="text-slate-300">—</span>
  const shown = values.slice(0, SHOWN_VALUES)
  return (
    <span title={values.join(' · ')}>
      {shown.join(' · ')}
      {values.length > shown.length && <span className="text-slate-400"> +{values.length - shown.length}</span>}
    </span>
  )
}
