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

  it('counts the review queue on its tab, not again as a stat', () => {
    // "Review queue 6 · Events 6 · In review 6 project-wide": the stat repeated
    // the tab's number on every tab, and a third time on the queue itself.
    render(
      <MemoryRouter>
        <EventsHeader
          total={1}
          inReviewCount={6}
          projectTotalSignal={null}
          eventTypeSignals={new Map()}
          activeTab="archived"
          slug="demo"
        />
      </MemoryRouter>,
    )

    expect(screen.getByRole('link', { name: /Review queue/ })).toHaveTextContent('6')
    expect(screen.queryByText('In review')).not.toBeInTheDocument()
    expect(screen.getAllByText('6')).toHaveLength(1)
  })

  it('prints the total once, with a thousands separator', () => {
    render(
      <EventsHeader
        total={5000}
        inReviewCount={0}
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
      />,
    )

    expect(screen.getAllByText('5,000')).toHaveLength(1)
    expect(screen.queryByText('5000')).not.toBeInTheDocument()
  })

  it('shows schema drift once per event type, named, not once per row', () => {
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

  it('says "none" when no chart signal is open, and a red figure when one is', () => {
    // "Live" is the lifecycle status in green one column over; an open anomaly
    // must not borrow the word. Nor repeat the caption: "1 · open" under
    // "Chart signals".
    const { rerender } = render(
      <EventsHeader
        total={3}
        inReviewCount={0}
        projectTotalSignal={null}
        eventTypeSignals={new Map()}
      />,
    )
    const stat = () => screen.getByText('Chart signals').closest('dl')
    expect(stat()).toHaveTextContent('none')
    expect(stat()).not.toHaveTextContent(/live|quiet/)
    // Overview's "Open signals" counts every significant signal; this one
    // counts only the charted series, so the two do not share a name.
    expect(screen.queryByText('Open signals')).not.toBeInTheDocument()

    rerender(
      <EventsHeader
        total={3}
        inReviewCount={0}
        projectTotalSignal={{ id: 's-1' } as unknown as MonitoringSignal}
        eventTypeSignals={new Map()}
      />,
    )
    expect(stat()).toHaveTextContent('1')
    expect(stat()).not.toHaveTextContent(/none|open\b|live/)
    expect(stat()?.querySelector('[data-slot="mini-stat-value"]')).toHaveAttribute('data-tone', 'danger')
  })

  it('puts the nav group in the eyebrow and the stats in the boxed strip under the title', () => {
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
    expect(stat).toHaveTextContent('12400 of 5,000 checked')
    expect(screen.queryByText('Total')).not.toBeInTheDocument()
  })

  it('shows a skeleton, not "0 · none", while the counts are pending', () => {
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
    const signals = screen.getByText('Chart signals').closest('dl')
    expect(signals).not.toHaveTextContent(/none|0/)
    expect(screen.getByText('Events', { selector: 'dt' }).closest('dl')).not.toHaveTextContent('0')
  })

  it('titles the queues after themselves and links the views', () => {
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
    // The tab carries the in-review count and is the way into that queue.
    expect(review).toHaveTextContent('6')
  })

  it('drops the stat strip for a project with no events', () => {
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
