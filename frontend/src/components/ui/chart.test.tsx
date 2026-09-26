import type { ReactElement, ReactNode } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

// Props every ComposedChart was rendered with, so tests can assert on values
// that never reach the DOM under jsdom (recharts skips its <svg> without a
// measured container size).
const composedChartProps = vi.hoisted(() => [] as Record<string, unknown>[])

vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts')
  const ActualComposedChart = actual.ComposedChart
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    ComposedChart: (props: Record<string, unknown>) => {
      composedChartProps.push(props)
      return <ActualComposedChart {...props} />
    },
  }
})

import { formatMetricValue, metricAxisFormatter } from '@/lib/metricFormat'
import type { ChartAnnotation, EventMetricPoint, EventMetricsResponse } from '@/types'
import { SHOW_RELEASES_STORAGE_KEY } from '@/hooks/useShowReleases'
import {
  AnomalyMark,
  AutomaticAnnotationLabel,
  buildChartData,
  ChartLegend,
  CustomTooltip,
  MetricsChart,
  MetricsMultiSeriesChart,
  MiniMetricsChart,
  MultiSeriesTooltip,
  renderCountSeries,
  SINGLE_SERIES_COLOR,
} from './chart'
import { EVENTS_NOUN, formatTooltipLabel, SERIES_COLORS } from './chart-format'
import { at } from '@/test/at'

