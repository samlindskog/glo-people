import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { ErrorNote } from '../components/Layout'
import { describeSearch, encodeSearch, formatDate } from '../lib'

const PAGE = 50

export default function SearchesPanel() {
  const { can } = useAuth()
  const [everyone, setEveryone] = useState(false)
  const [offset, setOffset] = useState(0)
  const q = useQuery({
    queryKey: ['searches', everyone, offset],
    queryFn: () => api.searches({ all: everyone, limit: PAGE, offset }),
  })

  return (
    <div>
      {can('searches:read_all') && (
        <label className="mb-3 flex items-center justify-end gap-2 text-sm">
          <input
            type="checkbox"
            checked={everyone}
            onChange={(e) => {
              setEveryone(e.target.checked)
              setOffset(0)
            }}
          />
          Everyone’s searches
        </label>
      )}
      <ErrorNote error={q.error} />
      <div className="card overflow-x-auto">
        <table className="data-table" data-testid="searches-table">
          <thead>
            <tr>
              <th>When</th>
              {everyone && <th>User</th>}
              <th>Via</th>
              <th>Search</th>
              <th className="text-right">Results</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {q.data?.items.map((s) => (
              <tr key={s.id}>
                <td className="whitespace-nowrap text-slate-500">{formatDate(s.created_at)}</td>
                {everyone && <td>{s.username}</td>}
                <td>
                  <span className="badge bg-slate-100 uppercase text-slate-600">{s.source}</span>
                </td>
                <td>
                  {s.params.ask && (
                    <span className="badge mr-1 bg-violet-50 text-violet-700" title={s.params.ask}>
                      ask
                    </span>
                  )}
                  {describeSearch(s.params)}
                </td>
                <td className="text-right tabular-nums">{s.result_count.toLocaleString()}</td>
                <td>
                  <Link className="btn-ghost" to={`/people?s=${encodeSearch({ ...s.params, page: 1 })}`}>
                    Re-run
                  </Link>
                </td>
              </tr>
            ))}
            {q.data?.items.length === 0 && (
              <tr>
                <td colSpan={6} className="py-8 text-center text-slate-500">
                  No searches yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <Pager offset={offset} size={PAGE} hasMore={!!q.data?.has_more} onChange={setOffset} />
    </div>
  )
}

export function Pager({ offset, size, hasMore, onChange }: { offset: number; size: number; hasMore: boolean; onChange: (o: number) => void }) {
  if (offset === 0 && !hasMore) return null
  return (
    <div className="mt-3 flex gap-2">
      <button className="btn-secondary" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - size))}>
        Newer
      </button>
      <button className="btn-secondary" disabled={!hasMore} onClick={() => onChange(offset + size)}>
        Older
      </button>
    </div>
  )
}
