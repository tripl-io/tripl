import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ShadowEventsResponse } from '@/api/reconciliation'
import type { DuplicateCheckResponse } from '@/types'
import { BranchContext } from '@/components/branch-context-internal'
import ReconciliationPage from './ReconciliationPage'

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const typedShadow: ShadowEventsResponse = {
  total: 1,
  new_count: 1,
  items: [
    {
      id: 'sh-paywall',
      scan_config_id: 'scan-1',
      scan_config_name: 'Mobile prod',
      event_type_id: 'et-screen',
      event_type_name: 'Screen',
      event_name: 'paywall_screen_view',
      observed_count: 120,
      first_seen_at: '2026-09-20T11:00:00Z',
      last_seen_at: '2026-09-26T11:00:00Z',
      status: 'new',
      accepted_event_id: null,
    },
  ],
}

const LOOKALIKE: DuplicateCheckResponse = {
  items: [
    {
      duplicates: [
        {
          event_id: 'ev-paywall',
          name: 'paywall_view',
          event_type_id: 'et-screen',
          status: 'live',
          score: 0.94,
          reasons: [],
        },
      ],
      lint: [],
      suggestion: null,
    },
  ],
  threshold: 0.88,
}

const NOTHING_ALIKE: DuplicateCheckResponse = {
  items: [{ duplicates: [], lint: [], suggestion: null }],
  threshold: 0.88,
}

function mockFetch(check: DuplicateCheckResponse | 'fail') {
  const accepted: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    if (url.includes('/events/duplicate-check')) {
      if (check === 'fail') return new Response('boom', { status: 500 })
      return jsonResponse(check)
    }
    const accept = /shadow-events\/([^/?]+)\/accept/.exec(url)
    if (accept?.[1]) {
      accepted.push(accept[1])
      return jsonResponse({ candidate_id: accept[1], event_id: 'ev-new', status: 'accepted' })
    }
    if (url.includes('/reconciliation/coverage')) {
      return jsonResponse({ days: 14, summary: { total_count: 0, matched_count: 0, coverage_pct: null }, items: [] })
    }
    if (url.includes('/reconciliation/dead-events')) return jsonResponse({ days: 30, total: 0, items: [] })
    if (url.includes('/reconciliation/shadow-events')) return jsonResponse(typedShadow)
    if (url.includes('/event-types')) return jsonResponse([])
    if (url.endsWith('/projects/demo')) return jsonResponse({ slug: 'demo', latest_scan_job: null })
    return jsonResponse({})
  })
  return accepted
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <BranchContext.Provider value={{ branchId: null, setBranchId: () => {}, slug: 'demo' }}>
        <MemoryRouter initialEntries={['/p/demo/reconciliation']}>
          <Routes>
            <Route path="/p/:slug/reconciliation" element={<ReconciliationPage />} />
          </Routes>
        </MemoryRouter>
      </BranchContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ReconciliationPage shadow accept duplicate check (F12, #265)', () => {
  it('asks before accepting a look-alike, and accepts on "Accept anyway"', async () => {
    const accepted = mockFetch(LOOKALIKE)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Accept' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText(/Looks like/)).toHaveTextContent('Looks like paywall_view (94%)')
    expect(within(dialog).getByRole('link', { name: /Open paywall_view/ })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/ev-paywall',
    )
    expect(accepted).toEqual([])

    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Accept anyway' }))
    })
    await waitFor(() => expect(accepted).toEqual(['sh-paywall']))
  })

  it('does not accept when the dialog is cancelled', async () => {
    const accepted = mockFetch(LOOKALIKE)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Accept' }))
    const dialog = await screen.findByRole('alertdialog')
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    })
    expect(accepted).toEqual([])
  })

  it('accepts straight away when nothing looks alike', async () => {
    const accepted = mockFetch(NOTHING_ALIKE)
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Accept' }))
    await waitFor(() => expect(accepted).toEqual(['sh-paywall']))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('accepts as before when the check fails', async () => {
    const accepted = mockFetch('fail')
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Accept' }))
    await waitFor(() => expect(accepted).toEqual(['sh-paywall']))
  })
})
