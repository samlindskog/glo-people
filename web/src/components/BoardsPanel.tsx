import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { CopyLink, kindLabel } from './boards'

/** The person's glojobs.app pages, on their record. */
export default function BoardsPanel({ personId }: { personId: string }) {
  const { can } = useAuth()
  const boards = useQuery({ queryKey: ['boards', personId], queryFn: () => api.boards(personId) })
  if (!boards.data) return null
  return (
    <div className="card mt-4 p-4" data-testid="boards-panel">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold">glojobs.app</h2>
        {can('people:write') && (
          <Link to={`/boards/new?person=${personId}`} className="btn-secondary">
            New board
          </Link>
        )}
      </div>
      {boards.data.length === 0 ? (
        <p className="text-sm text-slate-500">No page yet. A board gives this person a private link with roles, people, or both.</p>
      ) : (
        <ul className="divide-y divide-slate-100 text-sm">
          {boards.data.map((b) => (
            <li key={b.id} className="flex flex-wrap items-center gap-3 py-2">
              <Link to={`/boards/${b.id}`} className="font-medium text-brand-700 hover:underline">
                {b.title || kindLabel(b.kind)}
              </Link>
              <span className="text-slate-500">
                {kindLabel(b.kind)} · opened {b.views}×{!b.enabled && ' · disabled'}
              </span>
              <CopyLink url={b.url} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
