import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import type { AssistantRun, AssistantRunItem } from '../types'
import { RunDetail, RunsTable } from './Assistants'

const RUN: AssistantRun = {
  id: 7,
  workflow: 'enrich',
  status: 'running',
  params: { options: { mode: 'frugal', refresh: false, max_credits: 1500 } },
  user_id: 1,
  username: 'wendy',
  created_at: '2026-10-29T12:00:00+00:00',
  started_at: '2026-10-29T12:00:01+00:00',
  finished_at: null,
  total: 3,
  done: 1,
  failed: 0,
  credits_used: 12,
  summary: {},
  error: null,
}

const ITEMS: AssistantRunItem[] = [
  {
    id: 1,
    run_id: 7,
    workflow: 'enrich',
    person_id: 'aaaaaaaaaaaaaaaaaaaaaaaa',
    status: 'done',
    result: {
      text: 'LinkedIn profile matched; wrote github, company_description.',
      changes: [
        { key: 'github', action: 'set', value: 'https://github.com/adaok', previous: null, flag: null },
        { key: 'company_description', action: 'set', value: 'PayRail builds payments rails.', previous: null, flag: null },
      ],
      skipped: [{ step: 'sites', reason: 'no personal website known' }],
    },
    error: null,
    credits: 12,
    trace_id: '0123456789abcdef0123456789abcdef',
    logfire_url: 'https://logfire-us.pydantic.dev/org/proj?q=trace_id',
    started_at: '2026-10-29T12:00:01+00:00',
    finished_at: '2026-10-29T12:00:09+00:00',
  },
  {
    id: 2,
    run_id: 7,
    workflow: 'enrich',
    person_id: 'bbbbbbbbbbbbbbbbbbbbbbbb',
    status: 'failed',
    result: {},
    error: 'FIRECRAWL_API_KEY is not set',
    credits: 0,
    trace_id: null,
    logfire_url: null,
    started_at: null,
    finished_at: null,
  },
]

test('runs table shows progress, credits and a cancel action for active runs', async () => {
  const user = userEvent.setup()
  const selected: number[] = []
  const cancelled: number[] = []
  render(<RunsTable runs={[RUN, { ...RUN, id: 8, status: 'done', finished_at: '2026-10-29T12:01:00+00:00' }]} selected={null} onSelect={(id) => selected.push(id)} onCancel={(id) => cancelled.push(id)} />)
  const table = screen.getByTestId('runs-table')
  expect(table).toHaveTextContent('1/3')
  expect(table).toHaveTextContent('12')
  expect(table).toHaveTextContent('wendy')
  expect(screen.getAllByRole('button', { name: 'Cancel' })).toHaveLength(1)
  await user.click(screen.getByRole('button', { name: '7' }))
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(selected).toEqual([7])
  expect(cancelled).toEqual([7])
})

test('run detail lists written fields, skipped reasons, errors and trace links', () => {
  render(
    <MemoryRouter>
      <RunDetail run={RUN} items={ITEMS} />
    </MemoryRouter>,
  )
  const items = screen.getByTestId('run-items')
  expect(items).toHaveTextContent('github')
  expect(items).toHaveTextContent('company_description')
  expect(items).toHaveTextContent('no personal website known')
  expect(items).toHaveTextContent('FIRECRAWL_API_KEY is not set')
  expect(screen.getByRole('link', { name: 'Logfire' })).toHaveAttribute('href', ITEMS[0].logfire_url!)
  expect(screen.getByRole('link', { name: ITEMS[1].person_id })).toHaveAttribute('href', `/people/${ITEMS[1].person_id}`)
  expect(screen.getByText(/mode=frugal/)).toBeInTheDocument()
})
