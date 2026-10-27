import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router'
import { api, ApiError } from './api'
import type { Me, Permission } from './types'

interface AuthState {
  me: Me | null
  loading: boolean
  can: (p: Permission) => boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      try {
        return await api.me()
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null
        throw e
      }
    },
    staleTime: 60_000,
  })
  const me = data ?? null

  const can = useCallback((p: Permission) => !!me?.permissions.includes(p), [me])
  const login = useCallback(
    async (username: string, password: string) => {
      const who = await api.login(username, password)
      // Drop everything cached for the previous user but keep the live 'me' query observed.
      qc.removeQueries({ predicate: (q) => q.queryKey[0] !== 'me' })
      qc.setQueryData(['me'], who)
    },
    [qc],
  )
  const logout = useCallback(async () => {
    await api.logout()
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== 'me' })
    qc.setQueryData(['me'], null)
  }, [qc])

  const value = useMemo(() => ({ me, loading: isLoading, can, login, logout }), [me, isLoading, can, login, logout])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth outside AuthProvider')
  return ctx
}

export function RequireAuth({ children, permission }: { children: ReactNode; permission?: Permission }) {
  const { me, loading, can } = useAuth()
  const location = useLocation()
  if (loading) return <div className="p-8 text-sm text-slate-500">Loading…</div>
  if (!me) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  if (permission && !can(permission)) return <Forbidden permission={permission} />
  return <>{children}</>
}

export function Forbidden({ permission }: { permission: Permission }) {
  return (
    <div className="mx-auto max-w-lg p-10 text-center">
      <h1 className="text-lg font-semibold">Not allowed</h1>
      <p className="mt-2 text-sm text-slate-600">
        This page needs the <code className="rounded bg-slate-100 px-1">{permission}</code> permission, which your role
        doesn’t have.
      </p>
    </div>
  )
}
