import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PublicDemoBanner } from './public-demo-banner'

function renderWithStatus(status: Record<string, unknown>) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(
      new Response(JSON.stringify({ has_users: true, registration_enabled: true, ...status }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <PublicDemoBanner />
    </QueryClientProvider>,
  )
}

describe('PublicDemoBanner', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('says so on a public demo', async () => {
    renderWithStatus({ public_demo: true })
    expect(await screen.findByTestId('public-demo-banner')).toHaveTextContent(
      'connects to no warehouse of yours',
    )
  })

  it('is absent elsewhere', async () => {
    renderWithStatus({ public_demo: false })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.queryByTestId('public-demo-banner')).not.toBeInTheDocument()
  })
})