describe('MetricsChart', () => {
  it('renders anomaly dots for anomalous points', () => {
    render(
      <MetricsChart
        granularity="day"
        data={[
          {
            bucket: '2026-01-01T10:00:00Z',
            count: 10,
            expected_count: null,
            stddev: null,
            is_anomaly: false,
            anomaly_direction: null,
            z_score: null,
          },
          {
            bucket: '2026-01-02T10:00:00Z',
            count: 0,
            expected_count: 10,
            stddev: 2,
            is_anomaly: true,
            anomaly_direction: 'drop',
            z_score: -10,
          },
        ]}
      />,
    )

    expect(screen.getByTestId('anomaly-dot')).toBeInTheDocument()
  })

  it('snaps annotations to the bucket that contains them and exposes them for screen readers', () => {
    render(
      <MetricsChart
        granularity="hour"
        data={[
          {
            bucket: '2026-01-01T10:00:00Z',
            count: 10,
            expected_count: null,
            stddev: null,
            is_anomaly: false,
            anomaly_direction: null,
            z_score: null,
          },
          {
            bucket: '2026-01-01T11:00:00Z',
            count: 12,
            expected_count: null,
            stddev: null,
            is_anomaly: false,
            anomaly_direction: null,
            z_score: null,
          },
        ]}
        annotations={[
          {
            id: 'a1',
            project_id: 'proj',
            scope_type: null,
            scope_ref: null,
            // Closer to the 11:00 start, but inside the 10:00 bucket — the
            // containing bucket wins (F25).
            bucket: '2026-01-01T10:45:00Z',
            label: 'v1.4 deploy',
            description: null,
            color: '#ef4444',
            source: 'manual' as const,
            url: null,
            created_by_user_id: null,
            created_at: '2026-01-01T09:00:00Z',
          },
        ]}
      />,
    )

    const marker = screen.getByTestId('chart-annotation')
    // Humanized like the rest of the summary, never the raw ISO instant (DS-25).
    expect(marker.textContent).toContain(formatTooltipLabel('2026-01-01T10:00:00Z', 'hour'))
    expect(marker.textContent).not.toContain('2026-01-01T10:00:00Z')
    expect(marker.textContent).toContain('v1.4 deploy')
  })

  // MO-8: the annotation form defaults to "now", which is past the newest
  // bucket's start; it used to be dropped without a trace.
  it('draws an annotation inside the newest bucket on that bucket', () => {
    const bucket = (instant: string, count: number): EventMetricPoint => ({
      bucket: instant,
      count,
      expected_count: null,
      stddev: null,
      is_anomaly: false,
      anomaly_direction: null,
      z_score: null,
    })
    const annotation = {
      project_id: 'proj',
      scope_type: null,
      scope_ref: null,
      description: null,
      color: '#ef4444',
      source: 'manual' as const,
      url: null,
      created_by_user_id: null,
      created_at: '2026-01-01T09:00:00Z',
    }
    render(
      <MetricsChart
        granularity="hour"
        data={[bucket('2026-01-01T10:00:00Z', 10), bucket('2026-01-01T11:00:00Z', 12)]}
        annotations={[
          { ...annotation, id: 'now', bucket: '2026-01-01T11:40:00Z', label: 'Deploy' },
          // Beyond the newest bucket's span: still outside the chart.
          { ...annotation, id: 'later', bucket: '2026-01-01T12:30:00Z', label: 'Later' },
        ]}
      />,
    )

    const markers = screen.getAllByTestId('chart-annotation')
    expect(markers).toHaveLength(1)
    expect(at(markers, 0).textContent).toContain(formatTooltipLabel('2026-01-01T11:00:00Z', 'hour'))
    expect(at(markers, 0).textContent).toContain('Deploy')
  })

  // F25: at day granularity an 18:00 instant belongs to its own day, not to
  // the next day whose midnight start happens to be closer.
  it('puts an evening annotation on its own day at day granularity', () => {
    const bucket = (instant: string, count: number): EventMetricPoint => ({
      bucket: instant,
      count,
      expected_count: null,
      stddev: null,
      is_anomaly: false,
      anomaly_direction: null,
      z_score: null,
    })
    render(
      <MetricsChart
        granularity="day"
        data={[
          bucket('2026-01-01T00:00:00Z', 10),
          bucket('2026-01-02T00:00:00Z', 12),
          bucket('2026-01-03T00:00:00Z', 11),
        ]}
        annotations={[
          {
            id: 'evening',
            project_id: 'proj',
            scope_type: null,
            scope_ref: null,
            bucket: '2026-01-01T18:00:00Z',
            label: 'Spike',
            description: null,
            color: '#ef4444',
            source: 'manual' as const,
            url: null,
            created_by_user_id: null,
            created_at: '2026-01-01T18:00:00Z',
          },
        ]}
      />,
    )

    const marker = screen.getByTestId('chart-annotation')
    expect(marker.textContent).toContain(formatTooltipLabel('2026-01-01T00:00:00Z', 'day'))
    expect(marker.textContent).not.toContain(formatTooltipLabel('2026-01-02T00:00:00Z', 'day'))
  })

  // MO-8: with collection lag, "now" is hours past the forecast bucket; it
  // still lands on the newest bucket while it is inside the requested window.
  it('clamps an annotation past the data but inside the window onto the newest bucket', () => {
    const bucket = (instant: string, count: number): EventMetricPoint => ({
      bucket: instant,
      count,
      expected_count: null,
      stddev: null,
      is_anomaly: false,
      anomaly_direction: null,
      z_score: null,
    })
    const annotation = {
      project_id: 'proj',
      scope_type: null,
      scope_ref: null,
      description: null,
      color: '#ef4444',
      source: 'manual' as const,
      url: null,
      created_by_user_id: null,
      created_at: '2026-01-01T18:45:00Z',
    }
    render(
      <MetricsChart
        granularity="hour"
        data={[bucket('2026-01-01T15:00:00Z', 10), bucket('2026-01-01T16:00:00Z', 12)]}
        forecast={[
          {
            bucket: '2026-01-01T17:00:00Z',
            expected_count: 12,
            stddev: 2,
          },
        ]}
        to="2026-01-01T19:00:00Z"
        annotations={[
          { ...annotation, id: 'now', bucket: '2026-01-01T18:45:00Z', label: 'Deploy' },
          // Past the requested window: still outside the chart.
          { ...annotation, id: 'future', bucket: '2026-01-01T21:00:00Z', label: 'Future' },
        ]}
      />,
    )

    const markers = screen.getAllByTestId('chart-annotation')
    expect(markers).toHaveLength(1)
    expect(at(markers, 0).textContent).toContain('Deploy')
  })

  it('separates annotations in the screen-reader summary (DS-25)', () => {
    const annotation = {
      project_id: 'proj',
      scope_type: null,
      scope_ref: null,
      description: null,
      color: '#ef4444',
      source: 'manual' as const,
      url: null,
      created_by_user_id: null,
      created_at: '2026-01-01T09:00:00Z',
    }
    const point: EventMetricPoint = {
      bucket: '2026-01-01T10:00:00Z',
      count: 10,
      expected_count: null,
      stddev: null,
      is_anomaly: false,
      anomaly_direction: null,
      z_score: null,
    }
    render(
      <MetricsChart
        granularity="hour"
        data={[point, { ...point, bucket: '2026-01-01T11:00:00Z' }]}
        annotations={[
          { ...annotation, id: 'a1', bucket: '2026-01-01T10:00:00Z', label: 'Deploy' },
          { ...annotation, id: 'a2', bucket: '2026-01-01T11:00:00Z', label: 'Rollback' },
        ]}
      />,
    )

    const description = screen.getByRole('img').getAttribute('aria-describedby')
    const summary = document.getElementById(description ?? '')?.textContent ?? ''
    expect(summary).toContain('Deploy; ')
    expect(summary).not.toMatch(/DeployJan|Deploy2026/)
  })

  describe('release and API annotations (#256)', () => {
    const point: EventMetricPoint = {
      bucket: '2026-01-01T10:00:00Z',
      count: 10,
      expected_count: null,
      stddev: null,
      is_anomaly: false,
      anomaly_direction: null,
      z_score: null,
    }
    const base: ChartAnnotation = {
      id: 'base',
      project_id: 'proj',
      scope_type: null,
      scope_ref: null,
      bucket: '2026-01-01T10:00:00Z',
      label: 'label',
      description: null,
      color: '#ef4444',
      source: 'manual',
      url: null,
      created_by_user_id: null,
      created_at: '2026-01-01T09:00:00Z',
    }
    const manual: ChartAnnotation = { ...base, id: 'm1', label: 'Hotfix deploy' }
    const release: ChartAnnotation = {
      ...base,
      id: 'r1',
      bucket: '2026-01-01T11:00:00Z',
      label: 'Release 1.4.0',
      source: 'release',
    }

    function summaryText(): string {
      const description = screen.getByRole('img').getAttribute('aria-describedby')
      return document.getElementById(description ?? '')?.textContent ?? ''
    }

    function renderChart(annotations: ChartAnnotation[]) {
      return render(
        <MetricsChart
          granularity="hour"
          data={[point, { ...point, bucket: '2026-01-01T11:00:00Z' }]}
          annotations={annotations}
        />,
      )
    }

    it('offers no toggle when every annotation was placed by hand', () => {
      renderChart([manual])
      expect(screen.queryByTestId('show-releases-toggle')).not.toBeInTheDocument()
    })

    it('shows releases by default, with a checked toggle', () => {
      renderChart([manual, release])
      expect(screen.getByRole('checkbox', { name: 'Show releases' })).toBeChecked()
      expect(summaryText()).toContain('Release 1.4.0')
      expect(summaryText()).toContain('Hotfix deploy')
    })

    it('hides release and API markers only, and remembers the choice', () => {
      const api: ChartAnnotation = { ...release, id: 'a1', label: 'Deploy #512', source: 'api' }
      renderChart([manual, release, api])

      fireEvent.click(screen.getByRole('checkbox', { name: 'Show releases' }))

      expect(screen.getByRole('checkbox', { name: 'Show releases' })).not.toBeChecked()
      expect(summaryText()).not.toContain('Release 1.4.0')
      expect(summaryText()).not.toContain('Deploy #512')
      // A manual annotation is never hidden by the toggle.
      expect(summaryText()).toContain('Hotfix deploy')
      expect(localStorage.getItem(SHOW_RELEASES_STORAGE_KEY)).toBe('false')
    })

    it('starts hidden when the stored preference is off', () => {
      localStorage.setItem(SHOW_RELEASES_STORAGE_KEY, 'false')
      renderChart([manual, release])
      expect(screen.getByRole('checkbox', { name: 'Show releases' })).not.toBeChecked()
      expect(summaryText()).not.toContain('Release 1.4.0')
    })
  })

  describe('AutomaticAnnotationLabel (#256)', () => {
    const snapped = {
      id: 'r1',
      bucket: '2026-01-01T11:00:00Z',
      label: 'Release 1.4.0',
      color: 'var(--fg-subtle)',
      source: 'release' as const,
      description: null,
      url: null,
    }

    it('draws a tag icon for a release, without a link when there is no URL', () => {
      render(
        <svg>
          <AutomaticAnnotationLabel x={10} y={5} anchor="start" annotation={snapped} />
        </svg>,
      )
      expect(screen.getByTestId('annotation-icon-release')).toBeInTheDocument()
      expect(screen.getByText('Release 1.4.0')).toBeInTheDocument()
      expect(screen.getByTestId('automatic-annotation-label').tagName.toLowerCase()).toBe('g')
    })

    it('links an API marker to its URL in a new tab', () => {
      render(
        <svg>
          <AutomaticAnnotationLabel
            x={10}
            y={5}
            anchor="end"
            annotation={{ ...snapped, source: 'api', label: 'Deploy #512', url: 'https://ci.example.com/runs/512' }}
          />
        </svg>,
      )
      expect(screen.getByTestId('annotation-icon-api')).toBeInTheDocument()
      const link = screen.getByTestId('automatic-annotation-label')
      expect(link.tagName.toLowerCase()).toBe('a')
      expect(link.getAttribute('href')).toBe('https://ci.example.com/runs/512')
      expect(link.getAttribute('target')).toBe('_blank')
      expect(link.getAttribute('rel')).toContain('noopener')
      expect(link.querySelector('title')?.textContent).toContain('API: Deploy #512')
    })

    it('draws nothing without a position', () => {
      const { container } = render(
        <svg>
          <AutomaticAnnotationLabel anchor="start" annotation={snapped} />
        </svg>,
      )
      expect(container.querySelector('text')).toBeNull()
    })
  })

  it('summarizes forecast points as a humanized range in the sr-only summary', () => {
    render(
      <MetricsChart
        granularity="hour"
        data={[
          {
            bucket: '2026-01-01T10:00:00Z',
            count: 10,
            expected_count: 9,
            stddev: 2,
            is_anomaly: false,
            anomaly_direction: null,
            z_score: null,
          },
        ]}
        forecast={[
          {
            bucket: '2026-01-01T11:00:00Z',
            expected_count: 12,
            stddev: 2,
          },
          {
            bucket: '2026-01-01T12:00:00Z',
            expected_count: 13,
            stddev: 2,
          },
        ]}
      />,
    )

    const marker = screen.getByTestId('forecast-point')
    // Collapsed into a start/end range, humanized — never one raw-ISO span per bucket.
    expect(marker.textContent).toContain('Forecast from')
    expect(marker.textContent).toContain('to')
    expect(marker.textContent).toContain('Jan 1')
    expect(marker.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T/)
  })

  it('pluralizes the anomaly count and humanizes buckets in the sr-only summary', () => {
    const { rerender } = render(
      <MetricsChart
        granularity="day"
        data={[
          {
            bucket: '2026-01-01T10:00:00Z',
            count: 10,
            expected_count: null,
            stddev: null,
            is_anomaly: false,
            anomaly_direction: null,
            z_score: null,
          },
          {
            bucket: '2026-01-02T10:00:00Z',
            count: 0,
            expected_count: 10,
            stddev: 2,
            is_anomaly: true,
            anomaly_direction: 'drop',
            z_score: -10,
          },
        ]}
      />,
    )

    const singular = screen.getByTestId('anomaly-dot')
    // Singular wording for exactly one anomaly — never "1 anomalies".
    expect(singular.textContent).toContain('1 anomaly detected')
    expect(singular.textContent).not.toContain('1 anomalies')
    // Bucket is humanized, not a raw ISO instant abutting the sentence.
    expect(singular.textContent).toContain('Jan 2, 2026')
    expect(singular.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T/)

    rerender(
      <MetricsChart
        granularity="day"
        data={[
          {
            bucket: '2026-01-01T10:00:00Z',
            count: 0,
            expected_count: 10,
            stddev: 2,
            is_anomaly: true,
            anomaly_direction: 'drop',
            z_score: -10,
          },
          {
            bucket: '2026-01-02T10:00:00Z',
            count: 0,
            expected_count: 10,
            stddev: 2,
            is_anomaly: true,
            anomaly_direction: 'drop',
            z_score: -10,
          },
        ]}
      />,
    )

    const plural = screen.getByTestId('anomaly-dot')
    expect(plural.textContent).toContain('2 anomalies detected')
    expect(plural.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T/)
  })

  // Regression for tripl-yfsj.2: the events-metrics `events_total` response is
  // count-only (no expected_count/stddev/band/forecast). MetricsChart must treat
  // it as a real, non-empty series (not the "No metrics data available" state) so
  // the volume series is charted — even when one bucket is a huge outlier that
  // drives the whole y-domain.
  it('charts a count-only (events_total) series instead of the empty state', () => {
    const countOnly: EventMetricPoint[] = [
      {
        bucket: '2026-01-01T10:00:00Z',
        count: 5000,
        expected_count: null,
        stddev: null,
        is_anomaly: false,
        anomaly_direction: null,
        z_score: null,
      },
      {
        // A large outlier bucket (e.g. a backfill) that drives the y-domain.
        bucket: '2026-01-01T11:00:00Z',
        count: 600000,
        expected_count: null,
        stddev: null,
        is_anomaly: true,
        anomaly_direction: 'spike',
        z_score: null,
      },
      {
        bucket: '2026-01-01T12:00:00Z',
        count: 4800,
        expected_count: null,
        stddev: null,
        is_anomaly: false,
        anomaly_direction: null,
        z_score: null,
      },
    ]

    render(<MetricsChart granularity="hour" data={countOnly} />)

    // Non-empty count-only data is charted, not dropped to the empty state…
    expect(screen.queryByText('No metrics data available')).not.toBeInTheDocument()
    // …and every bucket reaches the chart (sr-only summary), including the
    // flagged outlier.
    const summary = screen.getByTestId('anomaly-dot')
    expect(summary.textContent).toContain('1 anomaly detected')
  })
})

