import { worstFreshness } from '@/lib/sourceFreshness'
import type { SourceFreshnessItem } from '@/types'

/**
 * The scan whose freshness speaks for a data source on its card: the worst
 * reading among the scans reading it. Null when none of `items` reads it.
 */
export function dataSourceFreshness(
  dataSourceId: string,
  items: readonly SourceFreshnessItem[],
): SourceFreshnessItem | null {
  const reading = items.filter(item => item.data_source_id === dataSourceId)
  const worst = worstFreshness(reading.map(item => item.freshness))
  if (!worst) return null
  return reading.find(item => item.freshness === worst) ?? null
}
