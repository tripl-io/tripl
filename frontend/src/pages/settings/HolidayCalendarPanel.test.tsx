import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { anomalySettingsApi } from '@/api/anomalySettings'
import {
  activeSignalsKey,
  projectAnomalySettingsKey,
  projectKey,
  projectMonitoringSeriesKey,
  projectPlannedEventsKey,
} from '@/lib/queryKeys'
import type { ProjectAnomalySettings } from '@/types'
import { HolidayCalendarPanel } from './HolidayCalendarPanel'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  render(
    <QueryClientProvider client={qc}>
      <HolidayCalendarPanel slug="demo" country={null} canWrite />
    </QueryClientProvider>,
  )
  return { invalidate }
}

afterEach(() => {
  vi.restoreAllMocks()
  // The mocked toast is a module mock, not a spy: restoreAllMocks leaves its
  // calls in place, and the next test would see this one's toast.
  vi.mocked(toast.success).mockClear()
})

describe('HolidayCalendarPanel', () => {
  it('calls the holidays expected windows, in its subtitle and its toast', async () => {
    vi.spyOn(anomalySettingsApi, 'holidayCountries').mockResolvedValue(['DE'])
    vi.spyOn(anomalySettingsApi, 'update').mockResolvedValue({} as ProjectAnomalySettings)
    renderPanel()

    expect(screen.getByText(/become project-wide expected windows/)).toBeInTheDocument()
    expect(screen.queryByText(/planned events/)).toBeNull()

    const select = screen.getByLabelText('Country')
    await waitFor(() => expect(select).toBeEnabled())
    fireEvent.change(select, { target: { value: 'DE' } })

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Holidays of Germany (DE) added as expected windows'),
    )
  })

  it('refreshes what the holiday windows retag, not only the window list', async () => {
    vi.spyOn(anomalySettingsApi, 'holidayCountries').mockResolvedValue(['DE'])
    vi.spyOn(anomalySettingsApi, 'update').mockResolvedValue({} as ProjectAnomalySettings)
    const { invalidate } = renderPanel()

    const select = screen.getByLabelText('Country')
    await waitFor(() => expect(select).toBeEnabled())
    fireEvent.change(select, { target: { value: 'DE' } })

    await waitFor(() => expect(toast.success).toHaveBeenCalled())
    await waitFor(() => {
      const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey)
      for (const key of [
        projectAnomalySettingsKey('demo'),
        projectPlannedEventsKey('demo'),
        projectMonitoringSeriesKey('demo'),
        activeSignalsKey('demo'),
        projectKey('demo'),
      ]) {
        expect(keys).toContainEqual(key)
      }
    })
  })
})
