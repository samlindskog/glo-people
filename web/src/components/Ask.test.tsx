import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { vi } from 'vitest'
import type { AskResult } from '../types'
import Ask from './Ask'

const RESULT: AskResult = {
  text: 'Ada Okafor matches: she builds payments infrastructure in Lagos.',
  changes: [],
  citations: [{ person_id: 'a'.repeat(24), field: 'bio', value: 'payments infrastructure' }],
  rationale: 'bio for the topic, mrr for the figure',
  request: {
    tier: null,
    filters: [{ field: 'mrr', op: 'gte', value: 10000 }],
    semantic: { query: '', fields: [], queries: [{ field: 'bio', query: 'payments founder' }], mode: 'avg', threshold: 0.4, require_all_fields: false },
    sort: 'score',
    limit: 12,
    page: 1,
  },
  total: 1,
  results: [{ id: 'a'.repeat(24), tier: 'member', score: 0.8, field_scores: { bio: 0.8 }, person: { id: 'a'.repeat(24), tier: 'member', name: 'Ada Okafor', norm: {}, norm_flags: {} } }],
  search_id: 7,
}

const RUN = { id: 5, workflow: 'ask', status: 'done', total: 1, done: 1, failed: 0, credits_used: 0, params: {}, summary: {} }

vi.mock('../api', () => ({
  api: {
    startRun: vi.fn(async () => RUN),
    run: vi.fn(async () => ({ ...RUN, items: [{ id: 1, status: 'done', result: RESULT, error: null }] })),
  },
}))
vi.mock('../hooks/useEvents', () => ({ useRunEvents: () => ({ events: [], run: null, done: true }) }))

test('asks as a run, shows the answer with citations and the executed search', async () => {
  const { api } = await import('../api')
  const user = userEvent.setup()
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <Ask tier="member" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await user.type(screen.getByLabelText('Question'), 'payments founders with mrr over 10k')
  await user.click(screen.getByRole('button', { name: 'Ask' }))
  expect(await screen.findByTestId('ask-answer')).toHaveTextContent('Ada Okafor matches')
  expect(api.startRun).toHaveBeenCalledWith('ask', { options: { question: 'payments founders with mrr over 10k', tier: 'member', k: 12, mode: 'avg', threshold: 0.4 } })
  const citations = screen.getByRole('list', { name: 'Citations' })
  expect(citations).toHaveTextContent('Ada Okafor')
  expect(citations).toHaveTextContent('bio: “payments infrastructure”')
  expect(screen.getByTestId('ask-plan')).toHaveTextContent('mrr ≥ 10000 · bio: “payments founder” (avg ≥ 0.4) · 1 person')
  expect(screen.getByRole('link', { name: 'run #5' })).toHaveAttribute('href', '/assistants/5')
  expect(screen.getByRole('button', { name: 'Run as search' })).toBeEnabled()
})
