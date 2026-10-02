import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { healthApi } from '@/api/health'
import type { EventHealth } from '@/types'
import { EventHealthCard } from './EventHealthCard'

function renderCard(status: string | undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <EventHealthCard slug="acme" eventId="ev-1" status={status} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => vi.restoreAllMocks())

describe('EventHealthCard', () => {
  it('scores a live event on the main plan', async () => {
    const spy = vi.spyOn(healthApi, 'event').mockResolvedValue({
      event_id: 'ev-1',
      score: 90,
      grade: 'A',
      top_issue: null,
      components: [],
    } as unknown as EventHealth)
    renderCard('live')
    expect(spy).toHaveBeenCalledOnce()
    expect(await screen.findByText('Health')).toBeInTheDocument()
  })

  it('does not ask about an archived event, which is never scored', () => {
    // The endpoint answers 404 for it; asking printed that 404 in the console
    // of every archived event page.
    const spy = vi.spyOn(healthApi, 'event')
    renderCard('archived')
    expect(spy).not.toHaveBeenCalled()
  })

  it('waits for the status before asking', () => {
    const spy = vi.spyOn(healthApi, 'event')
    renderCard(undefined)
    expect(spy).not.toHaveBeenCalled()
  })
})
