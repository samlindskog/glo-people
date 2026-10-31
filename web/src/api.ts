import type {
  ActivityItem,
  ApiKey,
  AssistantRun,
  AssistantRunDetail,
  AssistantRunItem,
  AssistantSpec,
  Credits,
  FieldFilter,
  ImportPreview,
  ImportStarted,
  Invocation,
  Me,
  Page,
  Person,
  PlanResult,
  Property,
  RunStatus,
  SearchRecord,
  SearchRequest,
  SearchResponse,
  StoreOptions,
  Tier,
  Tuning,
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
    return request<ImportStarted>('POST', '/api/people/import', fd)
  },

  properties: () => request<Property[]>('GET', '/api/properties'),

  // tools on one record: non-blocking; the invocation handle streams events and can be stopped
  plan: (body: { question: string; tier?: Tier | null; filters?: FieldFilter[]; options?: Partial<Tuning> }) =>
    request<PlanResult>('POST', '/api/search/plan', body),
  store: (id: string, passage: string, options?: Partial<StoreOptions>) =>
    request<Invocation>('POST', `/api/people/${id}/store`, { passage, options }),
  storeFile: (id: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<Invocation>('POST', `/api/people/${id}/store/file`, fd)
  },
  normalizePerson: (id: string) => request<Invocation>('POST', `/api/people/${id}/normalize`),
  invocation: (id: string) => request<Invocation>('GET', `/api/invocations/${id}`),
  stopInvocation: (id: string) => request<Invocation>('POST', `/api/invocations/${id}/stop`),
  credits: () => request<Credits | null>('GET', '/api/credits'),

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

  // assistants: runs that persist, pause, resume and stop
  assistants: () => request<AssistantSpec[]>('GET', '/api/assistants'),
  startRun: (
    key: string,
    body: { person_ids?: string[]; all_with_field?: string; tier?: Tier | null; options?: Record<string, unknown>; label?: string },
  ) => request<AssistantRun>('POST', `/api/assistants/${key}/runs`, body),
  runs: (params: { limit?: number; offset?: number; status?: 'active' | RunStatus } = {}) =>
    request<Page<AssistantRun>>('GET', `/api/assistants/runs${qs(params)}`),
  run: (id: number) => request<AssistantRunDetail>('GET', `/api/assistants/runs/${id}`),
  runItems: (id: number, params: { status?: string; after?: number; limit?: number } = {}) =>
    request<AssistantRunItem[]>('GET', `/api/assistants/runs/${id}/items${qs(params)}`),
  pauseRun: (id: number) => request<AssistantRun>('POST', `/api/assistants/runs/${id}/pause`),
  resumeRun: (id: number, options: Record<string, unknown> = {}) =>
    request<AssistantRun>('POST', `/api/assistants/runs/${id}/resume`, { options }),
  stopRun: (id: number) => request<AssistantRun>('POST', `/api/assistants/runs/${id}/stop`),
}
