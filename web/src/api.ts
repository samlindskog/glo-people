import type {
  ActivityItem,
  ApiKey,
  FieldInfo,
  ImportPreview,
  ImportResult,
  Job,
  Me,
  Page,
  Person,
  SearchRecord,
  SearchRequest,
  SearchResponse,
  Tier,
  User,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: 'same-origin', headers: {} }
  if (body instanceof FormData) {
    init.body = body
  } else if (body !== undefined) {
    init.body = JSON.stringify(body)
    ;(init.headers as Record<string, string>)['Content-Type'] = 'application/json'
  }
  const res = await fetch(path, init)
  if (!res.ok) {
    let message = res.statusText
    try {
      const data = await res.json()
      message = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail ?? data)
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, message)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

const qs = (params: Record<string, string | number | boolean | undefined | null>) => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v))
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const api = {
  me: () => request<Me>('GET', '/api/auth/me'),
  login: (username: string, password: string) =>
    request<Me>('POST', '/api/auth/login', { username, password }),
  logout: () => request<void>('POST', '/api/auth/logout'),

  search: (req: SearchRequest) => request<SearchResponse>('POST', '/api/search', req),
  person: (id: string) => request<Person>('GET', `/api/people/${id}`),
  createPerson: (tier: Tier, fields: Record<string, unknown>) =>
    request<Person>('POST', '/api/people', { tier, fields }),
  updatePerson: (id: string, fields: Record<string, unknown>, tier?: Tier) =>
    request<Person>('PATCH', `/api/people/${id}`, { fields, tier }),
  deletePerson: (id: string) => request<void>('DELETE', `/api/people/${id}`),
  importPreview: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<ImportPreview>('POST', '/api/people/import/preview', fd)
  },
  importCsv: (file: File, tier: Tier) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('tier', tier)
    return request<ImportResult>('POST', '/api/people/import', fd)
  },
  jobs: () => request<Job[]>('GET', '/api/jobs'),

  fields: () => request<FieldInfo[]>('GET', '/api/fields'),
  updateField: (key: string, changes: { label?: string; embed?: boolean }) =>
    request<FieldInfo>('PATCH', `/api/fields/${key}`, changes),

  searches: (params: { all?: boolean; user_id?: number; limit?: number; offset?: number }) =>
    request<Page<SearchRecord>>('GET', `/api/searches${qs(params)}`),
  activity: (params: {
    user_id?: number
    action?: string
    since?: string
    until?: string
    limit?: number
    offset?: number
  }) => request<Page<ActivityItem>>('GET', `/api/activity${qs(params)}`),

  users: () => request<User[]>('GET', '/api/users'),
  createUser: (username: string, password: string, role: string) =>
    request<User>('POST', '/api/users', { username, password, role }),
  updateUser: (id: number, changes: { role?: string; is_active?: boolean }) =>
    request<User>('PATCH', `/api/users/${id}`, changes),
  setPassword: (id: number, password: string) =>
    request<void>('POST', `/api/users/${id}/password`, { password }),

  myKeys: () => request<ApiKey[]>('GET', '/api/api-keys'),
  issueMyKey: (name: string) => request<ApiKey>('POST', '/api/api-keys', { name }),
  revokeMyKey: (id: number) => request<void>('DELETE', `/api/api-keys/${id}`),
  userKeys: (userId: number) => request<ApiKey[]>('GET', `/api/users/${userId}/api-keys`),
  issueUserKey: (userId: number, name: string) =>
    request<ApiKey>('POST', `/api/users/${userId}/api-keys`, { name }),
  revokeUserKey: (userId: number, id: number) =>
    request<void>('DELETE', `/api/users/${userId}/api-keys/${id}`),
}
