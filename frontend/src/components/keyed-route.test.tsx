import { fireEvent, render, screen } from '@testing-library/react'
import { useState, type ReactNode } from 'react'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { KeyedRoute } from './keyed-route'

// Stands in for a detail page's local state: a filter picked on entity A.
function Detail() {
  const [filter, setFilter] = useState('none')
  return (
    <div>
      <p>filter: {filter}</p>
      <button type="button" onClick={() => setFilter('country')}>
        Break down by country
      </button>
      <Link to="/p/demo/monitoring/event/b">Go to B</Link>
    </div>
  )
}

function renderAt(element: ReactNode) {
  return render(
    <MemoryRouter initialEntries={['/p/demo/monitoring/event/a']}>
      <Routes>
        <Route path="/p/:slug/monitoring/:scope/:id" element={element} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('KeyedRoute', () => {
  it('starts the next entity fresh instead of carrying the last one’s state', () => {
    renderAt(
      <KeyedRoute params={['slug', 'scope', 'id']}>
        <Detail />
      </KeyedRoute>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Break down by country' }))
    expect(screen.getByText('filter: country')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('link', { name: 'Go to B' }))
    expect(screen.getByText('filter: none')).toBeInTheDocument()
  })

  it('is what makes the difference: an unkeyed route element keeps the state', () => {
    // Pins the router behaviour the wrapper exists for, so the test
    // above cannot pass for a reason that has nothing to do with the key.
    renderAt(<Detail />)

    fireEvent.click(screen.getByRole('button', { name: 'Break down by country' }))
    fireEvent.click(screen.getByRole('link', { name: 'Go to B' }))
    expect(screen.getByText('filter: country')).toBeInTheDocument()
  })
})
