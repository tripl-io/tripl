import { useQuery } from '@tanstack/react-query'
import { eventTypeOwnersApi } from '@/api/eventTypeOwners'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { projectEventTypeOwnersKey } from '@/lib/queryKeys'
import type { EventTypeOwner } from '@/types'
import { ownersOfType } from './eventOwner'

/**
 * The owners of one event type, read from the project's owner list: the one
 * request, under the same key, that the event-type list makes, so either page
 * fills the other's cache and an owner change refreshes both. `owners` is
 * undefined until the list answers, and stays so if it fails: the caller then
 * says nothing about the type's owners rather than "none". Silent on failure,
 * since every caller has a plain answer to fall back to.
 */
export function useEventTypeOwners(
  slug: string,
  eventTypeId: string | null | undefined,
  { enabled = true }: { enabled?: boolean } = {},
): { owners: EventTypeOwner[] | undefined; loading: boolean } {
  const query = useQuery({
    queryKey: projectEventTypeOwnersKey(slug),
    queryFn: () => eventTypeOwnersApi.listForProject(slug),
    select: (owners: EventTypeOwner[]) => ownersOfType(owners, eventTypeId ?? ''),
    enabled: enabled && Boolean(slug) && Boolean(eventTypeId),
    meta: SILENT_ERROR_META,
  })
  return { owners: query.data, loading: query.isLoading }
}
