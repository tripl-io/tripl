import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { ConnectDataSourceButton, GoToScansButton } from './first-scan-actions'

// The same first step read "Connect a data source", "Add connection" and
// "Run a scan" on sibling pages, in three button sizes.
describe('first-run step buttons', () => {
  it('connects a data source from the workspace settings', () => {
    render(
      <MemoryRouter>
        <ConnectDataSourceButton />
      </MemoryRouter>,
    )
    const link = screen.getByRole('link', { name: 'Connect a data source' })
    expect(link).toHaveAttribute('href', '/settings/data-sources')
    // The sm button: 28px tall, as on every first-run empty state.
    expect(link).toHaveClass('h-7')
  })

  it('opens the project’s Scans page, without promising a run', () => {
    render(
      <MemoryRouter>
        <GoToScansButton slug="demo" />
      </MemoryRouter>,
    )
    const link = screen.getByRole('link', { name: 'Go to Scans' })
    expect(link).toHaveAttribute('href', '/p/demo/scans')
    expect(link).toHaveClass('h-7')
  })
})
