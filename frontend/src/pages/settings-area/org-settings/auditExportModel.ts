import type { AuditDeliveryStatus } from '@/api/auditExport'

/**
 * The pure half of the audit export card and the audit webhook page (F20):
 * the export's date range and the webhook URL's shape. The server checks all
 * of it again; this only keeps the obvious mistakes from costing a round trip
 * (a download link that lands on a 422 saves an error body as the file).
 */

export const ORG_AUDIT_WEBHOOK_PATH = 'organization/audit-webhook'

/** The longest range one export covers (the server answers 422 past it). */
export const AUDIT_EXPORT_MAX_DAYS = 366

/** The range the card starts on: the last 30 days, today included. */
export const AUDIT_EXPORT_DEFAULT_DAYS = 30

const DAY_MS = 24 * 60 * 60 * 1000
const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/

/** A UTC calendar day as `YYYY-MM-DD`: the server reads `from` and `to` in UTC. */
export function utcDay(date: Date): string {
  return date.toISOString().slice(0, 10)
}

/** Milliseconds of UTC midnight of a `YYYY-MM-DD` day, or `null` when it is not one. */
function dayStart(day: string): number | null {
  const match = ISO_DAY.exec(day)
  if (!match) return null
  const ms = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  // Rejects 2026-02-31 and the like, which Date.UTC rolls over.
  return utcDay(new Date(ms)) === day ? ms : null
}

export function defaultExportRange(now: Date = new Date()): { from: string; to: string } {
  return {
    from: utcDay(new Date(now.getTime() - AUDIT_EXPORT_DEFAULT_DAYS * DAY_MS)),
    to: utcDay(now),
  }
}

/**
 * Why the range cannot be exported, or `null` when it can. Both days are
 * included (the link asks the server for `[from, to + 1 day)`), so one day is
 * a valid range and 366 days means `to` is 365 days after `from`.
 */
export function exportRangeError(from: string, to: string): string | null {
  if (!from || !to) return 'Pick both dates.'
  const start = dayStart(from)
  const end = dayStart(to)
  if (start === null || end === null) return 'Enter the dates as YYYY-MM-DD.'
  if (end < start) return 'The end date must not be before the start date.'
  if ((end - start) / DAY_MS + 1 > AUDIT_EXPORT_MAX_DAYS) {
    return `One export covers at most ${AUDIT_EXPORT_MAX_DAYS} days. Export a longer period in parts.`
  }
  return null
}

/** Why a webhook URL cannot be saved, or `null`. Private addresses are for the server to refuse. */
export function webhookUrlError(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return 'Enter the URL that receives the events.'
  let parsed: URL
  try {
    parsed = new URL(trimmed)
  } catch {
    return 'Enter a full URL, e.g. https://siem.example.com/hooks/tripl.'
  }
  if (parsed.protocol !== 'https:') return 'The URL must start with https://.'
  if (parsed.username || parsed.password) return 'Leave credentials out of the URL.'
  return null
}

export const DELIVERY_STATUS_LABELS: Record<AuditDeliveryStatus, string> = {
  pending: 'Pending',
  sent: 'Sent',
  failed: 'Retrying',
  dead: 'Gave up',
}

/** The statuses the deliveries filter offers, in the order a reader looks for trouble. */
export const DELIVERY_STATUS_FILTERS: readonly AuditDeliveryStatus[] = ['failed', 'dead', 'pending', 'sent']
