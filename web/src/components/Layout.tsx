import { NavLink, Outlet, useNavigate } from 'react-router'
import { useAuth } from '../auth'
import { roleLabel } from '../roles'
import type { Permission } from '../types'
import CreditsChip from './CreditsChip'

const NAV: { to: string; label: string; permission: Permission }[] = [
  { to: '/people', label: 'People', permission: 'people:read' },
  { to: '/properties', label: 'Properties', permission: 'people:read' },
  { to: '/assistants', label: 'Assistants', permission: 'people:read' },
  { to: '/history', label: 'History', permission: 'searches:read_own' },
  { to: '/accounts', label: 'Accounts', permission: 'accounts:manage' },
]

export default function Layout() {
  const { me, can, logout } = useAuth()
  const navigate = useNavigate()

  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-2.5">
          <NavLink to="/people" className="text-base font-semibold tracking-tight text-brand-700">
            GLO People
          </NavLink>
          <nav className="flex flex-1 flex-wrap gap-1" aria-label="Main">
            {NAV.filter((n) => can(n.permission)).map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                className={({ isActive }) =>
                  `rounded-md px-2.5 py-1.5 text-sm ${isActive ? 'bg-brand-50 font-medium text-brand-700' : 'text-slate-600 hover:bg-slate-100'}`
                }
              >
                {n.label}
              </NavLink>
            ))}
          </nav>
          <div className="flex items-center gap-3 text-sm">
            <CreditsChip />
            <span className="text-slate-600" data-testid="whoami">
              {me?.username} <span className="badge bg-slate-100 text-slate-600">{roleLabel(me?.role ?? '')}</span>
            </span>
            <button
              className="btn-ghost"
              onClick={async () => {
                await logout()
                navigate('/login')
              }}
            >
              Sign out
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}

export function PageHeader({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
      <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      <div className="flex items-center gap-2">{children}</div>
    </div>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null
  return (
    <div role="alert" className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {error instanceof Error ? error.message : String(error)}
    </div>
  )
}
