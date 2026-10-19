import { useContext } from 'react'
import { History } from 'lucide-react'
import { ActiveProjectContext } from '@/components/active-project-context'
import { cn } from '@/lib/utils'
import { useDemoHistoryFill } from './useDemoHistoryFill'

/**
 * "History is still loading" — said where a demo's history is drawn (the
 * monitoring drilldown, the Overview volume card) while that history is
 * still being filled in.
 *
 * A demo opens with its last two days and fills in the rest afterwards, so
 * for the first minutes a 30-day chart is a short line at its right edge. The
 * banner says "filling in history", but it sits at the top of the page and
 * reads as status; next to the chart, a reader took the two-day line for the
 * demo's data and the empty stretch for an outage. This says it beside the
 * chart, and that the chart will move — the fill's own poller refetches the
 * series as each week lands (useDemoHistoryFill), so nothing here has to.
 *
 * A failed fill is said too: the demo is usable, only shorter than its
 * charts' range, and Reset is the way to the full history.
 *
 * Reads the shell's project from ActiveProjectContext and renders nothing
 * outside a demo, or once the fill is complete. `compact` sets it a step
 * smaller with tighter padding, for a card whose chart is the whole body; it
 * keeps the detail, since the lead alone does not say the chart will move.
 */
export function DemoHistoryFillNotice({
  compact = false,
  className,
}: {
  compact?: boolean
  className?: string
}) {
  const project = useContext(ActiveProjectContext)
  // A reader, not a poller: the banner polls for every demo surface.
  const { filling, failed, progress } = useDemoHistoryFill(project, { poll: false })
  if (!project?.is_demo || (!filling && !failed)) return null
  const lead = failed
    ? 'Older history could not be loaded.'
    : progress != null
      ? `History is still loading — ${progress}%.`
      : 'History is still loading…'
  const detail = failed
    ? 'The charts show the days this demo holds. Reset rebuilds the demo with its full history.'
    : 'This demo opened with its last 2 days. Older days fill in from the newest back, and the charts update as each week lands.'
  return (
    <div
      role="status"
      data-slot="demo-history-fill-notice"
      data-state={failed ? 'failed' : 'filling'}
      className={cn(
        'flex items-start gap-2 rounded-md bg-warning-soft text-fg-secondary',
        compact ? 'px-2.5 py-1.5 text-caption' : 'px-3 py-2 text-body-sm',
        className,
      )}
    >
      <History
        aria-hidden="true"
        className={cn('shrink-0 text-warning', compact ? 'mt-px h-3 w-3' : 'mt-[1px] h-3.5 w-3.5')}
      />
      <div className="min-w-0">
        <span className="font-medium text-fg">{lead}</span> <span>{detail}</span>
      </div>
    </div>
  )
}
