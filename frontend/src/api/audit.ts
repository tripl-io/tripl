import { api } from './client'
import type { AuditActionCatalog, AuditEntryDetail, AuditListResponse } from '../types'

export const auditApi = {
  list: (params?: {
    projectSlug?: string
    action?: string
    userId?: string
    userEmail?: string
    since?: string
    until?: string
    limit?: number
    offset?: number
  }) => {
    const sp = new URLSearchParams()
    if (params?.projectSlug) sp.set('project_slug', params.projectSlug)
    if (params?.action) sp.set('action', params.action)
    if (params?.userId) sp.set('user_id', params.userId)
    if (params?.userEmail) sp.set('user_email', params.userEmail)
    if (params?.since) sp.set('since', params.since)
    if (params?.until) sp.set('until', params.until)
    if (params?.limit !== undefined) sp.set('limit', String(params.limit))
    if (params?.offset !== undefined) sp.set('offset', String(params.offset))
    const qs = sp.toString()
    return api.get<AuditListResponse>(`/audit${qs ? `?${qs}` : ''}`)
  },

  // The payload of one entry. List rows carry no payload at all, so this is the
  // only way to read one — see `AuditEntry` in types/branches.ts.
  get: (entryId: string) => api.get<AuditEntryDetail>(`/audit/${entryId}`),

  // The action filter's vocabulary, grouped, from the backend that records it.
  actions: () => api.get<AuditActionCatalog>('/audit/actions'),
}
