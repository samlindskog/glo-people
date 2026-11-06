import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api } from '../api'
import { useAuth } from '../auth'
import { BoardsNav, CopyLink, kindLabel } from '../components/boards'
import { ErrorNote, PageHeader } from '../components/Layout'
import { formatDate } from '../lib'

export default function BoardsPage() {
  const { can } = useAuth()
  const qc = useQueryClient()
  const boards = useQuery({ queryKey: ['boards'], queryFn: () => api.boards() })
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => api.updateBoard(id, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['boards'] }),
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteBoard(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['boards'] }),
  })

  return (
    <div>
      <PageHeader title="Boards">
        {can('people:write') && (
          <Link to="/boards/new" className="btn-primary">
            New board
          </Link>
        )}
      </PageHeader>
      <BoardsNav />
      <p className="mb-4 text-sm text-slate-600">
        A board is one person's page on glojobs.app: open roles, people, or both, reachable only by its link. Jobs and people come from the shared catalogues.
      </p>
      <ErrorNote error={boards.error ?? toggle.error ?? remove.error} />
      {boards.data && boards.data.length === 0 && <p className="text-sm text-slate-500">No boards yet.</p>}
      {boards.data && boards.data.length > 0 && (
        <div className="card overflow-x-auto">
          <table className="data-table" data-testid="boards-table">
            <thead>
              <tr>
                <th>Person</th>
                <th>Kind</th>
                <th>Title</th>
                <th>Items</th>
                <th>Opened</th>
                <th>Link</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {boards.data.map((b) => (
                <tr key={b.id} className={b.enabled ? '' : 'opacity-60'}>
                  <td>
                    <Link to={`/people/${b.person_id}`} className="text-brand-700 hover:underline">
                      {b.person_name || b.person_id}
                    </Link>
                  </td>
                  <td>{kindLabel(b.kind)}</td>
                  <td>
                    <Link to={`/boards/${b.id}`} className="hover:underline">
                      {b.title || <span className="text-slate-500">(default)</span>}
                    </Link>
                  </td>
                  <td className="whitespace-nowrap text-slate-600">
                    {b.kind !== 'talent' && `${b.job_count} job${b.job_count === 1 ? '' : 's'}`}
                    {b.kind === 'both' && ', '}
                    {b.kind !== 'jobs' && `${b.talent_count} ${b.talent_count === 1 ? 'person' : 'people'}`}
                  </td>
                  <td className="whitespace-nowrap text-slate-600">{b.views ? `${b.views}× · ${formatDate(b.last_viewed_at)}` : 'never'}</td>
                  <td>
                    <CopyLink url={b.url} />
                  </td>
                  <td className="whitespace-nowrap">
                    {can('people:write') && (
                      <>
                        <button className="btn-ghost" onClick={() => toggle.mutate({ id: b.id, enabled: !b.enabled })}>
                          {b.enabled ? 'Disable' : 'Enable'}
                        </button>
                        <button
                          className="btn-ghost text-red-700"
                          onClick={() => {
                            if (confirm(`Delete the board for ${b.person_name}? Its link stops working.`)) remove.mutate(b.id)
                          }}
                        >
                          Delete
                        </button>
                      </>
                    )}
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
