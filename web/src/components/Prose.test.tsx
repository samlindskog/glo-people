import { render, screen } from '@testing-library/react'
import Prose from './Prose'

test('renders paragraphs, bullet lists, bold spans and code', () => {
  render(<Prose text={'**What it does.** Reads the `linkedin` URL.\n\n- one credit\n- a miss is free\n\nThen it stops.'} />)
  expect(screen.getByText('What it does.').tagName).toBe('STRONG')
  expect(screen.getByText('linkedin').tagName).toBe('CODE')
  expect(screen.getAllByRole('listitem').map((li) => li.textContent)).toEqual(['one credit', 'a miss is free'])
  expect(screen.getByText('Then it stops.')).toBeInTheDocument()
})
