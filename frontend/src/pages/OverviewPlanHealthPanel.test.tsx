// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { healthApi } from '@/api/health'
import type { ProjectHealthResponse } from '@/types/health'
import { trendLabel, trendScores } from '@/lib/health'
import { OverviewPlanHealthPanel } from './OverviewPlanHealthPanel'

vi.mock('@/api/health', () => ({
  HEALTH_BATCH_MAX_IDS: 150,
  healthApi: {
    events: vi.fn(),
    event: vi.fn(),
    eventTypes: vi.fn(),
    project: vi.fn(),
  },
}))

const HEALTH: ProjectHealthResponse = {
  score: 72,
  grade: 'warning',
  scored_events: 40,
  healthy_count: 25,
  warning_count: 10,
  unhealthy_count: 5,
  component_averages: [],
  worst: [
    {
      event_id: 'event-1',
      name: 'paywall_shown',
      score: 18,
      grade: 'unhealthy',
      top_issue: 'Never seen in data',
    },
    {
      event_id: 'event-2',
      name: 'checkout_started',
      score: 41,
      grade: 'unhealthy',
      top_issue: '1 signal needs a verdict',
    },
  ],
  trend: [
    { day: '2026-09-25', score: 70, scored_events: 39 },
    { day: '2026-09-26', score: null, scored_events: 0 },
    { day: '2026-09-27', score: 72, scored_events: 40 },
  ],
  previous_score: 75,
  computed_at: '2026-09-27T06:00:00Z',
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <OverviewPlanHealthPanel slug="acme" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('OverviewPlanHealthPanel', () => {
  it('asks for the 30-day trend of the project', async () => {
    vi.mocked(healthApi.project).mockResolvedValue(HEALTH)
    renderPanel()
    await screen.findByRole('img', { name: 'Health 72 of 100' })
    expect(healthApi.project).toHaveBeenCalledWith('acme', 30, expect.anything())
  })

  it('shows the score, the change against a week ago and the trend', async () => {
    vi.mocked(healthApi.project).mockResolvedValue(HEALTH)
    renderPanel()

    expect(await screen.findByRole('img', { name: 'Health 72 of 100' })).toHaveTextContent('72')
    expect(screen.getByText(/Needs attention/)).toBeInTheDocument()
    expect(screen.getByTitle('Against 75/100 a week ago')).toHaveTextContent('−3 vs 7 days ago')
    expect(
      screen.getByRole('img', { name: 'Plan health over the last 30 days: from 70 to 72 of 100' }),
    ).toBeInTheDocument()
  })

  it('counts the events in each grade', async () => {
    vi.mocked(healthApi.project).mockResolvedValue(HEALTH)
    renderPanel()
    const grades = await screen.findByRole('list', { name: 'Events by grade' })
    expect(grades).toHaveTextContent('25 healthy')
    expect(grades).toHaveTextContent('10 needs attention')
    expect(grades).toHaveTextContent('5 unhealthy')
  })

  it('links each least healthy event to its page, with its top issue', async () => {
    vi.mocked(healthApi.project).mockResolvedValue(HEALTH)
    renderPanel()
    const worst = await screen.findByRole('list', { name: 'Least healthy events' })
    const link = within(worst).getByRole('link', { name: 'paywall_shown' })
    expect(link).toHaveAttribute('href', '/p/acme/monitoring/event/event-1')
    expect(within(worst).getByText('Never seen in data')).toBeInTheDocument()
    expect(within(worst).getByRole('img', { name: 'Health 18 of 100' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Least healthy first' })).toHaveAttribute(
      'href',
      '/p/acme/events?sort=health',
    )
  })

  it('omits the delta when there is no snapshot from a week ago', async () => {
    vi.mocked(healthApi.project).mockResolvedValue({ ...HEALTH, previous_score: null })
    renderPanel()
    await screen.findByRole('img', { name: 'Health 72 of 100' })
    expect(screen.queryByText(/vs 7 days ago/)).not.toBeInTheDocument()
  })

  it('says so when nothing on the main plan is scored', async () => {
    vi.mocked(healthApi.project).mockResolvedValue({
      ...HEALTH,
      score: null,
      grade: null,
      scored_events: 0,
      worst: [],
      trend: [],
    })
    renderPanel()
    expect(await screen.findByText('No events on the main plan to score yet.')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Least healthy first' })).not.toBeInTheDocument()
  })

  it('offers a retry when the request fails', async () => {
    vi.mocked(healthApi.project).mockRejectedValue(new Error('boom'))
    renderPanel()
    expect(await screen.findByText('Plan health unavailable')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })
})

describe('trend helpers', () => {
  it('skips days without a score', () => {
    expect(trendScores(HEALTH.trend)).toEqual([70, 72])
    expect(trendScores(undefined)).toEqual([])
  })

  it('describes the trend in words', () => {
    expect(trendLabel([70, 72], 30)).toBe('Plan health over the last 30 days: from 70 to 72 of 100')
    expect(trendLabel([], 30)).toBe('No plan health history in the last 30 days')
  })
})
