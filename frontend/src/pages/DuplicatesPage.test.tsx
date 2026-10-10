import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DuplicateClustersResponse, Project } from '@/types'
import { duplicatesApi } from '@/api/duplicates'
import { eventsApi } from '@/api/events'
import { projectsApi } from '@/api/projects'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { BranchContext } from '@/components/branch-context-internal'
import DuplicatesPage from './DuplicatesPage'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

vi.mock('@/api/duplicates', () => ({
  MAX_DUPLICATE_CANDIDATES: 500,
  duplicatesApi: { check: vi.fn(), clusters: vi.fn(), dismiss: vi.fn() },
}))
vi.mock('@/api/events', () => ({ eventsApi: { update: vi.fn() } }))
vi.mock('@/api/dependencies', () => ({
  dependenciesApi: { impact: vi.fn().mockResolvedValue({ items: [] }), get: vi.fn() },
}))
// The page reads the plan's size to tell an empty plan from an all-clear
// (DuplicatesPage.empty.test.tsx); these tests are about a plan with events.
vi.mock('@/api/projects', () => ({
  projectsApi: { get: vi.fn() },
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const PAGE: DuplicateClustersResponse = {
  items: [
    {
      score: 0.94,
      events: [
        { id: 'ev-old', name: 'paywall_screen_view', status: 'live', volume_7d: 40, event_type_id: 'et-1' },
        { id: 'ev-main', name: 'paywall_view', status: 'live', volume_7d: 2100, event_type_id: 'et-1' },
      ],
    },
  ],
  next_cursor: null,
  total: 1,
  threshold: 0.88,
}

function viewer(): AuthContextValue {
  return personaAuth('viewer')
}

function renderPage(auth: AuthContextValue | null = null) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <SessionProject session={auth}>
          <BranchContext.Provider value={{ branchId: null, setBranchId: () => {}, slug: 'demo' }}>
            <MemoryRouter initialEntries={['/p/demo/duplicates']}>
              <Routes>
                <Route path="/p/:slug/duplicates" element={<DuplicatesPage />} />
              </Routes>
            </MemoryRouter>
          </BranchContext.Provider>
        </SessionProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(projectsApi.get).mockResolvedValue({ summary: { event_count: 2 } } as Project)
  vi.mocked(duplicatesApi.clusters).mockResolvedValue(PAGE)
  vi.mocked(duplicatesApi.dismiss).mockResolvedValue({
    event_a_id: 'ev-main',
    event_b_id: 'ev-old',
    created: true,
  })
  vi.mocked(eventsApi.update).mockResolvedValue({} as never)
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('DuplicatesPage (F12, #265)', () => {
  it('lists a cluster with its score, each member linked to its page', async () => {
    renderPage()

    expect(await screen.findByRole('heading', { name: '2 events · 94% alike' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Open paywall_screen_view/ })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/ev-old',
    )
    // The busier member is proposed as the one to keep.
    expect(screen.getByRole('radio', { name: 'Keep paywall_view' })).toBeChecked()
  })

  it('deprecates the other member with the kept one as successor, after a confirm', async () => {
    renderPage()

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Set paywall_view as successor of paywall_screen_view and deprecate it',
      }),
    )
    const dialog = await screen.findByRole('alertdialog')
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Set successor & deprecate' }))
    })

    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-old',
        { status: 'deprecated', superseded_by_event_id: 'ev-main' },
        null,
      ),
    )
  })

  it('follows the reader’s choice of which member to keep', async () => {
    renderPage()

    fireEvent.click(await screen.findByRole('radio', { name: 'Keep paywall_screen_view' }))
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Set paywall_screen_view as successor of paywall_view and deprecate it',
      }),
    )
    const dialog = await screen.findByRole('alertdialog')
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Set successor & deprecate' }))
    })

    await waitFor(() =>
      expect(eventsApi.update).toHaveBeenCalledWith(
        'demo',
        'ev-main',
        { status: 'deprecated', superseded_by_event_id: 'ev-old' },
        null,
      ),
    )
  })

  it('dismisses the pair on "Not a duplicate"', async () => {
    renderPage()

    fireEvent.click(
      await screen.findByRole('button', { name: 'paywall_screen_view is not a duplicate of paywall_view' }),
    )
    await waitFor(() =>
      expect(duplicatesApi.dismiss).toHaveBeenCalledWith(
        'demo',
        { event_a_id: 'ev-main', event_b_id: 'ev-old' },
        null,
      ),
    )
  })

  it('offers a viewer the clusters without the actions', async () => {
    renderPage(viewer())

    expect(await screen.findByRole('heading', { name: '2 events · 94% alike' })).toBeInTheDocument()
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /deprecate/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /not a duplicate/ })).not.toBeInTheDocument()
  })

  it('says so when there is nothing to review', async () => {
    vi.mocked(duplicatesApi.clusters).mockResolvedValue({ items: [], next_cursor: null, total: 0, threshold: 0.88 })
    renderPage()

    expect(await screen.findByText('No likely duplicates')).toBeInTheDocument()
  })

  it('renders a cluster that arrives on two pages once', async () => {
    vi.mocked(duplicatesApi.clusters)
      .mockResolvedValueOnce({ ...PAGE, next_cursor: 'c2' })
      .mockResolvedValueOnce({
        items: [
          // The same members as page one's cluster, listed the other way round.
          { ...PAGE.items[0]!, events: [...PAGE.items[0]!.events].reverse() },
        ],
        next_cursor: null,
        total: 1,
        threshold: 0.88,
      })
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(duplicatesApi.clusters).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument())
    expect(screen.getAllByRole('heading', { name: '2 events · 94% alike' })).toHaveLength(1)
  })

  it('loads the next page by cursor', async () => {
    vi.mocked(duplicatesApi.clusters)
      .mockResolvedValueOnce({ ...PAGE, next_cursor: 'c2' })
      .mockResolvedValueOnce({
        items: [
          {
            score: 0.9,
            events: [
              { id: 'a', name: 'signup_start', status: 'live', volume_7d: 1, event_type_id: 'et-1' },
              { id: 'b', name: 'signup_started', status: 'live', volume_7d: 2, event_type_id: 'et-1' },
            ],
          },
        ],
        next_cursor: null,
        total: 2,
        threshold: 0.88,
      })
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Load more' }))
    expect(await screen.findByRole('heading', { name: '2 events · 90% alike' })).toBeInTheDocument()
    expect(duplicatesApi.clusters).toHaveBeenLastCalledWith('demo', 'c2', null, expect.anything())
  })
})
