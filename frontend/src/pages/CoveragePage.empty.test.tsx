import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { projectsApi } from '@/api/projects'
import { reconciliationApi } from '@/api/reconciliation'
import type { Project, ProjectSummary } from '@/types'
import CoveragePage from './CoveragePage'

function project(summary: Partial<ProjectSummary>): Project {
  return {
    id: 'p1',
    name: 'Blank',
    slug: 'blank',
    description: '',
    app_version_keep_releases: 5,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    summary: {
      event_type_count: 0,
      event_count: 0,
      active_event_count: 0,
      implemented_event_count: 0,
      review_pending_event_count: 0,
      archived_event_count: 0,
      variable_count: 0,
      scan_count: 0,
      alert_destination_count: 0,
      alert_rule_count: 0,
      monitoring_signal_count: 0,
      firing_monitor_count: 0,
      open_incident_count: 0,
      failing_scan_config_count: 0,
      latest_scan_job: null,
      latest_signal: null,
      ...summary,
    },
  }
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/p/blank/coverage']}>
        <Routes>
          <Route path="/p/:slug/coverage" element={<CoveragePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

// A blank project showed "Plan coverage 0%" and four zeros over "No events to
// cover yet": a failing grade for a plan with nothing in it.
describe('CoveragePage — nothing to cover', () => {
  it('shows the empty state alone, with no stat strip above it', async () => {
    vi.spyOn(projectsApi, 'get').mockResolvedValue(project({}))
    vi.spyOn(reconciliationApi, 'deadEvents').mockResolvedValue({ days: 30, total: 0, items: [] })

    renderPage()

    expect(await screen.findByRole('heading', { name: 'No events to cover yet' })).toBeInTheDocument()
    expect(screen.queryByText('Plan coverage')).not.toBeInTheDocument()
    expect(screen.queryByText('0%')).not.toBeInTheDocument()
    // "Events", as the sidebar and "Go to Scans" name their pages.
    expect(screen.getByRole('link', { name: 'Go to Events' })).toHaveAttribute('href', '/p/blank/events')
  })

  it('gives a plan whose events are all archived no score rather than 0%', async () => {
    vi.spyOn(projectsApi, 'get').mockResolvedValue(project({ event_count: 4, archived_event_count: 4 }))
    vi.spyOn(reconciliationApi, 'deadEvents').mockResolvedValue({ days: 30, total: 0, items: [] })

    renderPage()

    // The tile renders with a skeleton while the project loads, so wait for
    // the figure itself rather than for the label.
    const tile = (await screen.findByText('Plan coverage')).closest('dl') as HTMLElement
    expect(await within(tile).findByText('—')).toBeInTheDocument()
    expect(within(tile).queryByText('0%')).not.toBeInTheDocument()
  })
})