describe('renderCountSeries', () => {
  // jsdom never paints recharts, so the blank-on-late-mount fix is asserted on
  // the series element: the volume series must keep animation OFF so it renders
  // its final geometry immediately instead of settling into an empty enter-frame
  // when MetricsChart mounts late inside a Collapsible (tripl-yfsj.2).
  it.each(['line', 'line-only', 'bar'] as const)(
    'renders a non-animated count series for the %s chart style',
    (chartStyle) => {
      const series = renderCountSeries({
        chartStyle,
        chartColor: 'var(--chart-3)',
        gradientId: 'grad',
        mini: false,
      }) as ReactElement<{ dataKey: string; isAnimationActive: boolean }>

      expect(series.props.dataKey).toBe('count')
      expect(series.props.isAnimationActive).toBe(false)
    },
  )
})

// The tooltip never paints under jsdom (recharts needs real dimensions), so
// the valueFormatter threading is covered on the exported tooltip directly.
describe('CustomTooltip', () => {
  const point = {
    bucket: '2026-01-01T10:00:00Z',
    count: 0.08,
    expected_count: 0.05,
    stddev: 0.01,
    band: [0.03, 0.07] as [number, number],
  }

  it('keeps the default raw value + series label without a formatter', () => {
    render(
      <CustomTooltip
        active
        payload={[{ value: 0.08, payload: point }]}
        label="2026-01-01T10:00:00Z"
        granularity="hour"
        seriesLabel="%"
      />,
    )

    expect(screen.getByText('0.08 %')).toBeInTheDocument()
    // 0.08 rounds onto the band's 0.07 edge, so the secondary values keep one
    // more decimal instead of reading "inside the band" (MO-38).
    expect(screen.getByText('Expected 0.1 (normal 0–0.1)')).toBeInTheDocument()
  })

  it('routes value, expected and the normal range through valueFormatter', () => {
    render(
      <CustomTooltip
        active
        payload={[{ value: 0.08, payload: point }]}
        label="2026-01-01T10:00:00Z"
        granularity="hour"
        seriesLabel="%"
        valueFormatter={metricAxisFormatter('%')}
      />,
    )

    expect(screen.getByText('8%')).toBeInTheDocument()
    // The unit once, after the range; no σ jargon or raw deviation (MO-38).
    expect(screen.getByText('Expected 5% (normal 3–7%)')).toBeInTheDocument()
    expect(screen.queryByText(/σ band|Deviation/)).toBeNull()
  })

  it('names the zone of a sub-day bucket and not of a calendar day (MO-38)', () => {
    const { rerender } = render(
      <CustomTooltip
        active
        payload={[{ value: 0.08, payload: point }]}
        label="2026-01-01T10:00:00Z"
        granularity="hour"
        seriesLabel="%"
      />,
    )
    // getByText collapses whitespace (ICU may emit a narrow no-break space).
    const hourLabel = formatTooltipLabel('2026-01-01T10:00:00Z', 'hour')
      .replace(/\s+/g, ' ')
      .replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    expect(screen.getByText(new RegExp(`^${hourLabel} \\S+`))).toBeInTheDocument()

    rerender(
      <CustomTooltip
        active
        payload={[{ value: 0.08, payload: point }]}
        label="2026-01-01T00:00:00Z"
        granularity="day"
        seriesLabel="%"
      />,
    )
    expect(screen.getByText(formatTooltipLabel('2026-01-01T00:00:00Z', 'day'))).toBeInTheDocument()
  })

  it('says a partial bucket is partial (MO-5)', () => {
    render(
      <CustomTooltip
        active
        payload={[{ value: 5, payload: { ...point, count: 5, expected_count: null, band: undefined, partial_through: '2026-01-01T18:00:00Z' } }]}
        label="2026-01-01T00:00:00Z"
        granularity="day"
        seriesLabel="events"
      />,
    )

    expect(screen.getByText(/^Partial day: data through /)).toBeInTheDocument()
  })

  // DS-31 / MET-40: the axis formatter leaves a trailing unit off; the tooltip
  // spells the value out with it, and a currency leads.
  it('prefers tooltipFormatter over the axis formatter', () => {
    render(
      <CustomTooltip
        active
        payload={[{ value: 1234, payload: { ...point, count: 1234, expected_count: 1000, band: undefined } }]}
        label="2026-01-01T10:00:00Z"
        granularity="hour"
        seriesLabel="$"
        valueFormatter={metricAxisFormatter('$')}
        tooltipFormatter={value => formatMetricValue(value, '$')}
      />,
    )

    expect(screen.getByText('$1,234')).toBeInTheDocument()
    expect(screen.getByText('Expected $1,000')).toBeInTheDocument()
    expect(screen.queryByText(/1,234 \$/)).not.toBeInTheDocument()
  })
})

