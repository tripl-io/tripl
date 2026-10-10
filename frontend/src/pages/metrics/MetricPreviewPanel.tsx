import { Sparkline } from '@/components/primitives/sparkline'
import { formatMetricValue } from '@/lib/metricFormat'
import type { MetricPreviewResponse } from '@/types'
import { countOf } from '@/lib/plural'

interface MetricPreviewPanelProps {
  result: MetricPreviewResponse
  color: string
  unit: string
  /**
   * `sql` names the columns the query projected and reads an empty result as a
   * query problem; `series` is a fact or event metric's dry run, which
   * has no columns of its own to name.
   */
  variant?: 'sql' | 'series'
}

const EMPTY_GUIDANCE = {
  sql: 'The query ran but returned no rows in the preview window. Check its WHERE clause and time range, and that the time column holds recent timestamps.',
  series:
    'No bucket in the preview window has a value. Check the filters, and that the source has recent rows; a ratio whose denominator is zero everywhere shows nothing too.',
} as const

const SINGLE_GUIDANCE = {
  sql: 'Only one bucket came back, so there is no trend to draw. Make sure the query groups by the time column.',
  series: 'Only one bucket has a value, so there is no trend to draw yet.',
} as const

/**
 * Compact result panel for a metric dry run. Expected user mistakes (bad SQL,
 * missing columns, warehouse errors) arrive as a 200 with `error` set and
 * render in the standard danger style; a successful run renders a chart that
 * follows the panel's width, the value range, and a mono summary line — or
 * says what an empty or one-point result most likely means.
 */
export function MetricPreviewPanel({ result, color, unit, variant = 'sql' }: MetricPreviewPanelProps) {
  if (result.error) {
    return (
      <div
        role="alert"
        className="mt-[10px] rounded-card border px-4 py-3 text-body-sm bg-danger-soft text-danger"
        style={{
          borderColor: 'color-mix(in oklab, var(--danger) 35%, var(--border))',
        }}
      >
        {result.error}
      </div>
    )
  }
  const points = result.points ?? []
  const columns = result.columns ?? []
  const values = points.map(p => p.value)
  const lastValue = values[values.length - 1]
  const summary =
    variant === 'sql'
      ? `${result.point_count} buckets · columns: ${columns.join(', ')}${result.truncated ? ' · truncated' : ''}`
      : countOf(result.point_count, 'bucket', 'buckets')
  const guidance =
    points.length === 0 ? EMPTY_GUIDANCE[variant] : points.length === 1 ? SINGLE_GUIDANCE[variant] : null
  const format = (value: number) => formatMetricValue(value, unit.trim() || null)
  return (
    <div
      role="status"
      className="mt-[10px] rounded-card border px-4 py-3 border-border"
    >
      {points.length > 1 && (
        <div className="mb-[8px]">
          <Sparkline data={values} color={color} width={560} height={64} responsive />
        </div>
      )}
      {lastValue !== undefined && (
        <p className="mono mb-[4px] text-body-sm text-fg">
          min {format(Math.min(...values))} · max {format(Math.max(...values))} · last{' '}
          {format(lastValue)}
        </p>
      )}
      {guidance && (
        <p className="mb-[4px] text-body-sm text-fg-secondary">
          {guidance}
        </p>
      )}
      <p className={`${variant === 'sql' ? 'mono ' : ''}text-body-sm text-fg-secondary`}>
        {summary}
      </p>
    </div>
  )
}
