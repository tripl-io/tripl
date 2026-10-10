import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { MonitoringSignal, SignalAttribution } from '@/types'
import { breakdownValueSearch } from './useMonitoringDetailSearch'
import { WhyChangedPanel } from './WhyChangedPanel'

const attribution: SignalAttribution = {
  delta: -3390,
  columns: [
    {
      column: 'platform',
      explained_share: 0.92,
      values: [
        { value: 'ios', delta: -3120, expected: 5000, actual: 1880, share: 0.92 },
        { value: 'android', delta: -300, expected: 4000, actual: 3700, share: 0.0885 },
      ],
    },
    {
      column: 'country',
      explained_share: 0.5,
      values: [{ value: 'US', delta: -1700, expected: 3000, actual: 1300, share: 0.5 }],
    },
  ],
  release: { version: '4.12', previous_version: null, share: 0.38, reached_at: '2026-09-25T15:00:00Z' },
  // Worded by the backend; the panel prints both verbatim.
  headline: '92% of the drop comes from platform = ios (−3,120 of −3,390)',
  release_line: 'Release 4.12 reached 38% of traffic 3h before the drop',
  computed_at: '2026-09-25T18:05:00Z',
}

function signal(overrides: Partial<MonitoringSignal> = {}): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'event-1',
    state: 'recent',
    event_id: 'event-1',
    event_type_id: null,
    bucket: '2026-09-25T18:00:00Z',
    actual_count: 6610,
    expected_count: 10000,
    stddev: 500,
    z_score: -6.8,
    direction: 'drop',
    incident_child: false,
    muted: false,
    expected: false,
    hidden: false,
    attribution_status: 'not_computed',
    unit: null,
    detected_at: null,
    ...overrides,
  }
}

function renderPanel(props: Partial<Parameters<typeof WhyChangedPanel>[0]> = {}) {
  return render(
    <MemoryRouter>
      <WhyChangedPanel
        signal={signal({ attribution, attribution_status: 'ready' })}
        scanSettingsHref="/p/demo/scans/scan-1"
        {...props}
      />
    </MemoryRouter>,
  )
}

describe('WhyChangedPanel (#255)', () => {
  it('leads with the top attribution and draws a column per breakdown', () => {
    renderPanel()
    expect(screen.getByTestId('why-changed-headline')).toHaveTextContent(
      '92% of the drop comes from platform = ios (−3,120 of −3,390)',
    )
    const columns = screen.getAllByTestId('why-changed-column')
    expect(columns).toHaveLength(2)
    expect(within(columns[0]!).getByText('explains 92%')).toBeInTheDocument()
    // The remainder is drawn too, so the bars sum to the delta.
    expect(within(columns[0]!).getByText('Everything else')).toBeInTheDocument()
    expect(within(columns[0]!).getByText('+30')).toBeInTheDocument()
    expect(screen.getByTestId('why-changed-release')).toHaveTextContent(
      'Release 4.12 reached 38% of traffic 3h before the drop',
    )
  })

  it('links each value where the page has a breakdown view', () => {
    renderPanel({
      valueHref: (column, value) => `/p/demo/monitoring/event/event-1${breakdownValueSearch('?range=30', column, value)}`,
    })
    const link = screen.getByRole('link', { name: 'Open platform = ios' })
    expect(link).toHaveAttribute('href', '/p/demo/monitoring/event/event-1?range=30&tab=breakdowns&column=platform&value=ios')
  })

  it('leaves values as text without a breakdown view', () => {
    renderPanel()
    expect(screen.queryByRole('link', { name: /^Open / })).not.toBeInTheDocument()
    expect(screen.getByText('ios')).toBeInTheDocument()
  })

  it('prints the backend headline verbatim, not a locally derived one', () => {
    renderPanel({
      signal: signal({
        attribution_status: 'ready',
        attribution: {
          delta: -100,
          release: attribution.release,
          headline: 'Platform shifted in both directions; no single value explains the drop',
          release_line: null,
          columns: [
            {
              column: 'platform',
              explained_share: 0,
              values: [
                { value: 'web', delta: 400, expected: 10, actual: 410, share: -4 },
                { value: 'ios', delta: -450, expected: 500, actual: 50, share: 4.5 },
              ],
            },
          ],
        },
      }),
    })
    expect(screen.getByTestId('why-changed-headline')).toHaveTextContent(
      'Platform shifted in both directions; no single value explains the drop',
    )
    // A release without a backend line prints nothing of its own.
    expect(screen.queryByTestId('why-changed-release')).not.toBeInTheDocument()
    // Signed shares are never shown as percentages.
    expect(screen.queryByText(/-400%|450%/)).not.toBeInTheDocument()
  })

  it('omits the headline when the backend sent none for listed columns', () => {
    renderPanel({ signal: signal({ attribution_status: 'ready', attribution: { ...attribution, headline: null } }) })
    expect(screen.queryByTestId('why-changed-headline')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('why-changed-column')).toHaveLength(2)
  })

  it('labels an empty value and each list by its column', () => {
    renderPanel({
      valueHref: (column, value) => `/x?column=${column}&value=${encodeURIComponent(value)}`,
      signal: signal({
        attribution_status: 'ready',
        attribution: {
          ...attribution,
          columns: [
            {
              column: 'platform',
              explained_share: 0.9,
              values: [{ value: '', delta: -3000, expected: 4000, actual: 1000, share: 0.885 }],
            },
          ],
        },
      }),
    })
    expect(screen.getByRole('link', { name: 'Open platform = (empty)' })).toHaveTextContent('(empty)')
    const list = screen.getByRole('list', { name: 'platform' })
    expect(within(list).getByText(', actual 1,000 vs expected 4,000')).toHaveClass('sr-only')
  })

  it('shows the empty state with a link to the scan settings', () => {
    renderPanel({ signal: signal({ attribution: null, attribution_status: 'no_breakdown_columns' }) })
    expect(screen.getByTestId('why-changed-empty')).toHaveTextContent(
      'No breakdown columns configured — add one in scan settings.',
    )
    expect(screen.getByRole('link', { name: 'add one in scan settings' })).toHaveAttribute('href', '/p/demo/scans/scan-1')
  })

  it('renders nothing for a payload without attribution or a scope it does not cover', () => {
    const { container, rerender } = renderPanel({ signal: signal() })
    expect(container).toBeEmptyDOMElement()
    rerender(
      <MemoryRouter>
        <WhyChangedPanel
          signal={signal({ scope_type: 'metric', attribution: null, attribution_status: 'not_computed' })}
          scanSettingsHref={null}
        />
      </MemoryRouter>,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('notes a volume signal that was never attributed', () => {
    renderPanel({ signal: signal({ attribution: null, attribution_status: 'not_computed' }) })
    expect(screen.getByTestId('why-changed-panel')).toHaveTextContent(
      'No attribution is stored for this signal (detected before attribution existed, or no breakdown data for this scope).',
    )
  })
})