// Same jsdom constraint as CustomTooltip: the breakdown/version tooltip is
// verified directly. Percent-unit catalog metrics store fractions, so without
// a formatter the old hardcoded `value.toLocaleString() events` rendered
// "0.081 events" (tripl-4dej).
describe('MultiSeriesTooltip', () => {
  const payload = [
    { value: 0.081, dataKey: 'series_0', color: '#111111', name: 'ios' },
    { value: 0.05, dataKey: 'series_1', color: '#222222', name: 'android' },
  ]

  it('keeps the default `value seriesLabel` lines without a formatter', () => {
    render(
      <MultiSeriesTooltip
        active
        payload={payload}
        label="2026-01-01T10:00:00Z"
        granularity="hour"
        seriesLabel="events"
      />,
    )

    expect(screen.getByText('0.081 events')).toBeInTheDocument()
    expect(screen.getByText('0.05 events')).toBeInTheDocument()
  })

  it('routes series values through valueFormatter and drops the label suffix', () => {
    render(
      <MultiSeriesTooltip
        active
        payload={payload}
        label="2026-01-01T10:00:00Z"
        granularity="hour"
        seriesLabel="%"
        valueFormatter={metricAxisFormatter('%')}
      />,
    )

    // Stored fractions render ×100 with the formatter's own unit…
    expect(screen.getByText('8.1%')).toBeInTheDocument()
    expect(screen.getByText('5%')).toBeInTheDocument()
    // …and the seriesLabel suffix disappears entirely.
    expect(screen.queryByText(/events/)).not.toBeInTheDocument()
  })

  it('prefers tooltipFormatter over the axis formatter', () => {
    render(
      <MultiSeriesTooltip
        active
        payload={[{ value: 0.0045, dataKey: 'series_0', color: '#111111', name: 'ios' }]}
        label="2026-01-01T10:00:00Z"
        granularity="hour"
        seriesLabel="s"
        valueFormatter={metricAxisFormatter('s')}
        tooltipFormatter={value => formatMetricValue(value, 's')}
      />,
    )

    expect(screen.getByText('0.0045 s')).toBeInTheDocument()
  })
})

