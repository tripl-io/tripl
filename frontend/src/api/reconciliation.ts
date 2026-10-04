import { api, withBranch } from './client'

export type ShadowEventStatus = 'new' | 'accepted' | 'dismissed'

export interface ShadowEvent {
  id: string
  scan_config_id: string
  scan_config_name: string
  event_type_id: string | null
  event_type_name: string | null
  event_name: string
  observed_count: number
  first_seen_at: string
  last_seen_at: string
  status: ShadowEventStatus
  accepted_event_id: string | null
  /**
   * A few rows the collector saw for this identity. Optional so a
   * response from an older server still renders.
   */
  sample_properties?: Record<string, string>[]
}

export interface ShadowEventsResponse {
  items: ShadowEvent[]
  total: number
  new_count: number
}

export interface AcceptShadowEventResponse {
  candidate_id: string
  event_id: string
  status: string
}

export interface DismissShadowEventResponse {
  candidate_id: string
  status: string
}

/** The most rows one batch request takes (the route's `MAX_SHADOW_BATCH`). */
export const MAX_SHADOW_BATCH = 200

export interface ShadowEventBatchItem {
  candidate_id: string
  /** Accept only; defaults to the candidate's detected type. */
  event_type_id?: string
  name?: string
}

export interface ShadowEventBatchItemResult {
  candidate_id: string
  ok: boolean
  status: ShadowEventStatus | null
  event_id: string | null
  /** Why a row was refused, in the single route's words. */
  error: string | null
  error_status: number | null
}

export interface ShadowEventBatchResponse {
  results: ShadowEventBatchItemResult[]
  succeeded: number
  failed: number
}

export interface DeadEvent {
  event_id: string
  name: string
  event_type_id: string
  event_type_name: string
  last_seen_at: string | null
  created_at: string
}

export interface DeadEventsResponse {
  items: DeadEvent[]
  total: number
  days: number
}

export type DeadEventArchiveStatus = 'archived' | 'deprecated'

export interface ArchiveDeadEventsResponse {
  event_ids: string[]
  status: DeadEventArchiveStatus
  archived_count: number
}

export interface CoverageBucket {
  bucket: string
  total_count: number
  matched_count: number
}

export interface CoverageSummary {
  total_count: number
  matched_count: number
  coverage_pct: number
}

export interface CoverageResponse {
  items: CoverageBucket[]
  summary: CoverageSummary
  days: number
}

export const reconciliationApi = {
  shadowEvents: (
    slug: string,
    params: { status?: ShadowEventStatus; limit?: number; offset?: number },
    branchId?: string | null,
  ) => {
    const qs = new URLSearchParams()
    if (params.status) qs.set('status', params.status)
    if (params.limit !== undefined) qs.set('limit', String(params.limit))
    if (params.offset) qs.set('offset', String(params.offset))
    const base = `/projects/${slug}/reconciliation/shadow-events`
    const path = qs.toString() ? `${base}?${qs}` : base
    return api.get<ShadowEventsResponse>(withBranch(path, branchId))
  },

  acceptShadowEvent: (
    slug: string,
    id: string,
    body: { event_type_id?: string; name?: string },
    branchId?: string | null,
  ) =>
    api.post<AcceptShadowEventResponse>(
      withBranch(`/projects/${slug}/reconciliation/shadow-events/${id}/accept`, branchId),
      body,
    ),

  dismissShadowEvent: (
    slug: string,
    id: string,
    branchId?: string | null,
  ) =>
    api.post<DismissShadowEventResponse>(
      withBranch(`/projects/${slug}/reconciliation/shadow-events/${id}/dismiss`, branchId),
    ),

  /**
   * Accept or dismiss many rows in one request. Each row succeeds or
   * fails on its own; `results` says which, in the order sent.
   */
  batchShadowEvents: (
    slug: string,
    body: { action: 'accept' | 'dismiss'; items: ShadowEventBatchItem[] },
    branchId?: string | null,
  ) =>
    api.post<ShadowEventBatchResponse>(
      withBranch(`/projects/${slug}/reconciliation/shadow-events/batch`, branchId),
      body,
    ),

  deadEvents: (slug: string, days: number) =>
    api.get<DeadEventsResponse>(`/projects/${slug}/reconciliation/dead-events?days=${days}`),

  archiveDeadEvents: (
    slug: string,
    eventIds: string[],
    status: DeadEventArchiveStatus = 'archived',
    branchId?: string | null,
  ) =>
    api.post<ArchiveDeadEventsResponse>(
      withBranch(`/projects/${slug}/reconciliation/dead-events/archive`, branchId),
      { event_ids: eventIds, status },
    ),

  coverage: (slug: string, days: number) =>
    api.get<CoverageResponse>(`/projects/${slug}/reconciliation/coverage?days=${days}`),
}
