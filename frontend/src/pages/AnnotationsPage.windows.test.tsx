import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { chartAnnotationsApi } from '@/api/chartAnnotations'
import { plannedEventsApi } from '@/api/plannedEvents'
import { formatUtcOffset } from '@/lib/datetime'
import { projectKey, projectsKey } from '@/lib/queryKeys'
import type { PlannedEvent } from '@/types'
import AnnotationsPage from './AnnotationsPage'

vi.mock('@/api/chartAnnotations', () => ({
  chartAnnotationsApi: { list: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api/plannedEvents', () => ({
  plannedEventsApi: { list: vi.fn(), delete: vi.fn(), suggestions: vi.fn(), create: vi.fn() },
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const WINDOW: PlannedEvent = {
  id: 'pe',
  project_id: 'p-1',
  label: 'Spring promo',
  description: null,
  starts_at: '2026-05-02T09:00:00Z',
  ends_at: '2026-05-04T09:00:00Z',
  direction: 'spike',
  scope_type: null,
  scope_ref: null,
  source: 'manual',
  scope_name: null,
  created_by_user_id: null,
  created_at: '2026-05-01T00:00:00Z',
  updated_at: '2026-05-01T00:00:00Z',
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/p/demo/annotations']}>
        <Routes>
          <Route path="/p/:slug/annotations" element={<AnnotationsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { invalidate }
}

beforeEach(() => {
  vi.mocked(chartAnnotationsApi.list).mockResolvedValue([])
  vi.mocked(plannedEventsApi.list).mockResolvedValue([WINDOW])
  vi.mocked(plannedEventsApi.suggestions).mockResolvedValue([])
  vi.mocked(plannedEventsApi.delete).mockResolvedValue(undefined)
})

// "Planned events" here meant windows of expected anomalies, while Coverage and
// Reconciliation use the phrase for the events of the plan.
describe('AnnotationsPage — expected windows', () => {
  it('names them expected windows, not planned events', async () => {
    renderPage()

    expect(await screen.findByRole('heading', { name: 'Expected windows (1)' })).toBeInTheDocument()
    expect(screen.queryByText(/Planned events/)).not.toBeInTheDocument()
  })

  it('tells an empty project where windows come from', async () => {
    vi.mocked(plannedEventsApi.list).mockResolvedValue([])
    renderPage()

    expect(await screen.findByText('No expected windows')).toBeInTheDocument()
    expect(screen.getByText(/mark one under any event or metric chart/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Detection settings' })).toHaveAttribute(
      'href',
      '/p/demo/settings/monitoring',
    )
  })

  it('names the zone of a window, and prints a holiday as its UTC day', async () => {
    vi.mocked(plannedEventsApi.list).mockResolvedValue([
      WINDOW,
      {
        ...WINDOW,
        id: 'h',
        label: 'German Unity Day',
        source: 'holiday',
        starts_at: '2026-10-03T00:00:00Z',
        ends_at: '2026-10-04T00:00:00Z',
      },
    ])
    renderPage()

    const list = await screen.findByTestId('planned-events-list')
    const promo = within(list).getByText('Spring promo').closest('li') as HTMLElement
    expect(promo).toHaveTextContent(formatUtcOffset(new Date(WINDOW.starts_at)))
    const holiday = within(list).getByText('German Unity Day').closest('li') as HTMLElement
    expect(holiday).toHaveTextContent('Oct 3, 2026, all day UTC')
  })

  it('deletes a project-wide window with the chart’s confirm and refreshes the summary', async () => {
    const { invalidate } = renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Delete expected window Spring promo' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('covers every chart in this project')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(plannedEventsApi.delete).toHaveBeenCalledWith('demo', 'pe'))
    // The sidebar badge and the Overview read the project summary.
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: projectKey('demo') }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: projectsKey() })
  })
})