describe('buildChartData confidence band', () => {
  // A flagged bucket: actual 0 vs expected 10 with effective stddev 2. The
  // detector served `stddev` as the FLOORED effective stddev, so the band is
  // expected ± sigmaThreshold * stddev.
  const flagged: EventMetricPoint = {
    bucket: '2026-01-02T10:00:00Z',
    count: 0,
    expected_count: 10,
    stddev: 2,
    is_anomaly: true,
    anomaly_direction: 'drop',
    z_score: -5,
  }
  const normal: EventMetricPoint = {
    bucket: '2026-01-01T10:00:00Z',
    count: 10,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
  }

  it('draws the band as expected ± sigmaThreshold * effective_stddev', () => {
    const built = at(buildChartData([flagged], [], 3), 0)
    expect(built.band).toEqual([10 - 3 * 2, 10 + 3 * 2])
  })

  it('scales the band width with the served sigma threshold', () => {
    const narrow = at(buildChartData([flagged], [], 2), 0)
    const wide = at(buildChartData([flagged], [], 4), 0)
    expect(narrow.band).toEqual([6, 14])
    expect(wide.band).toEqual([2, 18])
  })

  it('keeps a flagged point outside the band and leaves normal buckets bandless', () => {
    const built = at(buildChartData([flagged], [], 3), 0)
    const [lower, upper] = built.band as [number, number]
    // actual 0 sits below the lower band edge -> visually "flagged".
    expect(flagged.count).toBeLessThan(lower)
    expect(upper).toBeGreaterThan(lower)

    const normalPoint = at(buildChartData([normal], [], 3), 0)
    expect(normalPoint.band).toBeUndefined()
  })

  it('falls back to the default multiplier for a missing/invalid threshold', () => {
    // 4, not 3: the fallback is the detector's own default sigma threshold
    // (ProjectAnomalySettings.sigma_threshold = 4.0). It used to be 3 under a
    // comment claiming the scan-config default (tripl-0zpq.299).
    const built = at(buildChartData([flagged], [], Number.NaN), 0)
    expect(built.band).toEqual([10 - 4 * 2, 10 + 4 * 2])
  })
})

