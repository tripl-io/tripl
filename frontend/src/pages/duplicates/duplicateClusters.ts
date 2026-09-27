import type { DuplicateCluster, DuplicateClusterEvent } from '@/types'

/** A stable key for a cluster: its members, order-independent. */
export function clusterKey(cluster: DuplicateCluster): string {
  return cluster.events
    .map(event => event.id)
    .sort()
    .join('|')
}

/**
 * The clusters of every loaded page, each once: the first occurrence of a
 * `clusterKey` wins, the order is otherwise kept.
 */
export function uniqueClusters(clusters: readonly DuplicateCluster[]): DuplicateCluster[] {
  const seen = new Set<string>()
  return clusters.filter(cluster => {
    const key = clusterKey(cluster)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

/** Statuses an event keeps its place in the plan with, best first. */
const KEEP_PREFERENCE: Record<string, number> = {
  implemented: 0,
  live: 0,
  ready_for_dev: 1,
  in_review: 2,
  draft: 3,
}

/**
 * Which member the page proposes to keep: the one with the most traffic over
 * the last seven days, then the most advanced status, then the first listed.
 * A proposal only — the reader picks another with one click.
 */
export function proposedKeeper(cluster: DuplicateCluster): DuplicateClusterEvent | undefined {
  let best: DuplicateClusterEvent | undefined
  for (const event of cluster.events) {
    if (!best) {
      best = event
      continue
    }
    const volume = event.volume_7d
    const bestVolume = best.volume_7d
    if (volume !== bestVolume) {
      if (volume > bestVolume) best = event
      continue
    }
    const rank = KEEP_PREFERENCE[event.status] ?? 9
    const bestRank = KEEP_PREFERENCE[best.status] ?? 9
    if (rank < bestRank) best = event
  }
  return best
}

/** "1,234 in 7 days", or what to say when nothing arrived. */
export function volumeLabel(volume: number): string {
  if (volume <= 0) return 'no data in 7 days'
  return `${volume.toLocaleString()} in 7 days`
}
