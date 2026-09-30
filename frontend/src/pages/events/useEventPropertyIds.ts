import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { propertyEntriesApi } from '@/api/propertyEntries'
import { eventPropertiesKey } from '@/lib/queryKeys'

/** The ids of the properties on an event's list (F23): what `PropertyDriftList`
 *  needs to show their type changes. Shares `EventPropertiesGrid`'s query and cache. */
export function useEventPropertyIds(
  slug: string | undefined,
  branchId: string | null,
  eventId: string | undefined,
): string[] | undefined {
  const { data } = useQuery({
    queryKey: eventPropertiesKey(slug, branchId, eventId ?? ''),
    queryFn: () => propertyEntriesApi.forEvent(slug!, eventId!, branchId),
    enabled: !!slug && !!eventId,
  })
  return useMemo(() => data?.map((entry) => entry.variable_id), [data])
}
