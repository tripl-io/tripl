/**
 * "Seen with 2 values in the scan of Oct 5, 2026: map/main (62%), spot/main
 * (38%)" — the values the last observing scan saw for one field, when the
 * breakdown rows behind the event disagreed.
 *
 * An event stores one value per field, the busiest row's, and an analyst read
 * that one value as "this event fires only on map/main". The scan had seen the
 * rest and said so only in its run report. This line says it where the value
 * is read: on the edit form under the box, and under the value in the event
 * page's Fields table.
 *
 * Counts over time are not this line's job — that is the Breakdowns tab, which
 * "Split volume by this field" leads to. This is one scan's snapshot, dated.
 */
import { useState } from 'react'
import type { EventFieldObservedValues, ObservedFieldValue } from '@/types'
import { formatDate } from '@/lib/datetime'

/** Values shown before "+N more". */
const COLLAPSED_COUNT = 3

function formatShare(share: number | null): string | null {
  if (share === null) return null
  if (share > 0 && share < 0.005) return '<1%'
  return `${Math.round(share * 100)}%`
}

/** A NULL column reaches the tally as `''`; it is a real share of the rows,
 *  so it is listed, under a name that can be seen. */
function ValueText({ value }: { value: string }) {
  if (value === '') return <span className="italic">(empty)</span>
  return <span className="mono">{value}</span>
}

function ValueItem({ item, stored }: { item: ObservedFieldValue; stored: boolean }) {
  const share = formatShare(item.share)
  return (
    <span>
      <ValueText value={item.value} />
      {(share || stored) && (
        <span>
          {' ('}
          {[share, stored ? 'stored' : null].filter(Boolean).join(', ')}
          {')'}
        </span>
      )}
    </span>
  )
}

export function FieldObservedValues({
  observed,
  stored = null,
  onMain = false,
  compact = false,
}: {
  observed: EventFieldObservedValues | null | undefined
  /** The event's saved value for this field. Marked "stored" in the list only
   *  when a scan keeps it — a hand-set value is not one the scan chose. */
  stored?: { value: string; isAuthored: boolean } | null
  /** On a branch: the distribution is main's, read through the main twin. */
  onMain?: boolean
  /** The read view's caption-size line, without the expander. */
  compact?: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  if (!observed || observed.distinct_count <= 1 || observed.values.length === 0) return null

  const storedValue = stored && !stored.isAuthored ? stored.value : null
  const shown = expanded || compact ? observed.values : observed.values.slice(0, COLLAPSED_COUNT)
  const hiddenCount = observed.distinct_count - shown.length
  // What the expander opens: the kept values not listed yet. Past the cap
  // there is only a count, said after them as "and N more".
  const expandable = observed.values.length - shown.length
  const notKept = observed.distinct_count - observed.values.length
  const date = formatDate(observed.observed_at)
  const when = date ? ` in the scan of ${date}` : ' in the last scan'
  const where = onMain ? ' on main' : ''

  if (compact) {
    return (
      <p
        className="mt-[2px] text-caption text-fg-tertiary"
        title={`Values the scan saw for this field${where}${when}`}
        data-testid="field-observed-values"
      >
        Seen with {observed.distinct_count} values:{' '}
        {observed.values.slice(0, COLLAPSED_COUNT).map((item, i) => (
          <span key={item.value}>
            {i > 0 && ' · '}
            <ValueText value={item.value} />
            {formatShare(item.share) && ` ${formatShare(item.share)}`}
          </span>
        ))}
        {observed.distinct_count > COLLAPSED_COUNT
          && ` · +${observed.distinct_count - COLLAPSED_COUNT} more`}
      </p>
    )
  }

  return (
    <p className="mt-1 text-body-sm text-fg-tertiary" data-testid="field-observed-values">
      Seen with {observed.distinct_count} values{where}{when}:{' '}
      {shown.map((item, i) => (
        <span key={item.value}>
          {i > 0 && ', '}
          <ValueItem item={item} stored={item.value === storedValue} />
        </span>
      ))}
      {hiddenCount > 0 && (
        <>
          {' '}
          {/* Only the kept values can be expanded to; past the cap the line
              can say how many there were, not what they were. */}
          {!expanded && expandable > 0 ? (
            <>
              <button
                type="button"
                onClick={() => setExpanded(true)}
                className="underline underline-offset-2 hover:text-foreground"
              >
                +{expandable} more
              </button>
              {notKept > 0 && <span>{` · and ${notKept} more not kept`}</span>}
            </>
          ) : (
            <span>and {hiddenCount} more</span>
          )}
        </>
      )}
    </p>
  )
}
