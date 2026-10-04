import { useQuery } from '@tanstack/react-query'

import { alertingApi } from '@/api/alerting'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { alertDeliveriesKey, alertInboxKey } from '@/lib/queryKeys'

/**
 * Keys for the two tab counts. Under the inbox and delivery-log prefixes, so
 * `invalidateAlertingConfig`, a triage action and the realtime stream refresh
 * them together with the lists they count — an Ack that left "Inbox 3" on the
 * tab would make the strip a signal nobody could trust.
 */
export const openIncidentCountKey = (slug: string) => [...alertInboxKey(slug), 'tabCount'] as const
export const failedDeliveryCountKey = (slug: string) =>
  [...alertDeliveriesKey(slug), 'failedCount'] as const

export interface AlertingTabCounts {
  /** Open incidents, every other filter off; undefined until answered. */
  openIncidents: number | undefined
  /** Failed deliveries in the whole log; undefined until answered. */
  failedDeliveries: number | undefined
}

/**
 * The unfiltered counts the Alerting tab strip carries: "Inbox 3" and
 * "Delivery log 2", so the strip itself is a triage signal.
 *
 * Neither can come from the section queries: those carry the reader's filters
 * and only run on their own section. Each is one page-of-one request that the
 * server answers with its `total`, and the failed count is exactly what the
 * log's own "Status: Failed" filter lists, so the number and the list it
 * leads to agree.
 */
export function useAlertingTabCounts(
  slug: string,
  { enabled, refetchInterval }: { enabled: boolean; refetchInterval: number | false },
): AlertingTabCounts {
  const openQuery = useQuery({
    meta: SILENT_ERROR_META,
    queryKey: openIncidentCountKey(slug),
    queryFn: () => alertingApi.listInbox(slug, { status: 'open', limit: 1 }),
    enabled,
    refetchInterval,
  })
  const failedQuery = useQuery({
    meta: SILENT_ERROR_META,
    queryKey: failedDeliveryCountKey(slug),
    queryFn: () => alertingApi.listDeliveries(slug, { status: 'failed', limit: 1 }),
    enabled,
    refetchInterval,
  })
  return {
    openIncidents: openQuery.data?.total,
    failedDeliveries: failedQuery.data?.total,
  }
}
