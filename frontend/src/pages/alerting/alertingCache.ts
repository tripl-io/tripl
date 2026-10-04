import type { QueryClient } from '@tanstack/react-query'
import {
  alertDeliveriesAnyKey,
  alertDeliveriesKey,
  alertDestinationsKey,
  alertInboxKey,
  monitorsSummaryKey,
} from '@/lib/queryKeys'

/**
 * Refetch every alerting view a destination/rule/delivery write can change.
 *
 * Covers five query families:
 *
 *  - `['alertDestinations', slug]` — the destination cards and their rules.
 *  - `['alertInbox', slug]` — the incident queue.
 *  - `['alertDeliveries', slug]` — the delivery log, and the per-incident
 *    delivery lists inside it.
 *  - `['alertDeliveriesAny', slug]` — the unfiltered "has this project EVER
 *    delivered" probe that decides whether the page collapses into guided setup.
 *  - `['monitors-summary', slug]` — the routing summary above the cards, which
 *    counts the same rules.
 *
 * A CONFIGURATION write invalidates INCIDENT data because the two are the same
 * rows: `AlertDelivery.destination_id` and `.rule_id` are both ON DELETE CASCADE
 * and the inbox INNER JOINs through the deliveries, so deleting one rule deletes
 * every message it sent and every incident group assembled from them. Every
 * mutation on this page used to invalidate `['alertDestinations', slug]` and
 * nothing else, and with a 60s `staleTime` (main.tsx) that meant deleting a rule
 * and switching to the Inbox inside a minute listed incidents that no longer
 * existed — where every button 404s through `_get_or_create_correlation_state`.
 *
 * One helper rather than a copy at each of the nine call sites: the previous
 * shape was nine hand-kept copies of one key, which is how eight of them came to
 * be missing the other four.
 */
export function invalidateAlertingConfig(qc: QueryClient, slug: string): void {
  qc.invalidateQueries({ queryKey: alertDestinationsKey(slug) })
  qc.invalidateQueries({ queryKey: alertInboxKey(slug) })
  qc.invalidateQueries({ queryKey: alertDeliveriesKey(slug) })
  qc.invalidateQueries({ queryKey: alertDeliveriesAnyKey(slug) })
  qc.invalidateQueries({ queryKey: monitorsSummaryKey(slug) })
}
