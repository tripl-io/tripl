import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ThreadMuteToggle, WatchButton } from './watch-button'

const { get, watch, unwatch, setMuted } = vi.hoisted(() => ({
  get: vi.fn(),
  watch: vi.fn(),
  unwatch: vi.fn(),
  setMuted: vi.fn(),
}))

vi.mock('@/api/notifications', () => ({
  subscriptionsApi: { get, watch, unwatch, setMuted },
}))

function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

beforeEach(() => {
  get.mockReset()
  watch.mockReset()
  unwatch.mockReset()
  setMuted.mockReset()
})

describe('WatchButton (#259)', () => {
  it('watches an entity the reader does not watch yet', async () => {
    get.mockResolvedValue({ watching: false, muted: false, reasons: [] })
    watch.mockResolvedValue({ watching: true, muted: false, reasons: ['manual'] })

    renderWithClient(<WatchButton slug="demo" entityType="metric" entityId="m-1" />)

    fireEvent.click(await screen.findByRole('button', { name: 'Watch' }))

    await waitFor(() => expect(watch).toHaveBeenCalledWith('demo', 'metric', 'm-1'))
    expect(await screen.findByRole('button', { name: 'Unwatch' })).toHaveAttribute(
      'title',
      'Watching because you chose to watch it. Click to stop.',
    )
  })

  it('unwatches an entity the reader watches as its author', async () => {
    get.mockResolvedValue({ watching: true, muted: false, reasons: ['author'] })
    unwatch.mockResolvedValue({ watching: false, muted: false, reasons: [] })

    renderWithClient(<WatchButton slug="demo" entityType="event" entityId="e-1" />)

    const button = await screen.findByRole('button', { name: 'Unwatch' })
    expect(button).toHaveAttribute('title', 'Watching because you created it. Click to stop.')
    fireEvent.click(button)

    await waitFor(() => expect(unwatch).toHaveBeenCalledWith('demo', 'event', 'e-1'))
    expect(await screen.findByRole('button', { name: 'Watch' })).toBeInTheDocument()
  })

  it('shows a muted watch as Muted, and unmutes it from its menu', async () => {
    get.mockResolvedValue({ watching: true, muted: true, reasons: ['owner'] })
    setMuted.mockResolvedValue({ watching: true, muted: false, reasons: ['owner'] })

    renderWithClient(<WatchButton slug="demo" entityType="event_type" entityId="t-1" />)

    const trigger = await screen.findByRole('button', { name: 'Muted' })
    expect(screen.queryByRole('button', { name: 'Unwatch' })).toBeNull()
    fireEvent.keyDown(trigger, { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Unmute' }))

    await waitFor(() => expect(setMuted).toHaveBeenCalledWith('demo', 'event_type', 't-1', false))
    expect(await screen.findByRole('button', { name: 'Unwatch' })).toBeInTheDocument()
  })

  it('unwatches a muted watch from its menu', async () => {
    get.mockResolvedValue({ watching: true, muted: true, reasons: ['commenter'] })
    unwatch.mockResolvedValue({ watching: false, muted: false, reasons: [] })

    renderWithClient(<WatchButton slug="demo" entityType="event" entityId="e-1" />)

    fireEvent.keyDown(await screen.findByRole('button', { name: 'Muted' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Unwatch' }))

    await waitFor(() => expect(unwatch).toHaveBeenCalledWith('demo', 'event', 'e-1'))
    expect(await screen.findByRole('button', { name: 'Watch' })).toBeInTheDocument()
    expect(setMuted).not.toHaveBeenCalled()
  })

  it('draws nothing until the state is known, and nothing when it cannot be read', async () => {
    get.mockRejectedValue(new Error('boom'))

    renderWithClient(<WatchButton slug="demo" entityType="branch" entityId="b-1" />)

    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.queryByRole('button')).toBeNull()
  })
})

describe('ThreadMuteToggle (#259)', () => {
  it('mutes and unmutes the event thread', async () => {
    get.mockResolvedValue({ watching: true, muted: false, reasons: ['commenter'] })
    setMuted.mockResolvedValue({ watching: true, muted: true, reasons: ['commenter'] })

    renderWithClient(<ThreadMuteToggle slug="demo" eventId="e-1" />)

    fireEvent.click(await screen.findByRole('button', { name: 'Mute' }))
    await waitFor(() => expect(setMuted).toHaveBeenCalledWith('demo', 'event', 'e-1', true))
    expect(await screen.findByRole('button', { name: 'Unmute' })).toBeInTheDocument()
  })
})
