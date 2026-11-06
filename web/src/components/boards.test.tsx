import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { MemoryRouter } from 'react-router'
import { vi } from 'vitest'
import { api } from '../api'
import { PersonPicker, SlugList, moveItem, type PickedPerson } from './boards'

test('moveItem reorders within bounds and leaves the array alone otherwise', () => {
  expect(moveItem(['a', 'b', 'c'], 0, 1)).toEqual(['b', 'a', 'c'])
  expect(moveItem(['a', 'b', 'c'], 2, 0)).toEqual(['c', 'a', 'b'])
  const same = ['a', 'b']
  expect(moveItem(same, 0, -1)).toBe(same)
  expect(moveItem(same, 1, 2)).toBe(same)
  expect(moveItem(same, 1, 1)).toBe(same)
})

function Harness({ initial }: { initial: string[] }) {
  const [selected, setSelected] = useState(initial)
  return (
    <SlugList
      label="Jobs"
      available={[
        { slug: 'fastlane-founding-engineer', title: 'Founding Engineer', sub: '· Fastlane' },
        { slug: 'fastlane-head-of-growth', title: 'Head of Growth', sub: '· Fastlane' },
        { slug: 'pulse-cto', title: 'CTO', sub: '· Pulse' },
      ]}
      selected={selected}
      onChange={setSelected}
      emptyText="Nothing yet."
    />
  )
}

test('SlugList adds, reorders and removes items, and the add list excludes what is chosen', async () => {
  const user = userEvent.setup()
  render(<Harness initial={['pulse-cto']} />)
  const chosen = () => within(screen.getByTestId('selected-jobs')).getAllByRole('listitem').map((li) => li.textContent)

  expect(chosen()).toHaveLength(1)
  expect(screen.queryByRole('button', { name: /^CTO/ })).toBeNull() // already chosen: not offered

  await user.click(screen.getByRole('button', { name: /Founding Engineer/ }))
  await user.click(screen.getByRole('button', { name: /Head of Growth/ }))
  expect(chosen()).toEqual([expect.stringContaining('CTO'), expect.stringContaining('Founding Engineer'), expect.stringContaining('Head of Growth')])

  await user.click(screen.getByRole('button', { name: 'Move Head of Growth up' }))
  expect(chosen()[1]).toContain('Head of Growth')
  expect(screen.getByRole('button', { name: 'Move CTO up' })).toBeDisabled()

  await user.click(screen.getByRole('button', { name: 'Remove CTO' }))
  expect(chosen()).toHaveLength(2)
  expect(screen.getByRole('button', { name: /^CTO/ })).toBeInTheDocument() // offered again

  await user.type(screen.getByLabelText('Filter jobs'), 'pul')
  expect(screen.getAllByRole('button', { name: /Add$/ })).toHaveLength(1)
})

test('PersonPicker offers to create the person when nobody matches, and uses the new record', async () => {
  const user = userEvent.setup()
  vi.spyOn(api, 'search').mockResolvedValue({ total: 0, page: 1, limit: 8, results: [], search_id: null })
  const find = vi.spyOn(api, 'findPeople').mockResolvedValue({ decision: 'new', person_id: null, tier: null, keys: [], candidates: [] })
  const create = vi.spyOn(api, 'createPerson').mockResolvedValue({ id: 'abc123', tier: 'plus_two', name: 'Zed Nobody', norm: {}, norm_flags: {} })
  const picked: PickedPerson[] = []

  function Host() {
    const [value, setValue] = useState<PickedPerson | null>(null)
    return (
      <PersonPicker
        value={value}
        onChange={(p) => {
          if (p) picked.push(p)
          setValue(p)
        }}
      />
    )
  }
  render(
    <MemoryRouter>
      <Host />
    </MemoryRouter>,
  )
  await user.type(screen.getByLabelText('Person'), 'Zed Nobody')
  expect(await screen.findByText('Nobody by that name.')).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Create a person' }))
  const form = screen.getByTestId('create-person')
  expect(within(form).getByLabelText('Name')).toHaveValue('Zed Nobody') // the search term carries over
  await user.type(within(form).getByLabelText('Email'), 'zed@x.io')
  await user.click(within(form).getByRole('button', { name: 'Create and use' }))

  expect(await screen.findByTestId('picked-person')).toHaveTextContent('Zed Nobody')
  expect(find).toHaveBeenCalledWith({ name: 'Zed Nobody', email: 'zed@x.io' })
  expect(create).toHaveBeenCalledWith('plus_two', { name: 'Zed Nobody', email: 'zed@x.io' })
  expect(picked).toEqual([{ id: 'abc123', name: 'Zed Nobody' }])
})

test('PersonPicker refuses to create a double: a record the database already holds is offered instead', async () => {
  const user = userEvent.setup()
  vi.spyOn(api, 'search').mockResolvedValue({ total: 0, page: 1, limit: 8, results: [], search_id: null })
  vi.spyOn(api, 'findPeople').mockResolvedValue({
    decision: 'matched',
    person_id: 'bo1',
    tier: 'identifier',
    keys: ['email'],
    candidates: [{ person_id: 'bo1', name: 'Bo Chen', tier: 'member', keys: ['email'] }],
  })
  const create = vi.spyOn(api, 'createPerson')
  const picked: PickedPerson[] = []

  function Host() {
    const [value, setValue] = useState<PickedPerson | null>(null)
    return (
      <PersonPicker
        value={value}
        onChange={(p) => {
          if (p) picked.push(p)
          setValue(p)
        }}
      />
    )
  }
  render(
    <MemoryRouter>
      <Host />
    </MemoryRouter>,
  )
  await user.type(screen.getByLabelText('Person'), 'Bo')
  await user.click(screen.getByRole('button', { name: 'Create a person' }))
  const form = screen.getByTestId('create-person')
  await user.clear(within(form).getByLabelText('Name'))
  await user.type(within(form).getByLabelText('Name'), 'Bo Chen')
  await user.type(within(form).getByLabelText('Email'), 'bo@x.io')
  await user.click(within(form).getByRole('button', { name: 'Create and use' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('Already in glo-people (matched by email)')
  expect(create).not.toHaveBeenCalled()
  await user.click(within(screen.getByTestId('existing-people')).getByRole('button', { name: 'Use Bo Chen' }))
  expect(await screen.findByTestId('picked-person')).toHaveTextContent('Bo Chen')
  expect(picked).toEqual([{ id: 'bo1', name: 'Bo Chen' }])
})
