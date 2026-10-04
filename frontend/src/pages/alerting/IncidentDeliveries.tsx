import { useMemo } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'

import { alertingApi } from '@/api/alerting'
import { Button } from '@/components/ui/button'
import { countOf } from '@/lib/plural'
import { getErrorMessage } from '@/lib/utils'

import { AlertDeliveryRow, DeliveryTable } from './AlertDeliveryRow'
import { listPageRequest, nextListPageParam, type ListPageParam } from './listPaging'
import { incidentDeliveriesKey } from '@/lib/queryKeys'

/** One page of an incident's deliveries — the endpoint's own default. */
const INCIDENT_DELIVERY_PAGE_SIZE = 50

/**
 * The deliveries of ONE incident, shown inside its card.
 *
 * The incident is a property of the delivery ITEM, not of the delivery — a
 * single message can carry rows from several incidents — so this asks the API
 * for deliveries having an item in this group rather than filtering a list the
 * page already holds.
 *
 * Fetched only while expanded: a project with a long incident list would
 * otherwise fire one request per card on mount.
 *
 * Paged. It used to ask for 50 and ignore `total`, so a long-running incident
 * with 120 deliveries showed 50 under a toggle promising all 120.
 */
export function IncidentDeliveries({
  slug,
  correlationGroupId,
  focusDeliveryId,
  focusItemKey,
}: {
  slug: string
  correlationGroupId: string
  focusDeliveryId?: string
  focusItemKey?: string
}) {
  const query = useInfiniteQuery({
    queryKey: incidentDeliveriesKey(slug, correlationGroupId),
    queryFn: ({ pageParam }) =>
      alertingApi.listDeliveries(slug, {
        correlation_group_id: correlationGroupId,
        limit: INCIDENT_DELIVERY_PAGE_SIZE,
        ...listPageRequest(pageParam),
      }),
    initialPageParam: 0 as ListPageParam,
    getNextPageParam: nextListPageParam,
  })
  const { data, isLoading, isError, error } = query
  // De-duplicated by id. Pages continue by cursor, so a new delivery can no
  // longer shift a row into two pages; this stays for a fallback offset page
  // and because the same row twice is a React key collision.
  const items = useMemo(() => {
    const seen = new Set<string>()
    return (data?.pages ?? []).flatMap(page =>
      page.items.filter(item => {
        if (seen.has(item.id)) return false
        seen.add(item.id)
        return true
      }),
    )
  }, [data])
  const total = data?.pages[0]?.total ?? 0

  if (isLoading) {
    return <p className="mt-2 text-micro text-fg-tertiary">Loading deliveries…</p>
  }

  // A failed request must not read as "nothing was sent". They are opposite
  // facts about an incident someone is deciding whether to acknowledge, and the
  // empty state would state the reassuring one on no evidence.
  if (isError) {
    return (
      <p role="alert" className="mt-2 text-micro text-destructive">
        Could not load deliveries: {getErrorMessage(error)}
      </p>
    )
  }

  if (items.length === 0) {
    return (
      <p className="mt-2 text-micro text-fg-tertiary">
        No delivery recorded for this incident.
      </p>
    )
  }

  return (
    <div className="mt-2 space-y-2">
    <div className="overflow-x-auto rounded-sm border">
      <DeliveryTable>
        {items.map(delivery => (
          <AlertDeliveryRow
            key={delivery.id}
            slug={slug}
            delivery={delivery}
            focusDeliveryId={focusDeliveryId}
            focusItemKey={focusItemKey}
          />
        ))}
      </DeliveryTable>
    </div>
    {total > items.length && (
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-micro text-fg-tertiary">
          Showing {items.length} of {countOf(total, 'delivery', 'deliveries')}.
        </p>
        {query.hasNextPage && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-9 px-3 text-body-sm sm:h-7 sm:px-2 sm:text-caption"
            disabled={query.isFetchingNextPage}
            onClick={() => void query.fetchNextPage()}
          >
            {query.isFetchingNextPage ? 'Loading…' : 'Load older deliveries'}
          </Button>
        )}
      </div>
    )}
    </div>
  )
}
