import { Clock } from 'lucide-react'
import { Link } from 'react-router-dom'
import { formatLag } from '@/lib/sourceFreshness'
import { cn } from '@/lib/utils'
import type { SourceFreshnessItem } from '@/types'

/**
 * "Data late — drop signals held" (F16, #269). Shown where volume signals are
 * read (Anomalies, the monitoring drilldown) while a scan feeding them is late
 * or overdue: the worker holds its drop anomalies rather than raising one per
 * scope for what is really one delayed load, so the page is quieter than the
 * data looks, and this says why.
 *
 * Renders nothing when no scan is holding. `items` should already be the
 * holding ones (`holdingItems`).
 */
export function SignalsHeldNotice({
  slug,
  items,
  className,
}: {
  slug: string
  items: readonly SourceFreshnessItem[]
  className?: string
}) {
  if (items.length === 0) return null
  const overdue = items.every(item => item.freshness.status === 'overdue')
  return (
    <div
      role="status"
      data-slot="signals-held-notice"
      className={cn(
        'flex items-start gap-2 rounded-md px-3 py-2 text-body-sm bg-warning-soft text-fg-secondary',
        className,
      )}
    >
      <Clock aria-hidden="true" className="mt-[1px] h-3.5 w-3.5 shrink-0 text-warning" />
      <div className="min-w-0">
        <span className="font-medium text-fg">
          {overdue ? 'Scan overdue' : 'Data late'} — drop signals held.
        </span>{' '}
        <span>
          New drops are not raised for{' '}
          {items.map((item, index) => (
            <span key={item.id}>
              {index > 0 && (index === items.length - 1 ? ' and ' : ', ')}
              <Link to={`/p/${slug}/scans/${item.id}`} className="no-underline hover:underline text-fg">
                {item.name}
              </Link>
              {item.freshness.status === 'late' && item.freshness.lag_seconds != null && (
                <> (newest event {formatLag(item.freshness.lag_seconds)} ago)</>
              )}
              {item.freshness.status === 'overdue' && <> (scan overdue)</>}
            </span>
          ))}{' '}
          until data arrives; they are scored then, nothing is lost.
        </span>
      </div>
    </div>
  )
}
