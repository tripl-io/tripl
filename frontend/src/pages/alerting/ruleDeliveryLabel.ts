import { formatRelativeTime } from '@/lib/datetime'
import { countOf } from '@/lib/plural'
import type { AlertDeliveryStatus } from '@/types'

/**
 * What the newest delivery did, worded to sit BEFORE its time.
 *
 * The row printed "last 19m ago · sent": a time with no noun beside a raw
 * status, on the same row as a "Last fired 55m ago" column. Leading with the
 * delivery's own verb is what tells the two times apart: "last sent" is when a
 * message went out, "Last fired" is when the rule's scope was last anomalous.
 * `sent` stays the word the Delivery log uses for the same status.
 */
const LAST_DELIVERY_WORDS: Record<AlertDeliveryStatus, string> = {
  sent: 'last sent',
  failed: 'last delivery failed',
  pending: 'last delivery queued',
}

/**
 * A rule's delivery health, for its row on the Rules list:
 * "115 deliveries · 57 incidents · last sent 3h ago", or "Never delivered".
 */
export function ruleDeliveryHealthLabel(
  rule: {
    total_deliveries: number
    incident_count: number
    last_delivery_at: string | null
    last_delivery_status: AlertDeliveryStatus | null
  },
  now?: number,
): string {
  if (rule.total_deliveries === 0) return 'Never delivered'
  const parts = [
    countOf(rule.total_deliveries, 'delivery', 'deliveries'),
    countOf(rule.incident_count, 'incident', 'incidents'),
  ]
  if (rule.last_delivery_at) {
    const words = rule.last_delivery_status
      ? LAST_DELIVERY_WORDS[rule.last_delivery_status]
      : 'last delivery'
    parts.push(`${words} ${formatRelativeTime(rule.last_delivery_at, now)}`)
  }
  return parts.join(' · ')
}
