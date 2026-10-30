import { decodeSearch, DEFAULT_SEARCH, describeSearch, encodeSearch } from './lib'
import { roleCanGrant } from './roles'
import type { Permission, SearchRequest } from './types'

test('search state round-trips through the URL, including unicode', () => {
  const req: SearchRequest = {
    ...DEFAULT_SEARCH,
    tier: 'member',
    filters: [{ field: 'name', op: 'contains', value: 'Zoë' }],
    semantic: { query: 'fintech → Lagos', fields: ['bio'], mode: 'max', threshold: 0.3, require_all_fields: true },
  }
  const s = encodeSearch(req)
  expect(s).not.toMatch(/[+/=]/)
  expect(decodeSearch(s)).toEqual(req)
})

test('bad or missing search state falls back to defaults', () => {
  expect(decodeSearch(null)).toEqual(DEFAULT_SEARCH)
  expect(decodeSearch('%%%')).toEqual(DEFAULT_SEARCH)
})

test('describeSearch summarises filters and semantic options', () => {
  expect(describeSearch(DEFAULT_SEARCH)).toBe('Everyone')
  expect(
    describeSearch({
      filters: [
        { field: 'city', op: 'in', value: ['Lagos', 'Accra'] },
        { field: 'bio', op: 'exists', value: false },
      ],
      semantic: { query: 'q', fields: ['bio', 'one_liner'], mode: 'avg', threshold: 0.2, require_all_fields: false },
    }),
  ).toBe('city in Lagos | Accra · bio is empty · “q” in bio, one_liner (avg ≥ 0.2)')
  expect(
    describeSearch({
      semantic: { query: '', fields: [], queries: [{ field: 'bio', query: 'payments' }, { field: 'role', query: 'CTO' }], mode: 'max', threshold: 0.3, require_all_fields: false },
    }),
  ).toBe('bio: “payments” + role: “CTO” (max ≥ 0.3)')
})

test('roles from the shared contract: readers cannot grant writer', () => {
  const reader: Permission[] = ['people:read', 'searches:read_own', 'searches:read_all', 'activity:read', 'accounts:manage', 'api_keys:manage_own', 'api_keys:manage_all']
  expect(roleCanGrant(reader, 'reader')).toBe(true)
  expect(roleCanGrant(reader, 'writer')).toBe(false)
  expect(roleCanGrant([...reader, 'people:write'], 'writer')).toBe(true)
})
