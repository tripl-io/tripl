import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { chartAnnotationsApi } from '@/api/chartAnnotations'
import { plannedEventsApi } from '@/api/plannedEvents'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'
import type { ChartAnnotation, PlannedEvent } from '@/types'
import AnnotationsPage from './AnnotationsPage'

vi.mock('@/api/chartAnnotations', () => ({
  chartAnnotationsApi: { list: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api/plannedEvents', () => ({
  plannedEventsApi: { list: vi.fn(), delete: vi.fn(), suggestions: vi.fn(), create: vi.fn() },
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const ANNOTATION: ChartAnnotation = {
  id: 'a',
  project_id: 'p-1',
  scope_type: null,
  scope_ref: null,
  bucket: '2026-05-01T10:00:00Z',
  label: 'label',
  description: null,
  color: 'var(--info)',
  source: 'manual',
  url: null,
  created_by_user_id: null,
  created_at: '2026-05-01T10:00:00Z',
}

const PLANNED: PlannedEvent = {
  id: 'pe',
  project_id: 'p-1',
  label: 'Spring promo',
  description: null,
  starts_at: '2026-05-02T00:00:00Z',
  ends_at: '2026-05-04T00:00:00Z',
  direction: 'spike',
  scope_type: 'metric',
  scope_ref: 'm-1',
  source: 'manual',
  scope_name: 'Revenue',
  created_by_user_id: null,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
}

function renderPage(auth: AuthContextValue | null = null) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <SessionProject session={auth}>
          <MemoryRouter initialEntries={['/p/demo/annotations']}>
            <Routes>
              <Route path="/p/:slug/annotations" element={<AnnotationsPage />} />
            </Routes>
          </MemoryRouter>
        </SessionProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(chartAnnotationsApi.list).mockResolvedValue([
    { ...ANNOTATION, id: 'old', label: 'Old deploy', bucket: '2026-04-01T10:00:00Z' },
    {
      ...ANNOTATION,
      id: 'rel',
      label: 'Release 1.4.0',
      source: 'release',
      bucket: '2026-05-03T10:00:00Z',
    },
    {
      ...ANNOTATION,
      id: 'ev',
      label: 'Hotfix',
      bucket: '2026-05-02T10:00:00Z',
      scope_type: 'event',
      scope_ref: 'e-1',
      scope_name: 'Purchase',
    },
  ])
  vi.mocked(plannedEventsApi.list).mockResolvedValue([PLANNED])
  vi.mocked(plannedEventsApi.suggestions).mockResolvedValue([])
})

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('AnnotationsPage', () => {
  it('lists every annotation newest first, with its chart, and the planned events', async () => {
    renderPage()

    const list = await screen.findByTestId('annotations-list')
    const items = within(list).getAllByRole('listitem')
    expect(items.map(item => item.querySelector('.font-medium')?.textContent)).toEqual([
      'Release 1.4.0',
      'Hotfix',
      'Old deploy',
    ])
    expect(within(list).getByRole('link', { name: 'Purchase' })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/e-1',
    )
    expect(within(list).getAllByText('project-wide')).toHaveLength(2)

    const planned = screen.getByTestId('planned-events-list')
    expect(within(planned).getByText('Spring promo')).toBeInTheDocument()
    expect(within(planned).getByText('Expected rise')).toBeInTheDocument()
    expect(within(planned).getByRole('link', { name: 'Revenue' })).toBeInTheDocument()
  })

  it('filters annotations by source', async () => {
    renderPage()
    const list = await screen.findByTestId('annotations-list')

    fireEvent.click(screen.getByRole('button', { name: 'Releases' }))
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
    expect(within(list).getByText('Release 1.4.0')).toBeInTheDocument()
  })

  it('marks holiday rows and offers no delete for them', async () => {
    vi.mocked(plannedEventsApi.list).mockResolvedValue([
      PLANNED,
      { ...PLANNED, id: 'h', label: 'Labour Day', source: 'holiday', scope_type: null, scope_ref: null },
    ])
    renderPage()

    const planned = await screen.findByTestId('planned-events-list')
    expect(within(planned).getByText('Holiday')).toBeInTheDocument()
    expect(within(planned).getByRole('button', { name: 'Delete expected window Spring promo' })).toBeInTheDocument()
    expect(within(planned).queryByRole('button', { name: 'Delete expected window Labour Day' })).not.toBeInTheDocument()
  })

  it('plans a suggested recurring window as its next planned events', async () => {
    // The slot reads in the reader's clock, like the windows listed under it.
    vi.stubEnv('TZ', 'Europe/Berlin')
    vi.mocked(plannedEventsApi.suggestions).mockResolvedValue([
      {
        scope_type: 'event',
        scope_ref: 'e-1',
        scope_name: 'Paywall View',
        weekday: 0,
        hour: 9,
        direction: 'spike',
        verdict_count: 3,
        last_bucket: '2026-09-28T09:00:00Z',
        note: 'Weekly promo email',
        windows: [
          { starts_at: '2026-10-05T09:00:00Z', ends_at: '2026-10-05T10:00:00Z' },
          { starts_at: '2026-10-12T09:00:00Z', ends_at: '2026-10-12T10:00:00Z' },
        ],
      },
    ])
    vi.mocked(plannedEventsApi.create).mockResolvedValue(PLANNED)
    renderPage()

    const list = await screen.findByTestId('planned-window-suggestions')
    expect(within(list).getByText('Every Monday at 11:00 AM (UTC+2)')).toBeInTheDocument()
    expect(within(list).queryByText(/09:00 UTC/)).not.toBeInTheDocument()
    fireEvent.click(within(list).getByRole('button', { name: 'Plan the next 2 windows on Paywall View' }))

    await waitFor(() => expect(plannedEventsApi.create).toHaveBeenCalledTimes(2))
    expect(plannedEventsApi.create).toHaveBeenCalledWith('demo', expect.objectContaining({
      label: 'Weekly promo email',
      // Stored text is read later in any zone: it names the slot in UTC.
      description: 'Suggested from 3 expected verdicts on Paywall View, every Monday at 09:00 UTC.',
      starts_at: '2026-10-12T09:00:00Z',
      direction: 'spike',
      scope_type: 'event',
      scope_ref: 'e-1',
    }))
  })

  it('offers a viewer no delete', async () => {
    renderPage(personaAuth('viewer'))
    await screen.findByTestId('annotations-list')
    expect(screen.queryByRole('button', { name: /^Delete / })).not.toBeInTheDocument()
  })
})
