import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import type { EventType, MonitoringSignal } from '@/types'
import { EventsHeader } from './EventsHeader'
import { eventsPageTitle } from './eventsViews'

const PAGE_VIEW = {
  id: 'et-pv',
  name: 'pv',
  display_name: 'Page View',
} as unknown as EventType

describe('EventsHeader', () => {
  it('shows the generic "Events" heading when no type tab is active', () => {
    render(
      <EventsHeader
        total={12}
        inReviewCount={0}
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
      />,
    )

    expect(screen.getByRole('heading', { name: 'Events' })).toBeInTheDocument()
  })

  it('reflects the active type in the heading on a type-scoped list', () => {
    render(
      <EventsHeader
        total={12}
        inReviewCount={0}
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
        activeType={PAGE_VIEW}
      />,
    )

    expect(screen.getByRole('heading', { name: 'Page View events' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Events' })).not.toBeInTheDocument()
  })

  it('says the in-review stat is project-wide, not a slice of Total', () => {
    // The archived tab rendered "TOTAL 1 · IN REVIEW 6 pending" above a single
    // archived row. Lifecycle status is single-valued, so 6 of those 1 events
    // cannot be awaiting review — the row only reads as one sentence because
    // nothing marked the wider scope.
    render(
      <EventsHeader
        total={1}
        inReviewCount={6}
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
      />,
    )

    const inReviewStat = screen.getByText('6').closest('dl')
    expect(inReviewStat).toHaveTextContent(/project/i)
    expect(
      screen.getByRole('button', { name: /ignores the tab, filters and search/i }),
    ).toBeInTheDocument()
  })

  it('prints the total once, with a thousands separator (EVT-16)', () => {
    render(
      <EventsHeader
        total={5000}
        inReviewCount={0}
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
      />,
    )

    expect(screen.getAllByText((5000).toLocaleString())).toHaveLength(1)
    expect(screen.queryByText('5000')).not.toBeInTheDocument()
  })

  it('shows schema drift once per event type, named, not once per row (EVT-33)', () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
        <EventsHeader
          total={300}
          inReviewCount={0}
          projectTotalSignal={null}
          eventTypeSignals={new Map()}
          slug="demo"
          typeDrifts={[
            { eventTypeId: 'et-pv', label: 'Page View', count: 3 },
            { eventTypeId: 'et-se', label: 'Structured', count: 1 },
          ]}
        />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(
      screen.getByRole('button', { name: '3 schema drifts on event type Page View' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: '1 schema drift on event type Structured' }),
    ).toBeInTheDocument()
  })

  it('says "none"/"open" for chart signals, never "live" (EV-5 / DS-7)', () => {
    // "Live" is the lifecycle status in green one column over; an open anomaly
    // must not borrow the word.
    const { rerender } = render(
      <EventsHeader
        total={3}
        inReviewCount={0}
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
      />,
    )
    const stat = () => screen.getByText('Open signals').closest('dl')
    expect(stat()).toHaveTextContent('none')
    expect(stat()).not.toHaveTextContent(/live|quiet/)

    rerender(
      <EventsHeader
        total={3}
        inReviewCount={0}
        projectTotalSignal={{ id: 's-1' } as unknown as MonitoringSignal}
        eventTypeSignals={new Map()}
      />,
    )
    expect(stat()).toHaveTextContent('open')
    expect(stat()).not.toHaveTextContent(/live/)
  })

  it('puts the nav group in the eyebrow and the stats in the boxed strip under the title (DS-2 / DS-5)', () => {
    const { container } = render(
      <EventsHeader
        total={3}
        inReviewCount={0}
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
      />,
    )
    expect(container.querySelector('[data-slot="page-eyebrow"]')).toHaveTextContent('Plan')
    expect(
      container.querySelector('[data-slot="page-stats"] [data-slot="mini-stat-strip"]'),
    ).not.toBeNull()
  })

  it('under a column filter, counts the matches, not the server total', () => {
    // "Total 5,000" sat above a table a column filter had narrowed to 12 rows.
    render(
      <EventsHeader
        total={5000}
        columnFilter={{ matching: 12, checked: 400 }}
        inReviewCount={0}
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
      />,
    )

    const stat = screen.getByText('Matching').closest('dl')
    expect(stat).toHaveTextContent(`12${(400).toLocaleString()} of ${(5000).toLocaleString()} checked`)
    expect(screen.queryByText('Total')).not.toBeInTheDocument()
  })

  it('shows a skeleton, not "0 · none", while the counts are pending (DS-25 / EV-19)', () => {
    render(
      <EventsHeader
        total={0}
        totalPending
        inReviewCount={0}
        inReviewPending
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
        signalsPending
      />,
    )
    const signals = screen.getByText('Open signals').closest('dl')
    expect(signals).not.toHaveTextContent(/none|0/)
    expect(screen.getByText('Events', { selector: 'dt' }).closest('dl')).not.toHaveTextContent('0')
    expect(screen.getByText('In review').closest('dl')).not.toHaveTextContent(/0|project-wide/)
  })

  it('titles the queues after themselves and links the views (EV-23)', () => {
    expect(eventsPageTitle('review', null)).toBe('Review queue')
    expect(eventsPageTitle('archived', null)).toBe('Archived events')
    expect(eventsPageTitle('all', null)).toBe('Events')
    expect(eventsPageTitle('review', PAGE_VIEW)).toBe('Page View events')

    render(
      <MemoryRouter>
        <EventsHeader
          total={3}
          inReviewCount={6}
          projectTotalSignal={null}
          eventTypeSignals={new Map()}
          activeTab="review"
          slug="demo"
        />
      </MemoryRouter>,
    )
    expect(screen.getByRole('heading', { name: 'Review queue' })).toBeInTheDocument()
    const views = screen.getByRole('navigation', { name: 'Event views' })
    expect(views).toBeInTheDocument()
    const review = screen.getByRole('link', { name: /Review queue/ })
    expect(review).toHaveAttribute('href', '/p/demo/events/review')
    expect(review).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('link', { name: 'All' })).toHaveAttribute('href', '/p/demo/events')
    // The in-review figure is the way into that queue.
    expect(screen.getByRole('link', { name: '6' })).toHaveAttribute('href', '/p/demo/events/review')
  })

  it('drops the stat strip for a project with no events (EV-18)', () => {
    const { container } = render(
      <EventsHeader
        total={0}
        inReviewCount={0}
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
        hideStats
      />,
    )
    expect(container.querySelector('[data-slot="mini-stat-strip"]')).toBeNull()
  })
})
