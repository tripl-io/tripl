import { metricsCatalogApi, type MetricListParams } from '@/api/metricsCatalog'
import type {
  EventCompositionMetricDefinition,
  FactMetricDefinition,
  MetricDefinitionListResponse,
  MetricPreviewResponse,
} from '@/types'

/**
 * The body of `POST /metrics/series-preview`: the definition a save
 * would send for a fact or event-composition metric. SQL metrics keep their
 * own `/preview`, which takes the query rather than a definition.
 */
export type MetricSeriesPreviewRequest = FactMetricDefinition | EventCompositionMetricDefinition

/**
 * Dry-run a draft fact or event-composition metric's series; nothing is saved.
 * A seam of its own so the metric form's tests can stub just this request.
 */
export function previewMetricSeries(
  slug: string,
  body: MetricSeriesPreviewRequest,
): Promise<MetricPreviewResponse> {
  return metricsCatalogApi.previewSeries(slug, body)
}

/** Catalog list params, with the fact table the Used-by link narrows to (F7). */
export interface CatalogListParams extends Omit<MetricListParams, 'fact_table_id'> {
  /** Only metrics that read this fact table, as either ratio operand. */
  factTableId?: string
}

/** One catalog page, narrowed to a fact table when the URL names one. */
export function listCatalogPage(
  slug: string,
  { factTableId, ...params }: CatalogListParams,
): Promise<MetricDefinitionListResponse> {
  return metricsCatalogApi.list(slug, factTableId ? { ...params, fact_table_id: factTableId } : params)
}
