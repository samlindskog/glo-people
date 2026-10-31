import { render, screen } from '@testing-library/react'
import ActivityFeed from './ActivityFeed'

test('renders each event with its kind, newest last, and a placeholder when empty', () => {
  const { rerender } = render(<ActivityFeed events={[]} empty="Waiting for activity…" />)
  expect(screen.getByText('Waiting for activity…')).toBeInTheDocument()
  rerender(
    <ActivityFeed
      events={[
        { kind: 'step', message: 'reading 42 characters about Ada', ts: '2026-10-31T10:00:00Z' },
        { kind: 'agent', message: 'storage: thinking' },
        { kind: 'provider', message: 'fullenrich/people/match · 5 credit(s)' },
        { kind: 'write', message: 'notes: set' },
      ]}
    />,
  )
  const feed = screen.getByTestId('activity-feed')
  expect(feed.querySelectorAll('li')).toHaveLength(4)
  expect(feed).toHaveTextContent('storage: thinking')
  expect(feed).toHaveTextContent('5 credit(s)')
  expect(feed.querySelectorAll('li')[3]).toHaveTextContent('notes: set')
  expect(screen.getAllByText('agent')).toHaveLength(1)
})