// The `buildChartData confidence band` suite above hands the multiplier straight
// to the builder, so it stays green even if MetricsChart stops forwarding the
// prop — it certifies the arithmetic, not the wiring. These assert on the rows
// MetricsChart actually hands recharts, which is the only place the prop ->
// buildChartData hop is observable under jsdom (tripl-0zpq.299).
describe('MetricsChart served sigma threshold', () => {
  // A flagged bucket: actual 0 against expected 10 with effective stddev 2.
  const flagged: EventMetricPoint = {
    bucket: '2026-01-02T10:00:00Z',
    count: 0,
    expected_count: 10,
    stddev: 2,
    is_anomaly: true,
    anomaly_direction: 'drop',
    z_score: -6,
  }

  // jsdom measures every element as 0x0 and MetricsChart gates its
  // ResponsiveContainer on a positive size, so nothing reaches ComposedChart
  // without a measured box (mirrors the accessibility suite below).
  function renderCharted(node: ReactElement): Array<{ band?: [number, number] }> {
    const rect = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockReturnValue({ width: 400, height: 200, x: 0, y: 0, top: 0, left: 0, right: 400, bottom: 200, toJSON: () => ({}) })
    composedChartProps.length = 0
    render(node)
    rect.mockRestore()

    expect(composedChartProps).not.toHaveLength(0)
    return at(composedChartProps, -1).data as Array<{ band?: [number, number] }>
  }

  it('draws the band at the sigma threshold served on the payload', () => {
    // Shaped like the response MonitoringDetailPage and TabMetricsCard read, for
    // a project whose operator moved Settings -> Monitoring -> sigma to 6.0.
    const served: Pick<EventMetricsResponse, 'sigma_threshold' | 'data'> = {
      sigma_threshold: 6,
      data: [flagged],
    }

    const rows = renderCharted(
      // Unclamped, so the wiring is visible: at 6σ the lower edge is -2, which
      // a count chart floors at zero (MON-21, asserted below).
      <MetricsChart
        granularity="day"
        data={served.data}
        sigmaThreshold={served.sigma_threshold}
        nonNegative={false}
      />,
    )

    // expected ± 6σ, the multiplier the detector flagged this bucket with — NOT
    // the client's own DEFAULT_SIGMA_THRESHOLD of 4, which would read [2, 18].
    expect(at(rows, 0).band).toEqual([10 - 6 * 2, 10 + 6 * 2])
  })

  it('falls back to the client default when the payload serves no threshold', () => {
    // An older payload that predates the served `sigma_threshold`.
    const rows = renderCharted(
      <MetricsChart granularity="day" data={[flagged]} sigmaThreshold={undefined} />,
    )

    expect(at(rows, 0).band).toEqual([10 - 4 * 2, 10 + 4 * 2])
  })

  // MON-21: `expected - k·σ` below zero dragged a count chart's axis negative.
  it('floors a count series band at zero, but not a formatted (signed) metric', () => {
    const counted = renderCharted(<MetricsChart granularity="day" data={[flagged]} sigmaThreshold={6} />)
    expect(at(counted, 0).band).toEqual([0, 22])

    const signed = renderCharted(
      <MetricsChart
        granularity="day"
        data={[flagged]}
        sigmaThreshold={6}
        valueFormatter={(value) => value.toFixed(2)}
      />,
    )
    expect(at(signed, 0).band).toEqual([-2, 22])
  })

  // MON-22: the axis spans the requested window, not only the buckets with data.
  it('pads the rows out to the requested window with empty buckets', () => {
    const rows = renderCharted(
      <MetricsChart
        granularity="day"
        data={[flagged]}
        from="2026-01-01T10:00:00Z"
        to="2026-01-04T00:00:00Z"
      />,
    ) as unknown as Array<{ bucket: string; count: number | null }>

    expect(rows.map((row) => row.bucket)).toEqual([
      '2026-01-01T10:00:00.000Z',
      '2026-01-02T10:00:00Z',
      '2026-01-03T10:00:00.000Z',
    ])
    expect(at(rows, 0).count).toBeNull()
    expect(at(rows, 2).count).toBeNull()
  })
})

describe('buildChartData forecast floor (MON-21)', () => {
  it('clamps the forecast band at zero when asked', () => {
    const last: EventMetricPoint = {
      bucket: '2026-01-02T10:00:00Z',
      count: 1,
      expected_count: 2,
      stddev: 1,
      is_anomaly: false,
      anomaly_direction: null,
      z_score: null,
    }
    const built = buildChartData(
      [last],
      [{ bucket: '2026-01-03T10:00:00Z', expected_count: 1, stddev: 1 }],
      4,
      true,
    )
    expect(at(built, 0).band).toEqual([0, 6])
    expect(at(built, 1).forecast_band).toEqual([0, 5])
    // The whisker's offsets from the forecast value.
    expect(at(built, 1).forecast_error).toEqual([1, 4])
  })

  // MO-7: a dashed line from a spiking last actual down to the forecast read
  // as a crash; the forecast is its own point now.
  it('does not anchor the forecast to the last actual', () => {
    const last: EventMetricPoint = {
      bucket: '2026-01-02T10:00:00Z',
      count: 900,
      expected_count: 100,
      stddev: 10,
      is_anomaly: true,
      anomaly_direction: 'spike',
      z_score: 80,
    }
    const built = buildChartData(
      [last],
      [{ bucket: '2026-01-02T11:00:00Z', expected_count: 110, stddev: 10 }],
      4,
      true,
    )
    expect(at(built, 0).forecast_expected).toBeUndefined()
    expect(at(built, 0).forecast_band).toBeUndefined()
    expect(at(built, 1).forecast_expected).toBe(110)
  })
})

describe('buildChartData partial buckets (MO-5)', () => {
  const day = (bucket: string, count: number): EventMetricPoint => ({
    bucket,
    count,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
  })
  const days = [
    day('2026-01-01T00:00:00Z', 15),
    day('2026-01-02T00:00:00Z', 110),
    day('2026-01-03T00:00:00Z', 105),
    day('2026-01-04T00:00:00Z', 60),
  ]

  it('splits the partial first and last buckets onto the dashed series', () => {
    const built = buildChartData(days, [], 4, true, {
      first: '2026-01-01T14:00:00Z',
      last: '2026-01-04T18:00:00Z',
    })
    expect(built.map(point => point.solid_count)).toEqual([null, 110, 105, null])
    expect(built.map(point => point.partial_count)).toEqual([15, 110, 105, 60])
    expect(at(built, 0).partial_from).toBe('2026-01-01T14:00:00Z')
    expect(at(built, 3).partial_through).toBe('2026-01-04T18:00:00Z')
  })

  it('leaves the series alone without a partial window', () => {
    const built = buildChartData(days, [], 4, true)
    expect(built.every(point => point.solid_count === undefined && point.partial_count === undefined))
      .toBe(true)
  })
})

