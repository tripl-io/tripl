import { useQuery } from '@tanstack/react-query'
import { sourceFreshnessQueryOptions } from '@/lib/queryKeys'
import type { SourceFreshnessItem } from '@/types'

const NO_ITEMS: readonly SourceFreshnessItem[] = []

/**
 * Every scan's source freshness for a project (F16, #269), or an empty list
 * while it loads or when the read fails: freshness is a hint over a page, never
 * the page itself.
 */
export function useSourceFreshness(slug: string | undefined): readonly SourceFreshnessItem[] {
  const query = useQuery({
    ...sourceFreshnessQueryOptions(slug ?? ''),
    enabled: !!slug,
  })
  return query.data ?? NO_ITEMS
}
