import { dayBoundaryIso, formatIsoDate } from '@/lib/datetime'

import { CHANNEL_META } from './channelMeta'

export interface DeliveryFilters {
  status: string
  channel: string
  destination_id: string
  rule_id: string
  scan_config_id: string
  // ISO instants, not the `YYYY-MM-DD` the day picker shows. The page
  // forwards these straight to `date_from`/`date_to`, and a bare date pins
  // `date_to` to midnight — which drops the whole day the reader just asked
  // for. `dayBoundaryIso` converts; `formatIsoDate` converts back for the
  // picker. '' means unset.
  date_from: string
  date_to: string
}

/** Every filter off — what Clear writes, and what "no filter is active" means. */
export const NO_DELIVERY_FILTERS: DeliveryFilters = {
  status: '',
  channel: '',
  destination_id: '',
  rule_id: '',
  scan_config_id: '',
  date_from: '',
  date_to: '',
}

/** The statuses the Status select offers, and the only ones a URL may name. */
export const DELIVERY_STATUSES = ['pending', 'sent', 'failed'] as const

/**
 * URL keys. Prefixed with `delivery_` because the Inbox already owns
 * the bare `status`, `scope`, `direction` and `scope_type` on this same route —
 * and a Delivery log link pasted to a colleague must not also filter their
 * Inbox. `scan` is the exception, and deliberately so: it is the key a scan
 * run's "Alerts queued" counter has always linked with, and
 * ProjectSettingsPage hands it down as `focusScanId`.
 *
 * Dates travel as the reader's calendar day, not as the instant the state
 * holds, so a shared link means "the 12th" in the reader's own calendar rather
 * than a midnight somewhere else.
 */
const PARAM_KEYS = {
  status: 'delivery_status',
  channel: 'delivery_channel',
  destination_id: 'delivery_destination',
  rule_id: 'delivery_rule',
  scan_config_id: 'scan',
  date_from: 'delivery_from',
  date_to: 'delivery_to',
} as const satisfies Record<keyof DeliveryFilters, string>

/** Where the delivery window starts, beside the filters it indexes into. */
export const DELIVERY_OFFSET_PARAM = 'delivery_offset'

/** Every URL key the delivery log owns, the offset included. Derived, never restated. */
export const DELIVERY_FILTER_PARAM_KEYS: readonly string[] = [
  ...Object.values(PARAM_KEYS),
  DELIVERY_OFFSET_PARAM,
]

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/**
 * An id param, kept only when it is a UUID. The API declares every id filter
 * `uuid.UUID`, so a truncated or hand-edited id (`?delivery_destination=abc`)
 * would 422 the whole log instead of leaving that one filter off.
 */
function readId(value: string | null | undefined): string {
  const trimmed = value?.trim() ?? ''
  return UUID_PATTERN.test(trimmed) ? trimmed : ''
}

/**
 * The delivery filters out of the URL.
 *
 * Unknown statuses and channels are DROPPED, as the Inbox's reader does: the
 * API 422s a value it does not know, so forwarding a stale one would turn a
 * link into a failed request instead of an unfiltered list. Malformed ids are
 * dropped for the same reason — the API types them as UUIDs and 422s anything
 * else. A well-formed id that names nothing (a deleted destination or rule)
 * passes through and simply matches nothing, which the empty state names.
 *
 * `scanId` is the page's `focusScanId` — the same `?scan=` value, read once by
 * the route that renders the page.
 */
export function readDeliveryFilters(params: URLSearchParams, scanId: string | undefined): DeliveryFilters {
  const status = params.get(PARAM_KEYS.status) ?? ''
  const channel = params.get(PARAM_KEYS.channel) ?? ''
  return {
    status: (DELIVERY_STATUSES as readonly string[]).includes(status) ? status : '',
    channel: CHANNEL_META.some(meta => meta.channel === channel) ? channel : '',
    destination_id: readId(params.get(PARAM_KEYS.destination_id)),
    rule_id: readId(params.get(PARAM_KEYS.rule_id)),
    scan_config_id: readId(scanId),
    date_from: dayBoundaryIso(params.get(PARAM_KEYS.date_from) ?? '', 'start') ?? '',
    date_to: dayBoundaryIso(params.get(PARAM_KEYS.date_to) ?? '', 'end') ?? '',
  }
}

/** The filters as URL params, empty members left out (same rule as the Inbox). */
export function writeDeliveryFilters(filters: DeliveryFilters): Record<string, string> {
  const written: Record<string, string> = {}
  if (filters.status) written[PARAM_KEYS.status] = filters.status
  if (filters.channel) written[PARAM_KEYS.channel] = filters.channel
  if (filters.destination_id) written[PARAM_KEYS.destination_id] = filters.destination_id
  if (filters.rule_id) written[PARAM_KEYS.rule_id] = filters.rule_id
  if (filters.scan_config_id) written[PARAM_KEYS.scan_config_id] = filters.scan_config_id
  const from = filters.date_from ? formatIsoDate(filters.date_from) : ''
  const to = filters.date_to ? formatIsoDate(filters.date_to) : ''
  if (from) written[PARAM_KEYS.date_from] = from
  if (to) written[PARAM_KEYS.date_to] = to
  return written
}

/** A non-negative whole offset, or 0 for anything else a URL can carry. */
export function readDeliveryOffset(params: URLSearchParams): number {
  const raw = params.get(DELIVERY_OFFSET_PARAM)
  if (!raw || !/^\d+$/.test(raw)) return 0
  const value = Number(raw)
  return Number.isSafeInteger(value) ? value : 0
}

/** Whether any filter is narrowing the log. */
export function hasActiveDeliveryFilters(filters: DeliveryFilters): boolean {
  return Object.values(filters).some(Boolean)
}

/**
 * Where "Newer" should land from `offset`.
 *
 * Normally one page back. But the log can shrink under an offset — a retry
 * moves a row out of Status=Failed, a destination is deleted elsewhere — and a
 * reader parked past the end then sees an empty page. From there, Newer goes
 * straight to the last page that still has rows rather than one blank step at
 * a time.
 */
export function newerDeliveryOffset(offset: number, total: number, limit: number): number {
  if (total > 0 && offset >= total) {
    return Math.max(0, Math.floor((total - 1) / limit) * limit)
  }
  return Math.max(0, offset - limit)
}
