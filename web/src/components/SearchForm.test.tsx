import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DEFAULT_SEARCH } from '../lib'
import type { Property, SearchRequest } from '../types'
import SearchForm from './SearchForm'

const prop = (p: Partial<Property> & Pick<Property, 'key' | 'label' | 'type' | 'embeddable' | 'count'>): Property => ({
  values: [],
  normalisable: false,
  filter_ops: ['eq', 'ne', 'contains', 'starts_with', 'regex', 'in', 'exists'],
  source: 'spec',
  created_at: null,
  unmapped: 0,
  non_normalizable: 0,
  ...p,
})
const PROPERTIES: Property[] = [
  prop({ key: 'name', label: 'Name', type: 'keyword', embeddable: false, count: 10 }),
  prop({ key: 'bio', label: 'Bio', type: 'text', embeddable: true, count: 8 }),
  prop({ key: 'mrr', label: 'MRR', type: 'number', embeddable: false, normalisable: true, count: 3 }),
]

test('builds literal filters and a semantic query', async () => {
  const user = userEvent.setup()
  let got: SearchRequest | null = null
  render(<SearchForm initial={DEFAULT_SEARCH} properties={PROPERTIES} onSearch={(r) => (got = r)} />)

  await user.click(screen.getByRole('button', { name: '+ Add filter' }))
  await user.selectOptions(screen.getByLabelText('Filter field'), 'mrr')
  await user.selectOptions(screen.getByLabelText('Filter operator'), 'gte')
  await user.type(screen.getByLabelText('Filter value'), '1000')

  await user.click(screen.getByLabelText('Semantic search'))
  await user.type(screen.getByLabelText('Semantic query'), 'climate founder')
  await user.click(screen.getByRole('button', { name: /^Bio/ }))
  await user.click(screen.getByRole('radio', { name: 'Best field' }))
  const threshold = screen.getByLabelText('Threshold')
  await user.clear(threshold)
  await user.type(threshold, '0.35')
  await user.click(screen.getByRole('button', { name: 'Search' }))

  expect(got).toEqual({
    tier: null,
    filters: [{ field: 'mrr', op: 'gte', value: 1000 }],
    semantic: { query: 'climate founder', fields: ['bio'], mode: 'max', threshold: 0.35, require_all_fields: false },
    sort: 'score',
    limit: 25,
    page: 1,
  })
})

test('only embeddable properties are offered for semantic search', async () => {
  const user = userEvent.setup()
  render(<SearchForm initial={DEFAULT_SEARCH} properties={PROPERTIES} onSearch={() => {}} />)
  await user.click(screen.getByLabelText('Semantic search'))
  const group = screen.getByRole('group', { name: 'Semantic fields' })
  expect(group).toHaveTextContent('Bio')
  expect(group).not.toHaveTextContent('MRR')
  expect(group).not.toHaveTextContent('Name')
})
