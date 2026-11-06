import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { CopyLink, kindLabel } from './boards'

/** The person on glojobs.app: their own pages, and the talent entry that lists them with every
 * board showing it. */
export default function BoardsPanel({ personId }: { personId: string }) {
  const { can } = useAuth()
  const presence = useQuery({ queryKey: ['board-presence', personId], queryFn: () => api.boardPresence(personId) })
  if (!presence.data) return null
  const { boards, listed_on: listed } = presence.data
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
      {boards.length === 0 ? (
        <p className="text-sm text-slate-500">No page yet. A board gives this person a private link with roles, people, or both.</p>
      ) : (
        <ul className="divide-y divide-slate-100 text-sm">
          {boards.map((b) => (
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
      {listed.map((t) => (
        <div key={t.slug} className="mt-3 border-t border-slate-100 pt-3 text-sm" data-testid="listed-on">
          <span className="text-slate-500">Listed as talent</span>{' '}
          <Link to="/boards/talent" className="font-medium text-brand-700 hover:underline">
            {t.name}
          </Link>
          {t.status === 'hidden' && <span className="text-slate-500"> · hidden</span>}
          {t.boards.length === 0 ? (
            <span className="text-slate-500"> · on no board yet</span>
          ) : (
            <ul className="mt-1 space-y-1">
              {t.boards.map((b) => (
                <li key={b.id} className="flex flex-wrap items-center gap-3">
                  <Link to={`/boards/${b.id}`} className="text-brand-700 hover:underline">
                    {b.person_name}'s {kindLabel(b.kind).toLowerCase()}
                  </Link>
                  <CopyLink url={b.url} />
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  )
}
