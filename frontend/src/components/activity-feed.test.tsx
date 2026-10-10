import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { ActivityItem } from '@/types'
import { ActivityFeed, type ActivityFeedVariant } from './activity-feed'

function item(overrides: Partial<ActivityItem>): ActivityItem {
  return {
    id: 'event:e1',
    project_id: 'project-1',
    project_slug: 'demo',
    project_name: 'Demo',
    type: 'event',
    severity: 'low',
    title: 'Event implemented: Signup',
    detail: 'Signup',
    occurred_at: '2026-06-25T10:00:00Z',
    target_path: '/p/demo/events/e1',
    ...overrides,
  }
}

function renderFeed(items: ActivityItem[], variant: ActivityFeedVariant) {
  return render(
    <MemoryRouter>
      <ActivityFeed items={items} variant={variant} />
    </MemoryRouter>,
  )
}

const VARIANTS: ActivityFeedVariant[] = ['rail', 'panel']

// The Overview's "Recent activity" card and the activity rail render these same
// rows. While the card kept its own copy, each fix landed in one place only.
describe.each(VARIANTS)('ActivityFeed (%s)', (variant) => {
  it('names a scan-generated event by its values, not its key=value signature', () => {
    renderFeed(
      [item({ title: 'Event created: event_name=Home Screen View | screen_name=Home' })],
      variant,
    )

    expect(screen.getByText('Event created: Home Screen View · Home')).toBeInTheDocument()
    expect(screen.queryByText(/event_name=/)).not.toBeInTheDocument()
  })

  it("shows a failed scan's error in words, never the raw exception", () => {
    renderFeed(
      [
        item({
          id: 'scan:j1',
          type: 'scan',
          severity: 'high',
          title: 'Scan failed: Nightly metrics',
          detail:
            "HTTPSConnectionPool(host='clickhouse.internal', port=8443): Read timed out. (read timeout=30)",
          target_path: null,
        }),
      ],
      variant,
    )

    expect(
      screen.getByText('Scan failed: the data source did not respond in time.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/clickhouse\.internal/)).not.toBeInTheDocument()
  })

  it('opens an alert row at its delivery, not the top of Alerting', () => {
    renderFeed(
      [
        item({
          id: 'alert-delivery:dlv-1',
          type: 'alert',
          severity: 'medium',
          title: 'Alert sent: Volume spike',
          detail: 'Slack · #alerts',
          target_path: '/p/demo/alerting',
        }),
      ],
      variant,
    )

    expect(screen.getByRole('link', { name: /Alert sent: Volume spike/ })).toHaveAttribute(
      'href',
      '/p/demo/alerting/dlv-1',
    )
  })

  it("collapses a scan's burst into one row that expands", () => {
    const burst = ['Signup', 'Login', 'Logout'].map((name, index) =>
      item({ id: `event:${name}`, title: `Event implemented: ${name}`, detail: name, occurred_at: `2026-06-25T10:00:0${index}Z` }),
    )
    renderFeed(burst, variant)

    const summary = screen.getByRole('button', { name: /3 events implemented/ })
    expect(summary).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('link', { name: /Event implemented: Signup/ })).not.toBeInTheDocument()

    fireEvent.click(summary)
    expect(screen.getByRole('link', { name: /Event implemented: Signup/ })).toBeInTheDocument()
  })
})

describe('ActivityFeed panel rows', () => {
  it('keeps the full title on hover where the row cuts it to one line', () => {
    renderFeed([item({ title: 'Scan failed: Nightly metrics', type: 'scan', target_path: null })], 'panel')

    expect(screen.getByText('Scan failed: Nightly metrics')).toHaveAttribute(
      'title',
      'Scan failed: Nightly metrics',
    )
  })
})
