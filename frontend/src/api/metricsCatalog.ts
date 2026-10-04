import { api } from './client'
import type {
  EventCompositionMetricDefinition,
  FactMetricDefinition,
  FactOperandPreviewRequest,
  FactOperandPreviewResponse,
  MetricBreakdownsResponse,
  MetricCollectNowResponse,
  MetricCreate,
  MetricDefinitionBulkUpdate,
  MetricDefinitionDetailResponse,
  MetricDefinitionListResponse,
  MetricDefinitionMove,
  MetricDefinitionReorder,
  MetricDefinitionResponse,
  MetricDefinitionUpdate,
  MetricGeneratedSqlResponse,
  MetricKind,
  MetricPreviewRequest,
  MetricPreviewResponse,
  MetricSeriesResponse,
  MetricStatus,
  MetricVersionSeriesResponse,
} from '../types'

export interface MetricListParams {
  status?: MetricStatus[]
  kind?: MetricKind
  search?: string
  /** true: reviewed metrics only; false: not yet reviewed. */
  reviewed?: boolean
  owner_id?: string
  /** Only metrics that read this fact table, as either ratio operand. */
  fact_table_id?: string
  offset?: number
  limit?: number
}

export interface MetricSeriesParams {
  from?: string
  to?: string
}

export interface MetricBreakdownParams extends MetricSeriesParams {
  column?: string
}

/**
 * Catalog metric CRUD + the per-metric series surfaces (volume / breakdowns /
 * versions) that back the monitoring drilldown. Everything about a user-defined
 * metric lives here; `eventMetricsApi` (`api/eventMetrics.ts`) is event and scan
 * monitoring, not the catalog. Mirrors the thin-wrapper style
 * of `eventMetricsApi` / `eventsApi`: each call builds the query string explicitly
 * and is typed off the committed OpenAPI schemas.
 */
export const metricsCatalogApi = {
  list: (slug: string, params?: MetricListParams) => {
    const sp = new URLSearchParams()
    if (params?.status?.length) {
      for (const s of params.status) sp.append('status', s)
    }
    if (params?.kind) sp.set('kind', params.kind)
    if (params?.search) sp.set('search', params.search)
    if (params?.reviewed !== undefined) sp.set('reviewed', String(params.reviewed))
    if (params?.owner_id) sp.set('owner_id', params.owner_id)
    if (params?.fact_table_id) sp.set('fact_table_id', params.fact_table_id)
    if (params?.offset !== undefined) sp.set('offset', String(params.offset))
    if (params?.limit !== undefined) sp.set('limit', String(params.limit))
    const qs = sp.toString()
    return api.get<MetricDefinitionListResponse>(
      `/projects/${slug}/metrics${qs ? `?${qs}` : ''}`,
    )
  },

  get: (slug: string, metricId: string) =>
    api.get<MetricDefinitionDetailResponse>(`/projects/${slug}/metrics/${metricId}`),

  create: (slug: string, data: MetricCreate) =>
    api.post<MetricDefinitionResponse>(`/projects/${slug}/metrics`, data),

  update: (slug: string, metricId: string, data: MetricDefinitionUpdate) =>
    api.patch<MetricDefinitionResponse>(`/projects/${slug}/metrics/${metricId}`, data),

  del: (slug: string, metricId: string) =>
    api.del<void>(`/projects/${slug}/metrics/${metricId}`),

  /** Trigger an immediate backfill collection for one metric (editor-gated). */
  collect: (slug: string, metricId: string) =>
    api.post<MetricCollectNowResponse>(`/projects/${slug}/metrics/${metricId}/collect`, {}),

  /** Build the exact shared primary SQL for the saved fact-metric collection batch. */
  getGeneratedSql: (slug: string, metricId: string) =>
    api.get<MetricGeneratedSqlResponse>(
      `/projects/${slug}/metrics/${metricId}/generated-sql`,
    ),

  /**
   * Stateless dry-run of a sql-kind metric SELECT. Expected user mistakes
   * (bad SQL, missing columns, warehouse errors) come back as 200 with
   * `error` set; an unknown data source is a 404.
   */
  preview: (slug: string, data: MetricPreviewRequest) =>
    api.post<MetricPreviewResponse>(`/projects/${slug}/metrics/preview`, data),

  /**
   * Stateless dry-run of ONE fact operand's row filters: the backend compiles
   * them with the collector's own resolver for the fact table's dialect and
   * executes the result (1-row cap). Expected user mistakes (unknown named
   * filter, SQL the warehouse rejects, a measure column the filtered query does
   * not project) come back as 200 with `error` set; an unknown fact table is a
   * 404.
   */
  previewFactOperand: (slug: string, data: FactOperandPreviewRequest) =>
    api.post<FactOperandPreviewResponse>(`/projects/${slug}/metrics/fact-preview`, data),

  /**
   * Dry-run a draft fact or event-composition metric's series; nothing
   * is saved. SQL metrics keep `preview`, which takes the query instead.
   */
  previewSeries: (slug: string, data: FactMetricDefinition | EventCompositionMetricDefinition) =>
    api.post<MetricPreviewResponse>(`/projects/${slug}/metrics/series-preview`, data),

  bulkUpdate: (slug: string, data: MetricDefinitionBulkUpdate) =>
    api.post<void>(`/projects/${slug}/metrics/bulk-update`, data),

  reorder: (slug: string, data: MetricDefinitionReorder) =>
    api.patch<MetricDefinitionResponse[]>(`/projects/${slug}/metrics/reorder`, data),

  move: (slug: string, metricId: string, data: MetricDefinitionMove) =>
    api.patch<MetricDefinitionResponse>(`/projects/${slug}/metrics/${metricId}/move`, data),

  getSeries: (slug: string, metricId: string, params?: MetricSeriesParams) => {
    const sp = new URLSearchParams()
    if (params?.from) sp.set('from', params.from)
    if (params?.to) sp.set('to', params.to)
    const qs = sp.toString()
    return api.get<MetricSeriesResponse>(
      `/projects/${slug}/metrics/${metricId}/series${qs ? `?${qs}` : ''}`,
    )
  },

  getBreakdowns: (slug: string, metricId: string, params?: MetricBreakdownParams) => {
    const sp = new URLSearchParams()
    if (params?.column) sp.set('column', params.column)
    if (params?.from) sp.set('from', params.from)
    if (params?.to) sp.set('to', params.to)
    const qs = sp.toString()
    return api.get<MetricBreakdownsResponse>(
      `/projects/${slug}/metrics/${metricId}/breakdowns${qs ? `?${qs}` : ''}`,
    )
  },

  getVersions: (slug: string, metricId: string, params?: MetricSeriesParams) => {
    const sp = new URLSearchParams()
    if (params?.from) sp.set('from', params.from)
    if (params?.to) sp.set('to', params.to)
    const qs = sp.toString()
    return api.get<MetricVersionSeriesResponse>(
      `/projects/${slug}/metrics/${metricId}/versions${qs ? `?${qs}` : ''}`,
    )
  },
}
