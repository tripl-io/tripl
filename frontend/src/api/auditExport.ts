import { api, ApiError } from './client'

/**
 * An organization's audit log leaving tripl (F20): a streamed export and a
 * signed webhook (`/orgs/{org}/audit/export` and `/orgs/{org}/audit/webhook`).
 *
 * Hand-written rather than read from `api.gen.ts`: the export is a file the
 * browser downloads itself (a streamed CSV or NDJSON body, never parsed
 * here), and the webhook's contract is small. `/orgs/...` paths are never
 * rewritten by the client: the organization is named in the path.
 */

/** The file formats the export streams: CSV, or NDJSON (one JSON object per line). */
export type AuditExportFormat = 'csv' | 'json'

export interface AuditExportParams {
  format: AuditExportFormat
  /** First day, `YYYY-MM-DD` (UTC), included. */
  from: string
  /**
   * Last day, `YYYY-MM-DD` (UTC), INCLUDED: the server's `to` is exclusive, so
   * {@link auditExportUrl} sends the day after it. Not before `from`; the two
   * cover at most 366 days.
   */
  to: string
  /** Only rows with this action, e.g. `org.member_role_changed`. */
  action?: string
}

const DAY_MS = 24 * 60 * 60 * 1000

/** The `YYYY-MM-DD` day after `day` (UTC); `day` itself when it is not one (the server answers 422). */
export function dayAfter(day: string): string {
  const ms = Date.parse(`${day}T00:00:00Z`)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || Number.isNaN(ms)) return day
  return new Date(ms + DAY_MS).toISOString().slice(0, 10)
}

function orgBase(org: string): string {
  return `/orgs/${encodeURIComponent(org)}/audit`
}

/**
 * The export's address, for a link the browser follows to download it. Not a
 * fetch: the body is streamed in chunks and can be large, so the browser
 * writes it to disk instead of the page holding it in memory. The session
 * cookie goes with the navigation like any other same-origin request.
 * `params.to` is the last day included; the URL carries the day after it.
 */
export function auditExportUrl(org: string, params: AuditExportParams): string {
  // The server reads `[from, to)`; the last day picked is a whole day.
  const sp = new URLSearchParams({ format: params.format, from: params.from, to: dayAfter(params.to) })
  if (params.action) sp.set('action', params.action)
  return `/api/v1${orgBase(org)}/export?${sp.toString()}`
}

/**
 * The organization's audit webhook. The signing secret is never returned after
 * it is created or rotated. The server answers 200 `{configured: false, ...}`
 * when there is none; {@link auditWebhookApi.get} turns that into `null`.
 */
export interface AuditWebhook {
  /** `false`: no webhook is set up (the other fields are placeholders). */
  configured: boolean
  url: string
  enabled: boolean
  /** Whether a signing secret is stored. */
  secret_configured: boolean
  last_success_at: string | null
  last_error: string | null
  last_error_at: string | null
}

/**
 * A save's answer. `secret` is there only on the save that created the
 * webhook: the one time the signing secret is shown.
 */
export interface AuditWebhookSaved extends AuditWebhook {
  secret?: string | null
}

export interface AuditWebhookUpdate {
  /** `https://` only, and a public address. */
  url: string
  enabled: boolean
}

/** A rotation's answer: the webhook and its fresh signing secret, shown this once. */
export interface AuditWebhookSecret extends AuditWebhook {
  secret: string
}

/** What a synthetic `audit.webhook_test` delivery got back. */
export interface AuditWebhookTestResult {
  ok: boolean
  /** The receiver's HTTP status, `null` when none came back (timeout, refused, DNS). */
  status_code: number | null
  error: string | null
}

export type AuditDeliveryStatus = 'pending' | 'sent' | 'failed' | 'dead'

/** One audit entry's way to the webhook (the outbox row). */
export interface AuditWebhookDelivery {
  id: string
  audit_log_id: string
  action: string
  status: AuditDeliveryStatus
  attempts: number
  next_attempt_at: string | null
  last_error: string | null
  created_at: string
  sent_at: string | null
}

export const auditWebhookApi = {
  /**
   * The webhook, or `null` when the organization has none: the server answers
   * 200 `{configured: false}` (a 404 is read the same way).
   */
  get: async (org: string): Promise<AuditWebhook | null> => {
    try {
      const data = await api.get<AuditWebhook | null>(`${orgBase(org)}/webhook`)
      return data && data.configured !== false ? data : null
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null
      throw error
    }
  },
  /** Create or replace. The first save generates the signing secret and returns it once. */
  save: (org: string, data: AuditWebhookUpdate) =>
    api.put<AuditWebhookSaved>(`${orgBase(org)}/webhook`, data),
  remove: (org: string) => api.del<void>(`${orgBase(org)}/webhook`),
  /** A new signing secret, shown once; the old one stops verifying at once. */
  rotateSecret: (org: string) =>
    api.post<AuditWebhookSecret>(`${orgBase(org)}/webhook/rotate-secret`),
  /** Sends one synthetic event now and reports what the receiver answered. */
  test: (org: string) => api.post<AuditWebhookTestResult>(`${orgBase(org)}/webhook/test`),
  deliveries: (org: string, params?: { status?: AuditDeliveryStatus; limit?: number }) => {
    const sp = new URLSearchParams()
    if (params?.status) sp.set('status', params.status)
    if (params?.limit !== undefined) sp.set('limit', String(params.limit))
    const qs = sp.toString()
    return api.get<AuditWebhookDelivery[]>(`${orgBase(org)}/webhook/deliveries${qs ? `?${qs}` : ''}`)
  },
}
