/**
 * Grouping helper for near-identical, scan-generated event names.
 *
 * Auto-generated event names tend to cluster by a shared prefix and differ only
 * in their final delimiter-separated segment, e.g.
 *   page_value_question_..._sail_navigation_yes_selected
 *   page_value_question_..._sail_navigation_yes_clicked
 * both collapse to the prefix `page_value_question_..._sail_navigation_yes`.
 *
 * Grouping those events lets a cluster be scanned/triaged as one unit instead of
 * scrolling past dozens of near-duplicate rows. This module is pure and has no
 * React/DOM dependencies so it can be unit-tested in isolation.
 */

/** Minimum number of events that must share a prefix before it forms a cluster. */
export const MIN_GROUP_SIZE = 3

/**
 * Stricter bar for the events table's cluster banner. Any three names sharing
 * one segment (`checkout:started|completed|failed`) formed a cluster, so the
 * banner showed on almost every catalog and stopped meaning "these look
 * machine-generated".
 */
export const TABLE_CLUSTER_MIN_SIZE = 5

/** A prefix of two or more segments — `a_b`, not just `a`. */
export function isDeepPrefix(prefix: string): boolean {
  return /[_:/]/.test(prefix)
}

/** A cluster of events whose names share a common prefix. */
export interface EventNameGroup {
  /** Shared prefix: the name with its final delimiter-separated segment removed. */
  prefix: string
  /** Ids of the events in the cluster, in original input order. */
  eventIds: string[]
  /** Size of the cluster (always >= the configured minimum group size). */
  count: number
}

/** Result of {@link groupEventNames}: the clusters plus the leftover events. */
export interface EventNameGroupResult<T> {
  /** Clusters, sorted by count descending then prefix ascending (stable). */
  groups: EventNameGroup[]
  /** Events that did not fall into any cluster, in original input order. */
  ungrouped: T[]
}

/** Minimal shape the grouping helper needs from an event. */
export interface NameGroupInput {
  id: string
  name: string
}

const isDelimiter = (ch: string): boolean => ch === '_' || ch === ':' || ch === '/'

/**
 * Derive the grouping prefix for a single event name by stripping the final
 * delimiter-separated segment. Splits on the *last* run of delimiter characters
 * (`_`, `:`, `/`) so `a::b` and `a/b/c` behave like `a_b`.
 *
 * Returns `null` when there is no interior delimiter to split on (a single
 * segment, or only a leading/trailing delimiter), meaning the name cannot be
 * clustered by prefix.
 */
export function eventNamePrefix(name: string): string | null {
  let i = name.length - 1
  // 1. Skip any trailing delimiter run — it has no suffix segment after it.
  while (i >= 0 && isDelimiter(name.charAt(i))) i -= 1
  // 2. Skip the final (suffix) segment.
  while (i >= 0 && !isDelimiter(name.charAt(i))) i -= 1
  // 3. Skip the delimiter run separating the prefix from the suffix.
  while (i >= 0 && isDelimiter(name.charAt(i))) i -= 1

  const prefixEnd = i + 1
  // A non-positive boundary means no interior delimiter or an empty prefix.
  if (prefixEnd <= 0) return null
  return name.slice(0, prefixEnd)
}

/**
 * Cluster events by their shared name prefix. Only prefixes shared by at least
 * `minGroupSize` events become groups; everything else is returned untouched in
 * `ungrouped`, preserving the original input order in both cases.
 */
export function groupEventNames<T extends NameGroupInput>(
  events: readonly T[],
  minGroupSize: number = MIN_GROUP_SIZE,
): EventNameGroupResult<T> {
  const byPrefix = new Map<string, T[]>()
  for (const ev of events) {
    const prefix = eventNamePrefix(ev.name)
    if (prefix === null) continue
    const bucket = byPrefix.get(prefix)
    if (bucket) bucket.push(ev)
    else byPrefix.set(prefix, [ev])
  }

  const groups: EventNameGroup[] = []
  const groupedIds = new Set<string>()
  for (const [prefix, bucket] of byPrefix) {
    if (bucket.length < minGroupSize) continue
    for (const ev of bucket) groupedIds.add(ev.id)
    groups.push({ prefix, eventIds: bucket.map((ev) => ev.id), count: bucket.length })
  }

  // Largest, most-worth-triaging clusters first; prefix as a stable tiebreak.
  groups.sort((a, b) => b.count - a.count || a.prefix.localeCompare(b.prefix))
  const ungrouped = events.filter((ev) => !groupedIds.has(ev.id))
  return { groups, ungrouped }
}
