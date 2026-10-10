import { formatPresence, formatThreshold } from '@/lib/propertyEntries'

/**
 * How often an event carries a property, and whether that disagrees with the
 * entry's Required flag. The event's property grid and the property's Events
 * tab show the same entry from either side, so they read it through this one
 * cell. Plain fields, because the event grid knows one threshold for all its
 * rows while each row of the property's tab carries its own event's.
 */
export function PropertyPresenceCell({
  presenceRate,
  required,
  suggestedRequired,
  threshold,
}: {
  presenceRate: number | null
  required: boolean
  /** Whether presence reaches the threshold; null when no scan measured it. */
  suggestedRequired: boolean | null
  /** The event's required threshold; null: the default. */
  threshold: number | null
}) {
  const below = required && suggestedRequired === false
  const looksRequired = !required && suggestedRequired === true
  return (
    <span
      className="tabular-nums"
      title={
        presenceRate === null
          ? 'No scan has measured this yet.'
          : `Carried by ${formatPresence(presenceRate)} of the event's rows at the last scan; the required threshold is ${formatThreshold(threshold)}.`
      }
    >
      {formatPresence(presenceRate)}
      {below && <span className="ml-1.5 text-caption text-warning">below threshold</span>}
      {looksRequired && <span className="ml-1.5 text-caption text-fg-tertiary">looks required</span>}
    </span>
  )
}
