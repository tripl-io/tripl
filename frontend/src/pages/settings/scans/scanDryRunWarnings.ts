import type { ScanDryRunResponse } from '@/types'

/**
 * More new events than this from one draft is almost never what the author
 * meant: it is the signature of a query whose leftover columns all went into
 * the event name.
 */
const MANY_NEW_EVENTS = 25

/**
 * A name built from more than this many `column=value` segments is a dump of
 * the row rather than a name (`event_name=… | button_id= | product_id= | …`).
 */
const MANY_NAME_SEGMENTS = 3

/** Which naming control a dry-run warning's link opens. */
export type NamingFixTarget = 'format' | 'groups'

export interface NameExplosion {
  /** New events the draft would add on its first run. */
  newEvents: number
  /**
   * Columns joined into the widest name, or 0 when the names are plain values
   * and it is the count alone that is too high.
   */
  columns: number
}

/**
 * Whether the draft would flood the plan with combinatorial event names.
 *
 * The default setup (a wide SELECT, events named from a column) made the dry
 * run answer "Would create 153 events", every one a pipe-joined dump of the
 * remaining columns, in the same neutral tone as a good answer, and one Run now
 * added all 153 to the plan (#247). Null when the answer looks sane.
 */
export function dryRunNameExplosion(dryRun: ScanDryRunResponse | null): NameExplosion | null {
  if (!dryRun || dryRun.sampled_rows === 0) return null
  const newEvents = dryRun.events.filter(event => event.status === 'new').length
  const columns = dryRun.events.reduce(
    (widest, event) => Math.max(widest, (event.name.match(/=/g) ?? []).length),
    0,
  )
  if (newEvents === 0) return null
  if (newEvents <= MANY_NEW_EVENTS && columns <= MANY_NAME_SEGMENTS) return null
  return { newEvents, columns: columns > MANY_NAME_SEGMENTS ? columns : 0 }
}
