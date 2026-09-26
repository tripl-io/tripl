import { api, withBranch } from './client'
import type {
  AppVersionAdoptionResponse,
  AppVersionSeriesResponse,
  BreakdownTimeline,
  EventMetricBreakdownsResponse,
  EventMetricsResponse,
  DistributionDriftsResponse,
  EventWindowMetrics,
  MonitoringSignal,
  OverviewKpiSeries,
  ReleaseRegressionsResponse,
  SeasonalityHeatmap,
  SignalMuteDuration,
  SignalSeries,
  SignalSeriesScope,
  SignalTriageScope,
  SignalTriageState,
  SignalVerdictCounts,
  SignalVerdictRequest,
  TopEvent,
  TopMoverItem,
} from '../types'

export interface EventsMetricsParams {
  event_type_id?: string
  search?: string
  status?: string[]
  tag?: string
  from?: string
  to?: string
}

/** A triage DELETE's scope as query parameters; a mute is per scope, so no bucket. */
function triageQuery(scope: SignalTriageScope, withBucket: boolean): string {
  const sp = new URLSearchParams({ scope_type: scope.scope_type, scope_ref: scope.scope_ref })
  if (scope.scan_config_id) sp.set('scan_config_id', scope.scan_config_id)
  if (withBucket) sp.set('bucket', scope.bucket)
  return sp.toString()
}

/**
 * Event and scan MONITORING reads: per-event / per-event-type volume, overview
 * KPIs, seasonality, drift, app-version adoption and release regressions.
 *
 * Not the metrics catalog. User-defined metrics (fact, SQL and event
 * composition), their CRUD, collection and series live in
 * {@link metricsCatalogApi} (`api/metricsCatalog.ts`). This module was
 * `metricsApi` in `api/metrics.ts` until MET-45 renamed it.
 */
