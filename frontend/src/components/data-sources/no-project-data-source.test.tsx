import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthContext } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import type { Role } from '@/types'
import { NoProjectDataSource } from './no-project-data-source'

function renderAs(role: Role) {
  render(
    <AuthContext.Provider value={authAs(role)}>
      <MemoryRouter>
        <NoProjectDataSource id="metric-sql-data-source" />
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}

describe('NoProjectDataSource', () => {
  it('offers an owner the way to connect one', () => {
    renderAs('owner')

    expect(screen.getByText(/No data source in this project yet/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Connect one' })).toHaveAttribute(
      'href',
      expect.stringContaining('/settings/data-sources'),
    )
  })

  it('tells anyone else that an owner connects data sources', () => {
    renderAs('member')

    expect(screen.getByText(/An owner has to connect one first/)).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('takes the select it stands in for, so a required message can send focus to it', () => {
    renderAs('member')

    const note = document.getElementById('metric-sql-data-source')!
    note.focus()
    expect(note).toHaveFocus()
  })
})
