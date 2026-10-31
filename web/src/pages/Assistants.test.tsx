import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import type { AssistantRun, AssistantRunItem } from '../types'
import { RunCard, RunItemsTable, RunsTable } from './Assistants'

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
  worker: 'api:1',
  heartbeat_at: '2026-10-29T12:00:05+00:00',
  label: null,
  pause_reason: null,
}

const ITEMS: AssistantRunItem[] = [
  {
    id: 1,
    run_id: 7,
    workflow: 'enrich',
    person_id: 'aaaaaaaaaaaaaaaaaaaaaaaa',
    payload: {},
    label: 'Ada Okafor',
    attempts: 1,
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
    payload: {},
    label: null,
    attempts: 1,
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

test('runs table shows progress, credits, labels and who started a run', async () => {
  const user = userEvent.setup()
  const selected: number[] = []
  render(<RunsTable runs={[RUN, { ...RUN, id: 8, status: 'paused', pause_reason: 'credit cap', label: 'everyone' }]} selected={null} onSelect={(id) => selected.push(id)} />)
  const table = screen.getByTestId('runs-table')
  expect(table).toHaveTextContent('1/3')
  expect(table).toHaveTextContent('12')
  expect(table).toHaveTextContent('wendy')
  expect(table).toHaveTextContent('(credit cap)')
  expect(table).toHaveTextContent('everyone')
  await user.click(screen.getByRole('button', { name: '7' }))
  expect(selected).toEqual([7])
})

test('run card offers the controls its status allows', async () => {
  const user = userEvent.setup()
  const calls: string[] = []
  const { rerender } = render(
    <RunCard run={RUN} expanded={false} onToggle={() => calls.push('toggle')} onPause={() => calls.push('pause')} onResume={(cap) => calls.push(`resume:${cap}`)} onStop={() => calls.push('stop')} />,
  )
  expect(screen.getByTestId('run-credits')).toHaveTextContent('12 credits of 1500')
  expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '33')
  await user.click(screen.getByRole('button', { name: 'Pause' }))
  await user.click(screen.getByRole('button', { name: 'Stop' }))
  expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
  rerender(
    <RunCard run={{ ...RUN, status: 'paused', pause_reason: 'credit cap' }} expanded onToggle={() => calls.push('toggle')} onPause={() => calls.push('pause')} onResume={(cap) => calls.push(`resume:${cap}`)} onStop={() => calls.push('stop')}>
      <p>detail here</p>
    </RunCard>,
  )
  expect(screen.getByText('(credit cap)')).toBeInTheDocument()
  expect(screen.getByText('detail here')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull()
  await user.clear(screen.getByLabelText('New credit cap'))
  await user.type(screen.getByLabelText('New credit cap'), '2000')
  await user.click(screen.getByRole('button', { name: 'Resume' }))
  expect(calls).toEqual(['pause', 'stop', 'resume:2000'])
  rerender(<RunCard run={{ ...RUN, status: 'done', finished_at: '2026-10-29T12:01:00+00:00' }} expanded={false} onToggle={() => calls.push('toggle')} onStop={() => calls.push('stop')} />)
  expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
})

test('run items list written fields, skipped reasons, errors and trace links', () => {
  render(
    <MemoryRouter>
      <RunItemsTable items={ITEMS} />
    </MemoryRouter>,
  )
  const items = screen.getByTestId('run-items')
  expect(items).toHaveTextContent('github')
  expect(items).toHaveTextContent('company_description')
  expect(items).toHaveTextContent('no personal website known')
  expect(items).toHaveTextContent('FIRECRAWL_API_KEY is not set')
  expect(screen.getByRole('link', { name: 'Logfire' })).toHaveAttribute('href', ITEMS[0].logfire_url!)
  expect(screen.getByRole('link', { name: 'Ada Okafor' })).toHaveAttribute('href', `/people/${ITEMS[0].person_id}`)
  expect(screen.getByRole('link', { name: ITEMS[1].person_id! })).toBeInTheDocument()
})
