import { api } from './client'
import type { AuditActionCatalog, AuditEntryDetail, AuditListResponse } from '../types'

/** The filters and page of one audit list request. */
export interface AuditListParams {
  action?: string
  userId?: string
  userEmail?: string
  since?: string
  until?: string
  limit?: number
  offset?: number
}

/**
 * Where an audit log reads from. Community reads one project's history; the
 * Enterprise edition adds the organization-wide log through the same view.
 */
export interface AuditSource {
  /** Tells this log's cached pages and entries apart from another log's. */
  key: string
  /** Rows span every project and those of none (an organization-wide log). */
  wide: boolean
  list: (params: AuditListParams) => Promise<AuditListResponse>
  /** One entry with its payload: list rows carry none (`AuditEntry`). */
  get: (entryId: string) => Promise<AuditEntryDetail>
  /** The action filter's vocabulary, grouped, from the backend that records it. */
  actions: () => Promise<AuditActionCatalog>
}

/** `?action=…&limit=…` for a list request, or `''`. */
export function auditListQuery(params: AuditListParams): string {
  const sp = new URLSearchParams()
  if (params.action) sp.set('action', params.action)
  if (params.userId) sp.set('user_id', params.userId)
  if (params.userEmail) sp.set('user_email', params.userEmail)
  if (params.since) sp.set('since', params.since)
  if (params.until) sp.set('until', params.until)
  if (params.limit !== undefined) sp.set('limit', String(params.limit))
  if (params.offset !== undefined) sp.set('offset', String(params.offset))
  const qs = sp.toString()
  return qs ? `?${qs}` : ''
}

/** One project's history: its settings' Audit tab. */
export function projectAuditSource(slug: string): AuditSource {
  const base = `/projects/${encodeURIComponent(slug)}/audit`
  return {
    key: `project:${slug}`,
    wide: false,
    list: (params) => api.get<AuditListResponse>(`${base}${auditListQuery(params)}`),
    get: (entryId) => api.get<AuditEntryDetail>(`${base}/${entryId}`),
    actions: () => api.get<AuditActionCatalog>(`${base}/actions`),
  }
}
