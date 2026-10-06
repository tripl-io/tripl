import { Link } from 'react-router-dom'
import { Copy } from 'lucide-react'
import type { EventDuplicate } from './duplicateEvent'

/**
 * The line under a duplicate's title: which event it was filled in from, and
 * exactly what did and did not come along. Nothing is copied silently — the
 * presence threshold has no control on this form, so it is named here, and a
 * value the active plan has no field for is listed rather than lost.
 */
export function DuplicateNotice({
  duplicate,
  sourceHref,
}: {
  duplicate: EventDuplicate
  /** The source's own page, on the plan it was opened on. */
  sourceHref: string
}) {
  const { source, seed, dropped } = duplicate
  const threshold = seed.requiredPresenceThreshold
  return (
    <div
      role="note"
      data-slot="duplicate-notice"
      className="mb-[18px] flex items-start gap-2.5 rounded-card border border-border bg-bg-sunken px-3 py-2.5 text-body-sm text-fg-secondary"
    >
      <Copy className="mt-0.5 size-3.5 shrink-0 text-fg-tertiary" aria-hidden="true" />
      <div className="min-w-0 flex-1 space-y-1">
        <p>
          Duplicating{' '}
          <Link to={sourceHref} className="font-medium text-foreground underline underline-offset-2">
            {source.name}
          </Link>
          . Status starts as Draft, and the copied field values are authored on the new
          event, so scans do not overwrite them.
          {threshold != null && (
            <> The required presence threshold ({Math.round(threshold * 100)}%) is copied too.</>
          )}{' '}
          The property list and the discussion are not copied.
        </p>
        {dropped.length > 0 && (
          <p className="text-warning">
            Not copied, because no field on this plan can hold the value as stored:{' '}
            {dropped.join(', ')}.
          </p>
        )}
      </div>
    </div>
  )
}
