import { useQueries } from '@tanstack/react-query'
import { FreshnessChip } from '@/components/source-freshness/freshness-chip'
import { sourceFreshnessQueryOptions } from '@/lib/queryKeys'
import { dataSourceFreshness } from './data-source-freshness-model'
import type { DataSource } from '@/types'

/**
 * The card's freshness pill (F16, #269): the worst freshness among the scans
 * reading this source, named after that scan. Data sources are workspace-wide
 * and freshness is read per project, so this asks each project the card's
 * scans live in — the same cached query the Overview and the scan pages use,
 * so cards sharing a project share one request. Nothing while loading, when
 * every scan is fresh, or when the source feeds no scan.
 */
export function DataSourceFreshnessChip({ ds }: { ds: DataSource }) {
  const slugs = [...new Set((ds.scans ?? []).map(scan => scan.project_slug))]
  const results = useQueries({
    queries: slugs.map(slug => sourceFreshnessQueryOptions(slug)),
  })
  const worst = dataSourceFreshness(
    ds.id,
    results.flatMap(result => result.data ?? []),
  )
  if (!worst) return null
  return <FreshnessChip freshness={worst.freshness} name={worst.name} />
}
