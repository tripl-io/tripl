import type { QueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

import { alertingApi } from '@/api/alerting'
import { alertDeliveryKey } from '@/lib/queryKeys'

import { invalidateAlertingConfig } from './alertingCache'

export interface RetryWatchOptions {
  /** Wait between two looks at the delivery. */
  intervalMs?: number
  /** How many looks before giving up quietly; 0 turns the watch off. */
  attempts?: number
}

const DEFAULT_INTERVAL_MS = 4_000
// ~2 minutes: the worker picks a re-queued delivery up within seconds, and a
// send that has not settled by then is better read off the log than toasted.
const DEFAULT_ATTEMPTS = 30

/**
 * Follow a re-queued delivery until the worker has tried it, then say how that
 * went. The retry endpoint only re-queues, so "Retry queued" is all
 * its response can promise; the outcome — delivered, or "Still failing: <why>"
 * — arrives later, and without this the reader had to reopen the row to learn
 * the retry had failed again.
 *
 * Deliberately NOT tied to the row's lifetime: the refetch a retry triggers can
 * move the row off a Status=Failed page, which is exactly when the reader most
 * needs to hear it failed. Bounded by `attempts`, and a failed look ends the
 * watch silently — the row and the log still show the truth.
 *
 * Returns a cancel function.
 */
export function watchRetriedDelivery(
  qc: QueryClient,
  slug: string,
  deliveryId: string,
  destinationName: string,
  { intervalMs = DEFAULT_INTERVAL_MS, attempts = DEFAULT_ATTEMPTS }: RetryWatchOptions = {},
): () => void {
  if (attempts <= 0) return () => {}
  let cancelled = false
  let timer: ReturnType<typeof setTimeout> | undefined

  const look = async (left: number) => {
    if (cancelled) return
    let detail
    try {
      detail = await alertingApi.getDelivery(slug, deliveryId)
    } catch {
      return
    }
    if (cancelled) return
    qc.setQueryData(alertDeliveryKey(slug, deliveryId), detail)
    if (detail.status === 'sent') {
      invalidateAlertingConfig(qc, slug)
      toast.success(`Delivered — ${destinationName} accepted the retried alert.`)
      return
    }
    if (detail.status === 'failed') {
      invalidateAlertingConfig(qc, slug)
      toast.error(`Still failing: ${detail.error_message ?? `${destinationName} refused it again.`}`)
      return
    }
    if (left > 1) timer = setTimeout(() => void look(left - 1), intervalMs)
  }

  timer = setTimeout(() => void look(attempts), intervalMs)
  return () => {
    cancelled = true
    clearTimeout(timer)
  }
}