describe('ChartLegend (MO-1)', () => {
  it('lists only the marks the chart draws', () => {
    render(
      <ChartLegend color="red" expected band={2.5} anomaly partial={false} forecast={false} />,
    )
    const legend = screen.getByRole('list', { name: 'Chart legend' })
    expect(legend).toHaveTextContent('Actual')
    expect(legend).toHaveTextContent('Expected')
    expect(legend).toHaveTextContent('Normal range (±2.5σ)')
    expect(legend).toHaveTextContent('Anomaly')
    expect(legend).not.toHaveTextContent('Forecast')
    expect(legend).not.toHaveTextContent('Partial')
  })

  it('renders under MetricsChart only when asked', () => {
    const point: EventMetricPoint = {
      bucket: '2026-01-01T10:00:00Z',
      count: 10,
      expected_count: null,
      stddev: null,
      is_anomaly: false,
      anomaly_direction: null,
      z_score: null,
    }
    const { rerender } = render(<MetricsChart granularity="day" data={[point]} />)
    expect(screen.queryByTestId('chart-legend')).toBeNull()
    rerender(<MetricsChart granularity="day" data={[point]} legend />)
    expect(screen.getByTestId('chart-legend')).toHaveTextContent('Actual')
  })
})

describe('anomaly marks and tooltip lines (MON-17)', () => {
  it('says which way an anomaly moved and how far, in the tooltip', () => {
    render(
      <CustomTooltip
        active
        payload={[
          {
            value: 0,
            payload: {
              bucket: '2026-01-02T10:00:00Z',
              count: 0,
              expected_count: 10,
              stddev: 2,
              is_anomaly: true,
              anomaly_direction: 'drop',
              z_score: -5,
            },
          },
        ]}
        label="2026-01-02T10:00:00Z"
        granularity="day"
        seriesLabel="events"
      />,
    )

    // In words and against the expectation, not a z-score (MO-38).
    expect(screen.getByText(/Drop to zero$/)).toBeInTheDocument()
    expect(screen.queryByText(/z=/)).toBeNull()
  })

  it('states how far above expected a spike went', () => {
    render(
      <CustomTooltip
        active
        payload={[
          {
            value: 37,
            payload: {
              bucket: '2026-01-02T10:00:00Z',
              count: 37,
              expected_count: 32,
              stddev: 1,
              is_anomaly: true,
              anomaly_direction: 'spike',
              z_score: 5,
            },
          },
        ]}
        label="2026-01-02T10:00:00Z"
        granularity="day"
        seriesLabel="events"
      />,
    )

    expect(screen.getByText(/Spike, \+16% above expected$/)).toBeInTheDocument()
  })

  it('names the series an anomaly belongs to in the multi-series tooltip', () => {
    render(
      <MultiSeriesTooltip
        active
        payload={[
          {
            value: 40,
            dataKey: 'series_0',
            color: '#111111',
            name: 'ios',
            payload: {
              bucket: '2026-01-02T10:00:00Z',
              series_0: 40,
              series_0__anomaly: true,
              series_0__direction: 'spike',
              series_0__z: 6.25,
            },
          },
        ]}
        label="2026-01-02T10:00:00Z"
        granularity="day"
        seriesLabel="events"
      />,
    )

    expect(screen.getByText('ios anomaly: spike (z=6.3)')).toBeInTheDocument()
  })

  it('says a padded bucket has no data instead of reading it as zero', () => {
    render(
      <CustomTooltip
        active
        payload={[
          {
            value: 0,
            payload: { bucket: '2026-01-01T10:00:00Z', count: null, expected_count: null, stddev: null },
          },
        ]}
        label="2026-01-01T10:00:00Z"
        granularity="day"
        seriesLabel="events"
      />,
    )

    expect(screen.getByText('No data for this bucket')).toBeInTheDocument()
    expect(screen.queryByText(/0 events/)).toBeNull()
  })

  it('draws a spike as an up triangle and a drop as a down one', () => {
    const { container } = render(
      <svg>
        <AnomalyMark cx={10} cy={10} direction="spike" mini={false} />
        <AnomalyMark cx={30} cy={10} direction="drop" mini={false} />
      </svg>,
    )

    const marks = container.querySelectorAll('polygon[data-testid="anomaly-dot"]')
    expect(Array.from(marks).map((mark) => mark.getAttribute('data-direction'))).toEqual([
      'spike',
      'drop',
    ])
    expect(at(marks, 0).getAttribute('fill')).toBe('var(--danger)')
    expect(at(marks, 1).getAttribute('fill')).toBe('var(--warning)')
    // Tip above the centre for a spike, below it for a drop.
    expect(at(marks, 0).getAttribute('points')).toMatch(/^10,5 /)
    expect(at(marks, 1).getAttribute('points')).toMatch(/^30,15 /)
  })
})

describe('chart surface accessibility', () => {
  const point: EventMetricPoint = {
    bucket: '2026-01-01T10:00:00Z',
    count: 10,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
  }

  it('keeps the recharts surface out of the tab order and names the wrapper', () => {
    // The chart only mounts its ResponsiveContainer once the wrapper measures
    // a positive size, and jsdom reports 0×0 for everything.
    const rect = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockReturnValue({ width: 400, height: 200, x: 0, y: 0, top: 0, left: 0, right: 400, bottom: 200, toJSON: () => ({}) })

    composedChartProps.length = 0
    const { container } = render(<MetricsChart granularity="day" data={[point]} />)
    rect.mockRestore()

    // Recharts focuses its <svg class="recharts-surface"> by default, which
    // added an unnamed tab stop on every charted page (tripl-jfm3.67).
    expect(composedChartProps).not.toHaveLength(0)
    for (const props of composedChartProps) {
      expect(props.tabIndex).toBe(-1)
    }

    // The accessible content lives on the wrapper, which stays named.
    const wrapper = container.querySelector('[role="img"]')
    expect(wrapper).toHaveAttribute('aria-label')
  })
})

