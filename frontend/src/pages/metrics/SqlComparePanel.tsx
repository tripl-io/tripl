import { formatMetricValue } from '@/lib/metricFormat'
import type { MetricPreviewPoint } from '@/types'
import { compareSeries } from './sqlCompare'

/** One side of the comparison: its points, or why its preview did not run. */
export interface CompareSide {
  points: MetricPreviewPoint[]
  error: string | null
}

interface SqlComparePanelProps {
  saved: CompareSide
  draft: CompareSide
  unit: string
  /** Rows shown in the table; the summary always covers every bucket. */
  maxRows?: number
}

const PERCENT = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 })

function formatPct(value: number | null): string {
  return value === null ? '—' : `${value > 0 ? '+' : ''}${PERCENT.format(value)}%`
}

/**
 * Saved-vs-draft comparison over one preview window. A side that failed (bad
 * SQL, a warehouse error, or a timeout) is named by its label and the
 * comparison is withheld; nothing here saves anything.
 */
export function SqlComparePanel({ saved, draft, unit, maxRows = 50 }: SqlComparePanelProps) {
  // Each side's error already names it ("Saved definition: …", "Draft …").
  const failed = [saved.error, draft.error].filter((message): message is string => !!message)

  if (failed.length > 0) {
    return (
      <div role="alert" className="mt-[10px] rounded-card border px-4 py-3 text-body-sm bg-danger-soft text-danger">
        {failed.map(message => (
          <p key={message}>{message}</p>
        ))}
      </div>
    )
  }

  const summary = compareSeries(saved.points, draft.points)
  const format = (value: number | null) =>
    value === null ? '—' : formatMetricValue(value, unit.trim() || null)
  const shown = summary.rows.slice(-maxRows)

  return (
    <div role="status" className="mt-[10px] rounded-card border px-4 py-3 border-border">
      <p className="mono mb-[6px] text-body-sm text-fg">
        saved {format(summary.savedTotal)} · draft {format(summary.draftTotal)} · change{' '}
        {format(summary.netAbsChange)} ({formatPct(summary.netPctChange)})
      </p>
      <p className="mb-[8px] text-body-sm text-fg-secondary">
        {summary.comparedCount} buckets compared
        {summary.missingCount > 0
          ? `, ${summary.missingCount} missing on one side (shown as —, not zero)`
          : ''}
        . A change of — means it is undefined: a missing side or a zero saved baseline.
      </p>
      <table className="mono w-full text-body-sm">
        <thead>
          <tr className="text-fg-secondary">
            <th className="text-left font-normal">Bucket</th>
            <th className="text-right font-normal">Saved</th>
            <th className="text-right font-normal">Draft</th>
            <th className="text-right font-normal">Change</th>
            <th className="text-right font-normal">%</th>
          </tr>
        </thead>
        <tbody>
          {shown.map(row => (
            <tr key={row.bucketMs}>
              <td>{new Date(row.bucketMs).toISOString().slice(0, 16).replace('T', ' ')}</td>
              <td className="text-right">{format(row.saved)}</td>
              <td className="text-right">{format(row.draft)}</td>
              <td className="text-right">{format(row.absChange)}</td>
              <td className="text-right">{formatPct(row.pctChange)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