export const eventMetricsApi = {
  // `branchId` scopes the tag / status / search filter to that branch's events;
  // their metrics are read through each copy's main twin (tripl-vk1p).
  getEventsMetrics: (slug: string, params?: EventsMetricsParams, branchId?: string | null) => {
    const sp = new URLSearchParams()
    if (params?.event_type_id) sp.set('event_type_id', params.event_type_id)
    if (params?.search) sp.set('search', params.search)
    if (params?.status?.length) {
      for (const s of params.status) sp.append('status', s)
    }
    if (params?.tag) sp.set('tag', params.tag)
    if (params?.from) sp.set('from', params.from)
    if (params?.to) sp.set('to', params.to)
    const qs = sp.toString()
    return api.get<EventMetricsResponse>(
      withBranch(`/projects/${slug}/events-metrics${qs ? `?${qs}` : ''}`, branchId),
    )
  },

  getProjectTotalMetrics: (
    slug: string,
    params?: { scan_config_id?: string; from?: string; to?: string },
  ) => {
    const sp = new URLSearchParams()
    if (params?.scan_config_id) sp.set('scan_config_id', params.scan_config_id)
    if (params?.from) sp.set('from', params.from)
    if (params?.to) sp.set('to', params.to)
    const qs = sp.toString()
    return api.get<EventMetricsResponse>(`/projects/${slug}/metrics/total${qs ? `?${qs}` : ''}`)
  },

  getEventMetrics: (slug: string, eventId: string, params?: { from?: string; to?: string }) => {
    const sp = new URLSearchParams()
    if (params?.from) sp.set('from', params.from)
    if (params?.to) sp.set('to', params.to)
    const qs = sp.toString()
    return api.get<EventMetricsResponse>(`/projects/${slug}/events/${eventId}/metrics${qs ? `?${qs}` : ''}`)
  },

  getEventMetricBreakdowns: (
    slug: string,
    eventId: string,
    params?: { column?: string; from?: string; to?: string },
  ) => {
    const sp = new URLSearchParams()
    if (params?.column) sp.set('column', params.column)
    if (params?.from) sp.set('from', params.from)
    if (params?.to) sp.set('to', params.to)
    const qs = sp.toString()
    return api.get<EventMetricBreakdownsResponse>(
      `/projects/${slug}/events/${eventId}/metrics/breakdowns${qs ? `?${qs}` : ''}`,
    )
  },

  getEventTypeMetrics: (slug: string, eventTypeId: string, params?: { from?: string; to?: string }) => {
    const sp = new URLSearchParams()
    if (params?.from) sp.set('from', params.from)
    if (params?.to) sp.set('to', params.to)
    const qs = sp.toString()
    return api.get<EventMetricsResponse>(`/projects/${slug}/event-types/${eventTypeId}/metrics${qs ? `?${qs}` : ''}`)
  },

  getEventsWindowMetrics: (
    slug: string,
    data: { event_ids: string[]; time_from: string; time_to: string },
  ) => api.post<EventWindowMetrics[]>(`/projects/${slug}/events/window-metrics`, data),

  getActiveSignals: (slug: string, eventIds?: string[], opts?: { expanded?: boolean }) => {
    // Empty / no ids → cacheable GET. Any filter → POST body, because with
    // 500+ events we'd otherwise blow past proxy/browser query-string limits.
    if (!eventIds || eventIds.length === 0) {
      // expanded=true (AnomaliesPage) adds per-event scopes and keeps tagged
      // incident children; the other callers omit it for the collapsed rollup.
      const query = opts?.expanded ? '?expanded=true' : ''
      return api.get<MonitoringSignal[]>(`/projects/${slug}/anomalies/signals${query}`)
    }
    return api.post<MonitoringSignal[]>(
      `/projects/${slug}/anomalies/signals/query`,
      { event_ids: eventIds },
    )
  },

  /**
   * `POST /anomalies/signals/series` — row sparklines for many open signals in
   * one request (MO-19): the buckets around each flagged one.
   */
  getSignalSeries: (slug: string, scopes: SignalSeriesScope[]) =>
    api.post<SignalSeries[]>(`/projects/${slug}/anomalies/signals/series`, { scopes }),

  // --- Signal triage (MO-4 / JR-5) --------------------------------------------
  // Only for signals no rule routed to an incident (the server answers 409
  // otherwise). Each POST returns the signal's new triage fields; each DELETE
  // is the Undo and is idempotent.

  acknowledgeSignal: (slug: string, scope: SignalTriageScope) =>
    api.post<SignalTriageState>(`/projects/${slug}/anomalies/signals/acknowledge`, scope),

  unacknowledgeSignal: (slug: string, scope: SignalTriageScope) =>
    api.del<void>(
      `/projects/${slug}/anomalies/signals/acknowledge?${triageQuery(scope, true)}`,
    ),

  muteSignalScope: (slug: string, scope: SignalTriageScope, duration: SignalMuteDuration) =>
    api.post<SignalTriageState>(`/projects/${slug}/anomalies/signals/mute`, {
      ...scope,
      duration,
    }),

  unmuteSignalScope: (slug: string, scope: SignalTriageScope) =>
    api.del<void>(`/projects/${slug}/anomalies/signals/mute?${triageQuery(scope, false)}`),

  // --- Signal verdicts (#254) ------------------------------------------------
  // Any signal, routed or not. On a routed one the server writes the verdict to
  // its incident (false positive, acknowledged or resolved), which stays the
  // source of truth. The DELETE clears the signal's own verdict and is
  // idempotent.

  setSignalVerdict: (slug: string, body: SignalVerdictRequest) =>
    api.post<unknown>(`/projects/${slug}/signals/verdict`, body),

  clearSignalVerdict: (slug: string, scope: SignalTriageScope) =>
    api.del<void>(`/projects/${slug}/signals/verdict?${triageQuery(scope, true)}`),

  getSignalVerdictCounts: (slug: string) =>
    api.get<SignalVerdictCounts>(`/projects/${slug}/signals/verdict-counts`),

  getTopMovers: (
    slug: string,
    scanConfigId: string,
    params: { scope_type: string; scope_ref: string; bucket: string; limit?: number },
  ) => {
    const sp = new URLSearchParams({
      scope_type: params.scope_type,
      scope_ref: params.scope_ref,
      bucket: params.bucket,
    })
    if (params.limit !== undefined) sp.set('limit', String(params.limit))
    return api.get<TopMoverItem[]>(
      `/projects/${slug}/scans/${scanConfigId}/top-movers?${sp.toString()}`,
    )
  },

  getOverviewKpiSeries: (slug: string, days = 14) =>
    api.get<OverviewKpiSeries>(`/projects/${slug}/overview/kpi-series?days=${days}`),

  getTopEvents: (slug: string, params?: { windowHours?: number; limit?: number }) => {
    const sp = new URLSearchParams()
    if (params?.windowHours !== undefined) sp.set('window_hours', String(params.windowHours))
    if (params?.limit !== undefined) sp.set('limit', String(params.limit))
    const qs = sp.toString()
    return api.get<TopEvent[]>(`/projects/${slug}/overview/top-events${qs ? `?${qs}` : ''}`)
  },

  getSeasonalityHeatmap: (
    slug: string,
    scanConfigId: string,
    params: { scope_type: string; scope_ref: string; from?: string; to?: string },
  ) => {
    const sp = new URLSearchParams({
      scope_type: params.scope_type,
      scope_ref: params.scope_ref,
    })
    if (params.from) sp.set('from', params.from)
    if (params.to) sp.set('to', params.to)
    return api.get<SeasonalityHeatmap>(
      `/projects/${slug}/scans/${scanConfigId}/seasonality?${sp.toString()}`,
    )
  },

  getBreakdownTimeline: (
    slug: string,
    scanConfigId: string,
    params: {
      scope_type: string
      scope_ref: string
      breakdown_column: string
      breakdown_value: string
      is_other?: boolean
      from?: string
      to?: string
    },
  ) => {
    const sp = new URLSearchParams({
      scope_type: params.scope_type,
      scope_ref: params.scope_ref,
      breakdown_column: params.breakdown_column,
      breakdown_value: params.breakdown_value,
    })
    if (params.is_other !== undefined) sp.set('is_other', String(params.is_other))
    if (params.from) sp.set('from', params.from)
    if (params.to) sp.set('to', params.to)
    return api.get<BreakdownTimeline>(
      `/projects/${slug}/scans/${scanConfigId}/breakdown-timeline?${sp.toString()}`,
    )
  },

  getDistributionDrifts: (
    slug: string,
    params: {
      scope_type: 'project_total' | 'event_type' | 'event'
      scope_ref: string
      scan_config_id?: string
      from?: string
      to?: string
    },
  ) => {
    const sp = new URLSearchParams({
      scope_type: params.scope_type,
      scope_ref: params.scope_ref,
    })
    if (params.scan_config_id) sp.set('scan_config_id', params.scan_config_id)
    if (params.from) sp.set('from', params.from)
    if (params.to) sp.set('to', params.to)
    return api.get<DistributionDriftsResponse>(
      `/projects/${slug}/distribution-drifts?${sp.toString()}`,
    )
  },

  getAppVersionSeries: (
    slug: string,
    scanConfigId: string,
    params?: {
      scope_type?: 'project_total' | 'event_type' | 'event'
      scope_ref?: string
      from?: string
      to?: string
    },
  ) => {
    const sp = new URLSearchParams()
    if (params?.scope_type) sp.set('scope_type', params.scope_type)
    if (params?.scope_ref) sp.set('scope_ref', params.scope_ref)
    if (params?.from) sp.set('from', params.from)
    if (params?.to) sp.set('to', params.to)
    const qs = sp.toString()
    return api.get<AppVersionSeriesResponse>(
      `/projects/${slug}/scans/${scanConfigId}/app-versions${qs ? `?${qs}` : ''}`,
    )
  },

  getAppVersionAdoption: (
    slug: string,
    scanConfigId: string,
    params?: { from?: string; to?: string },
  ) => {
    const sp = new URLSearchParams()
    if (params?.from) sp.set('from', params.from)
    if (params?.to) sp.set('to', params.to)
    const qs = sp.toString()
    return api.get<AppVersionAdoptionResponse>(
      `/projects/${slug}/scans/${scanConfigId}/version-adoption${qs ? `?${qs}` : ''}`,
    )
  },

  getReleaseRegressions: (
    slug: string,
    scanConfigId: string,
    params?: { scope_type?: 'event' | 'event_type' },
  ) => {
    const sp = new URLSearchParams()
    if (params?.scope_type) sp.set('scope_type', params.scope_type)
    const qs = sp.toString()
    return api.get<ReleaseRegressionsResponse>(
      `/projects/${slug}/scans/${scanConfigId}/release-regressions${qs ? `?${qs}` : ''}`,
    )
  },
}