describe('chart summaries reach assistive tech (DS-25)', () => {
  const point: EventMetricPoint = {
    bucket: '2026-01-01T10:00:00Z',
    count: 10,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
  }
  const flagged: EventMetricPoint = {
    ...point,
    bucket: '2026-01-01T11:00:00Z',
    is_anomaly: true,
    anomaly_direction: 'spike',
    z_score: 5,
  }

  // role="img" makes its children presentational, so a summary inside it is
  // only read when the wrapper points at it.
  it('describes the multi-series chart with its series and anomalies', () => {
    render(
      <MetricsMultiSeriesChart
        granularity="hour"
        series={[
          { label: 'ios', data: [point, flagged] },
          { label: 'android', data: [point] },
        ]}
      />,
    )

    const chart = screen.getByRole('img', { name: 'events breakdown over time' })
    expect(chart).toHaveAccessibleDescription(/2 series: ios, android\./)
    expect(chart).toHaveAccessibleDescription(/ios: 1 anomaly detected/)
  })

  it('describes the mini chart', () => {
    render(<MiniMetricsChart data={[point, flagged]} label="Event volume trend" />)

    const chart = screen.getByRole('img', { name: 'Event volume trend' })
    expect(chart).toHaveAccessibleDescription('2 data points, 1 anomaly detected.')
  })
})

// DS-27: the mini chart mounted recharts inside zero-size containers.
describe('MiniMetricsChart container gate', () => {
  const point: EventMetricPoint = {
    bucket: '2026-01-01T10:00:00Z',
    count: 10,
    expected_count: null,
    stddev: null,
    is_anomaly: false,
    anomaly_direction: null,
    z_score: null,
  }

  it('waits for a measured size before mounting recharts', () => {
    composedChartProps.length = 0
    // jsdom measures every element as 0x0.
    render(<MiniMetricsChart data={[point]} />)
    expect(composedChartProps).toHaveLength(0)
  })

  it('mounts recharts once the container has a size', () => {
    const rect = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockReturnValue({ width: 200, height: 72, x: 0, y: 0, top: 0, left: 0, right: 200, bottom: 72, toJSON: () => ({}) })
    composedChartProps.length = 0
    render(<MiniMetricsChart data={[point]} />)
    rect.mockRestore()
    expect(composedChartProps).not.toHaveLength(0)
  })

  // The hook used to measure once on mount; the empty state carries no ref, so
  // a chart that first rendered with no data stayed blank once points arrived.
  it('mounts recharts when data arrives after an empty first render', () => {
    const rect = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockReturnValue({ width: 200, height: 72, x: 0, y: 0, top: 0, left: 0, right: 200, bottom: 72, toJSON: () => ({}) })
    composedChartProps.length = 0
    const { rerender } = render(<MiniMetricsChart data={[]} />)
    expect(screen.getByText('No recent events')).toBeInTheDocument()
    expect(composedChartProps).toHaveLength(0)

    rerender(<MiniMetricsChart data={[point]} />)
    expect(composedChartProps).not.toHaveLength(0)

    // …and again after going back to empty and returning.
    rerender(<MiniMetricsChart data={[]} />)
    composedChartProps.length = 0
    rerender(<MiniMetricsChart data={[point]} />)
    rect.mockRestore()
    expect(composedChartProps).not.toHaveLength(0)
  })
})

// DS-26: a single-event bucket read "1 events".
describe('tooltip nouns agree with the count', () => {
  it('says "1 event" for a one-event bucket', () => {
    render(
      <CustomTooltip
        active
        payload={[
          {
            value: 1,
            payload: { bucket: '2026-01-02T10:00:00Z', count: 1, expected_count: null, stddev: null },
          },
        ]}
        label="2026-01-02T10:00:00Z"
        granularity="day"
        seriesLabel={EVENTS_NOUN}
      />,
    )

    expect(screen.getByText('1 event')).toBeInTheDocument()
  })

  it('groups large counts in the app locale', () => {
    render(
      <MultiSeriesTooltip
        active
        payload={[{ value: 1234, dataKey: 'series_0', color: '#111111', name: 'ios' }]}
        label="2026-01-02T10:00:00Z"
        granularity="day"
        seriesLabel="events"
      />,
    )

    expect(screen.getByText('1,234 events')).toBeInTheDocument()
  })
})

describe('single-series default colour (DS-27)', () => {
  // Every colour-bearing prop in a rendered element tree.
  function colorsIn(node: unknown, out: Set<string> = new Set()): Set<string> {
    if (Array.isArray(node)) {
      node.forEach((child) => colorsIn(child, out))
      return out
    }
    if (!node || typeof node !== 'object' || !('props' in node)) return out
    const props = (node as ReactElement<Record<string, unknown>>).props
    for (const key of ['stroke', 'fill', 'stopColor']) {
      if (typeof props[key] === 'string') out.add(props[key] as string)
    }
    colorsIn(props.children, out)
    return out
  }

  it('is the first categorical slot, not the user accent', () => {
    expect(SINGLE_SERIES_COLOR).toBe(SERIES_COLORS[0])
    expect(SINGLE_SERIES_COLOR).not.toMatch(/--(accent|primary|chart-\d)\b/)
  })

  it('draws a MetricsChart without a color prop in that slot', () => {
    // jsdom measures 0×0, and the chart mounts only once its wrapper has a size.
    const rect = vi
      .spyOn(HTMLElement.prototype, 'getBoundingClientRect')
      .mockReturnValue({ width: 400, height: 200, x: 0, y: 0, top: 0, left: 0, right: 400, bottom: 200, toJSON: () => ({}) })
    composedChartProps.length = 0
    render(
      <MetricsChart
        granularity="day"
        data={[
          {
            bucket: '2026-01-01T10:00:00Z',
            count: 10,
            expected_count: null,
            stddev: null,
            is_anomaly: false,
            anomaly_direction: null,
            z_score: null,
          },
        ]}
      />,
    )
    rect.mockRestore()
    expect(composedChartProps).not.toHaveLength(0)
    const colors = colorsIn(at(composedChartProps, -1).children)
    expect(colors).toContain(SINGLE_SERIES_COLOR)
    expect([...colors].some((color) => /var\(--(accent|primary)\)/.test(color))).toBe(false)
  })
})
