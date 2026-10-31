import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { api } from '../api'
import { useAuth } from '../auth'
import { roleCanGrant, roleLabel, roleNames } from '../roles'
import KeyManager, { McpSnippet } from '../components/KeyManager'
import { ErrorNote, PageHeader } from '../components/Layout'
import { formatDate } from '../lib'
import type { User } from '../types'

export default function AccountsPage() {
  const { me, can } = useAuth()
  const qc = useQueryClient()
  const users = useQuery({ queryKey: ['users'], queryFn: api.users })
  const [selected, setSelected] = useState<User | null>(null)
  const grantable = roleNames.filter((r) => roleCanGrant(me?.permissions ?? [], r))

  const update = useMutation({
    mutationFn: ({ id, changes }: { id: number; changes: { role?: string; is_active?: boolean } }) => api.updateUser(id, changes),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['users'] }),
  })

  return (
    <div className="space-y-6">
      <PageHeader title="Accounts" />
      <CreateUser roles={grantable} onCreated={() => qc.invalidateQueries({ queryKey: ['users'] })} />
      <ErrorNote error={users.error ?? update.error} />
      <div className="card overflow-x-auto">
        <table className="data-table" data-testid="users-table">
          <thead>
            <tr>
              <th>Username</th>
              <th>Role</th>
              <th>Status</th>
              <th>Created</th>
              <th>Last activity</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {users.data?.map((u) => {
              const self = u.id === me?.id
              const manageable = !self && roleCanGrant(me?.permissions ?? [], u.role)
              return (
                <tr key={u.id} className={u.is_active ? '' : 'text-slate-400'}>
                  <td className="font-medium">
                    {u.username}
                    {self && <span className="ml-1 text-xs text-slate-400">(you)</span>}
                  </td>
                  <td>
                    <select
                      className="input w-32"
                      aria-label={`Role for ${u.username}`}
                      value={u.role}
                      disabled={!manageable}
                      onChange={(e) => update.mutate({ id: u.id, changes: { role: e.target.value } })}
                    >
                      {roleNames.map((r) => (
                        <option key={r} value={r} disabled={!grantable.includes(r)}>
                          {roleLabel(r)}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>{u.is_active ? 'Active' : 'Deactivated'}</td>
                  <td className="whitespace-nowrap">{formatDate(u.created_at)}</td>
                  <td className="whitespace-nowrap">{formatDate(u.last_seen)}</td>
                  <td className="space-x-1 whitespace-nowrap">
                    {manageable && (
                      <>
                        <button className="btn-secondary" onClick={() => update.mutate({ id: u.id, changes: { is_active: !u.is_active } })}>
                          {u.is_active ? 'Deactivate' : 'Reactivate'}
                        </button>
                        <ResetPassword user={u} />
                      </>
                    )}
                    {can('api_keys:manage_all') && (
                      <button className="btn-ghost" onClick={() => setSelected(selected?.id === u.id ? null : u)}>
                        API keys
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {selected && (
        <section className="space-y-3">
          <h2 className="text-base font-semibold">API keys for {selected.username}</h2>
          <KeyManager
            key={selected.id}
            queryKey={['user-keys', selected.id]}
            list={() => api.userKeys(selected.id)}
            issue={(name) => api.issueUserKey(selected.id, name)}
            revoke={(id) => api.revokeUserKey(selected.id, id)}
          />
        </section>
      )}

      {can('api_keys:manage_own') && !selected && (
        <section className="space-y-3" data-testid="my-keys">
          <h2 className="text-base font-semibold">My API keys</h2>
          <p className="text-sm text-slate-600">
            API keys let MCP clients and scripts use the database as you, with your role’s permissions. Keys can’t manage
            accounts or issue other keys.
          </p>
          <KeyManager queryKey={['my-keys']} list={api.myKeys} issue={api.issueMyKey} revoke={api.revokeMyKey} />
          <div className="card p-4">
            <h3 className="text-sm font-semibold">Connecting an MCP client</h3>
            <McpSnippet />
          </div>
        </section>
      )}
    </div>
  )
}

function CreateUser({ roles, onCreated }: { roles: string[]; onCreated: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState(roles[0] ?? 'reader')
  const m = useMutation({
    mutationFn: () => api.createUser(username.trim().toLowerCase(), password, role),
    onSuccess: () => {
      setUsername('')
      setPassword('')
      onCreated()
    },
  })
  function submit(e: FormEvent) {
    e.preventDefault()
    m.mutate()
  }
  return (
    <form onSubmit={submit} className="card flex flex-wrap items-end gap-3 p-4" aria-label="Create account">
      <label className="space-y-1">
        <span className="label">Username</span>
        <input className="input w-48" aria-label="New username" value={username} onChange={(e) => setUsername(e.target.value)} required />
      </label>
      <label className="space-y-1">
        <span className="label">Initial password</span>
        <input className="input w-56" type="password" aria-label="New password" minLength={10} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
      </label>
      <label className="space-y-1">
        <span className="label">Role</span>
        <select className="input w-36" aria-label="New role" value={role} onChange={(e) => setRole(e.target.value)}>
          {roles.map((r) => (
            <option key={r} value={r}>
              {roleLabel(r)}
            </option>
          ))}
        </select>
      </label>
      <button className="btn-primary" disabled={m.isPending}>
        Create account
      </button>
      {m.error && <p className="w-full text-sm text-red-700">{m.error.message}</p>}
    </form>
  )
}

function ResetPassword({ user }: { user: User }) {
  const [msg, setMsg] = useState<string | null>(null)
  return (
    <>
      <button
        className="btn-secondary"
        onClick={async () => {
          const pw = prompt(`New password for ${user.username} (min 10 characters)`)
          if (!pw) return
          try {
            await api.setPassword(user.id, pw)
            setMsg('Password reset')
          } catch (e) {
            setMsg(e instanceof Error ? e.message : 'Failed')
          }
        }}
      >
        Reset password
      </button>
      {msg && <span className="ml-1 text-xs text-slate-500">{msg}</span>}
    </>
  )
}
