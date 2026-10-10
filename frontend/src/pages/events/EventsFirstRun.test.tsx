import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { EventsFirstRun } from './EventsFirstRun'

function renderFirstRun(props: { canWrite?: boolean; noEventTypes?: boolean } = {}) {
  const onNewEvent = vi.fn()
  const onBulkNew = vi.fn()
  render(
    <MemoryRouter>
      <EventsFirstRun
        slug="demo"
        canWrite={props.canWrite ?? true}
        noEventTypes={props.noEventTypes ?? false}
        onNewEvent={onNewEvent}
        onBulkNew={onBulkNew}
      />
    </MemoryRouter>,
  )
  return { onNewEvent, onBulkNew }
}

describe('EventsFirstRun', () => {
  it('offers both create forms and the scan once the project has an event type', () => {
    const { onNewEvent, onBulkNew } = renderFirstRun()

    fireEvent.click(screen.getByRole('button', { name: 'New event' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add many events…' }))
    expect(onNewEvent).toHaveBeenCalledOnce()
    expect(onBulkNew).toHaveBeenCalledOnce()
    expect(screen.getByRole('link', { name: 'Import from a scan' })).toHaveAttribute('href', '/p/demo/scans')
  })

  it('leads with the event type when the project has none, not with forms that cannot finish', () => {
    // Every event belongs to a type: "New event" and "Add many events" opened
    // forms whose Create could never unblock.
    renderFirstRun({ noEventTypes: true })

    expect(screen.getByText(/Every event belongs to an event type/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Create an event type' })).toHaveAttribute('href', '/p/demo/event-types')
    expect(screen.queryByRole('button', { name: 'New event' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add many events…' })).not.toBeInTheDocument()
    // A scan creates types and events both.
    expect(screen.getByRole('link', { name: 'Import from a scan' })).toBeInTheDocument()
  })

  it('offers a viewer only the scan, with or without types', () => {
    renderFirstRun({ canWrite: false, noEventTypes: true })

    expect(screen.queryByRole('link', { name: 'Create an event type' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Import from a scan' })).toBeInTheDocument()
  })
})
