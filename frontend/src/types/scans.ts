import type { MetricScopeType } from './metrics'

export interface ScanJobResultSummary {
  mode?: 'metrics_collection' | 'metrics_replay'
  catalog_sync_skipped?: boolean
  time_from?: string
  time_to?: string
  events_created?: number
  events_skipped?: number
  events_grouped?: number
  events_merged?: number
  variables_created?: number
  variable_values_touched?: number
  /** Scheduled-run sampler counters (absent on replay and on pre-2026-09 jobs). */
  json_path_ring_size?: number
  json_paths_sampled?: number
  json_paths_with_samples?: number
  variable_values_written?: number
  variable_contexts_unfilled?: number
  variables_retired?: number
  columns_analyzed?: number
  event_metrics?: number
  type_metrics?: number
  breakdown_event_metrics?: number
  breakdown_type_metrics?: number
  metrics_deleted?: number
  breakdown_metrics_deleted?: number
  distribution_drifts?: number
  significant_distribution_drifts?: number
  distribution_drifts_deleted?: number
  contract_violations_detected?: number
  contract_checks_failed?: number
  contract_expectations_skipped?: number
  anomalies_detected?: number
  breakdown_anomalies_detected?: number
  signals_added?: number
  signals_removed?: number
  alerts_queued?: number
  scan_row_limit?: number
  scan_rows_processed?: number
  /**
   * Warehouse rows behind a catalog run's breakdown (the sum of each GROUP BY
   * row's count), the population the dry run reports. Absent on older runs.
   */
  catalog_rows_scanned?: number
  metrics_row_limit?: number
  query_rows_scanned?: number
  replay_chunk_interval?: string
  replay_chunks_total?: number
  replay_chunks_completed?: number
  replay_current_chunk_index?: number | null
  replay_current_chunk_from?: string | null
  replay_current_chunk_to?: string | null
  replay_progress_percent?: number
  replay_progress_phase?: 'preparing' | 'collecting' | 'finalizing' | 'completed'
  /**
   * Source freshness at the end of a metrics collection (F16, #269), and how
   * many drop-direction volume signals it held back because the source was
   * late or its scan overdue. Absent on older jobs and on replays.
   */
  freshness_status?: SourceFreshnessStatus
  signals_held?: number
  details?: string[]
}

/**
 * How current a scan's source is (F16, #269), computed server-side on read and
 * never stored:
 *   - `fresh`   the newest event is within the expected lag;
 *   - `late`    the scan runs, but the newest event it sees is older than
 *               max(3 intervals, settling window + 1 interval);
 *   - `overdue` the scan itself has not collected for over 2 intervals
 *               (wins over `late`);
 *   - `unknown` a manual scan (no interval) or no collection yet.
 */
export type SourceFreshnessStatus = 'fresh' | 'late' | 'overdue' | 'unknown'

export interface SourceFreshness {
  status: SourceFreshnessStatus
  /** Seconds between now and `last_event_at`; null without an observed event. */
  lag_seconds: number | null
  /** Start of the newest bucket that had events, as the latest successful
   *  metrics collection saw it: bucket resolution, so it can trail the newest
   *  event by up to one scan interval. */
  last_event_at: string | null
  /** When that collection completed. */
  last_collection_at: string | null
  /** The moment past which the source counts as late; null when unknown. */
  expected_by: string | null
}

/** One scan's row in `GET /projects/{slug}/source-freshness`. */
export interface SourceFreshnessItem {
  id: string
  name: string
  data_source_id: string
  freshness: SourceFreshness
}

export interface ProjectLatestScanJob {
  id: string
  scan_config_id: string
  scan_name: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  started_at: string | null
  completed_at: string | null
  result_summary: ScanJobResultSummary | null
  error_message: string | null
  created_at: string
}

export interface ProjectLatestSignal {
  scan_config_id: string
  scan_name: string
  scope_type: MetricScopeType
  scope_ref: string
  scope_name: string
  state: 'latest_scan' | 'recent'
  bucket: string
  actual_count: number
  expected_count: number
  z_score: number
  direction: 'spike' | 'drop'
}

export type IntervalCode = '15m' | '1h' | '6h' | '1d' | '1w'

export interface EventGroupCondition {
  field: string
  pattern: string
}

export interface EventGroupRule {
  name: string
  condition_logic: 'all' | 'any'
  conditions: EventGroupCondition[]
}

export interface ScanConfig {
  id: string
  data_source_id: string
  project_id: string
  event_type_id: string | null
  name: string
  base_query: string
  event_type_column: string | null
  time_column: string | null
  event_name_format: string | null
  json_value_paths: string[]
  event_group_rules: EventGroupRule[]
  metric_breakdown_columns: string[]
  metric_breakdown_values_limit: number | null
  distribution_drift_fields: string[]
  cardinality_threshold: number
  interval: IntervalCode | null
  replay_chunk_interval: IntervalCode | null
  scan_lookback_hours: number | null
  scan_row_limit: number | null
  metrics_row_limit: number | null
  app_version_column: string | null
  app_version_keep_releases: number | null
  app_version_prerelease_pattern: string | null
  app_version_active_share_min: number | null
  platform_column: string | null
  created_at: string
  updated_at: string
  /**
   * Server-derived `interval IS NOT NULL` (MO-23): whether the scheduler
   * collects metrics for this scan. Read-only, never sent back. The views keep
   * `scanModeOf`, which also needs a time column: an interval without one is
   * `misconfigured`, a state this flag alone would report as monitoring.
   * Optional so hand-built configs (tests, the form's draft) need not invent it.
   */
  readonly monitoring_enabled?: boolean
  /**
   * Sent only by `GET /scans/{id}` (i9mt.16 DA-5): when the newest scheduled
   * metrics collection finished, and the earliest moment the scheduler
   * considers the scan due again. Null for a scan it never collects.
   */
  readonly last_metrics_run_at?: string | null
  readonly next_metrics_run_at?: string | null
  /**
   * Source freshness (F16, #269), computed on read. Optional so hand-built
   * configs (tests, the form's draft) need not invent it.
   */
  readonly freshness?: SourceFreshness
}

export interface PlatformPresenceRow {
  event_id: string
  event_name: string
  present_platforms: string[]
}

export interface PlatformPresenceResponse {
  scan_config_id: string
  platform_column: string | null
  platforms: string[]
  items: PlatformPresenceRow[]
}

export interface ScanPreviewColumn {
  name: string
  type_name: string
  is_nullable: boolean
}

export interface ScanPreviewJsonPath {
  full_path: string
  path: string
  sample_values: string[]
}

export interface ScanPreviewJsonColumn {
  column: string
  paths: ScanPreviewJsonPath[]
}

export interface ScanConfigPreview {
  columns: ScanPreviewColumn[]
  rows: Record<string, unknown>[]
  json_columns: ScanPreviewJsonColumn[]
}

export interface ScanPreviewJob {
  id: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  started_at: string | null
  completed_at: string | null
  // Holds a ScanConfigPreview when status === 'completed'; null otherwise.
  result_summary: ScanConfigPreview | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface ProjectAnomalySettings {
  id: string
  project_id: string
  anomaly_detection_enabled: boolean
  detect_project_total: boolean
  detect_event_types: boolean
  detect_events: boolean
  // Catalog metrics (Metrics tab) are scored on their own series, independent
  // of the event-scope flags above.
  detect_metrics: boolean
  baseline_window_buckets: number
  min_history_buckets: number
  sigma_threshold: number
  min_expected_count: number
  // How long a detected anomaly keeps counting as an "open" signal for the
  // Anomalies page and the sidebar badge.
  recent_signal_window_hours: number
  // Wall-clock allowance for the warehouse to finish delivering a bucket before
  // that bucket is scored. Holds the newest buckets back from raising signals.
  anomaly_ingestion_settling_minutes: number
  created_at: string
  updated_at: string
}

/**
 * One scope the false-positive ratchet has made stricter.
 *
 * Marking an alert a false positive raises `sigma_threshold` and
 * `min_expected_count` for the scope it fired on — and only that scope. The
 * values here are ABSOLUTE and replace the project settings for that scope.
 * The ratchet never decays, so deleting the override is the only way back.
 */
export interface AnomalyScopeOverride {
  id: string
  // null for `metric` scopes: catalog metric series are project-global.
  scan_config_id: string | null
  scan_config_name: string | null
  scope_type: string
  scope_ref: string
  scope_name: string
  sigma_threshold: number
  min_expected_count: number
  false_positive_count: number
  created_at: string
  updated_at: string
}

export interface AnomalyScopeOverrideList {
  items: AnomalyScopeOverride[]
  total: number
}

export interface ScanJob {
  id: string
  scan_config_id: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  started_at: string | null
  completed_at: string | null
  result_summary: ScanJobResultSummary | null
  error_message: string | null
  created_at: string
  updated_at: string
}

// Mirrors ScanActivityItem / ScanActivityResponse (schemas/scan_job.py): the
// Scans list's per-scan figures, aggregated in SQL over the whole history.
export interface ScanActivityItem {
  scan_config_id: string
  latest_job: ScanJob | null
  // Consecutive failed runs, newest first, looking past queued/running jobs.
  failing_streak: number
  // Rows read by jobs stamped (completed, else started) inside the window.
  // Mixes two units; the split pair below sums to it.
  rows_read_24h: number
  // Warehouse rows metrics runs read, and the GROUP BY ALL combinations catalog
  // runs read back, over the same window (#247 DA-4). Always sent; optional so
  // fixtures written before the split still type.
  warehouse_rows_24h?: number
  catalog_combinations_24h?: number
}

export interface ScanActivityResponse {
  window_from: string
  window_to: string
  items: ScanActivityItem[]
}

// ─── Dry run: "what would this scan create?" ──────────────────────────────────
// Mirrors the backend's ScanDryRun* Pydantic models (schemas/scan_config.py).
// The payload is computed by the SAME planner a real run uses, so the names
// below are the names a run would write — never a second implementation of
// generation in TypeScript.

/**
 * One event the config would produce, and how much of the sample it is.
 *
 * Identified by `(event_type, source_name)`, never by the name alone: a run
 * writes one Event per event type, so a grouped scan whose name format collapses
 * to the same string under two event types creates two events.
 */
export interface ScanDryRunEvent {
  name: string
  /** The name before group rules merged it; equal to `name` when nothing merged. */
  source_name: string
  /**
   * The event type this event lands under — the group value on the
   * `event_type_column` path, the chosen event type's name otherwise.
   */
  event_type: string
  /**
   * Sampled warehouse rows behind this name — an EXACT count of the rows the dry
   * run looked at, never an estimate of the whole table.
   */
  approx_row_count: number
  share_of_sample: number
  status: 'new' | 'existing'
  /** The event group rule that merged this name, when one did. */
  grouped_by_rule: string | null
  count_confidence: 'exact' | 'sampled'
}

/**
 * A field the config would add to the plan. `type` is json-or-string and nothing
 * else — that is the entire type inference a scan performs, and promising
 * `integer`/`timestamp` would be a claim about something it does not do.
 */
export interface ScanDryRunField {
  name: string
  type: 'json' | 'string'
  status: 'new' | 'exists'
  event_type: string
}

/** A column the cardinality rule collapsed into a `{column}` template. */
export interface ScanDryRunTemplatedColumn {
  column: string
  distinct_values: number
  threshold: number
}

/**
 * What a scan would create, bounded by three separate partialities the UI must
 * report rather than round away: the lookback window, the sample cap
 * (`sample_is_complete === false` ⇒ say "at least N"), and the event cap.
 */
export interface ScanDryRunResponse {
  window_from: string | null
  window_to: string | null
  sampled_rows: number
  sample_row_limit: number
  sample_is_complete: boolean
  breakdown_combinations: number
  events: ScanDryRunEvent[]
  events_truncated: boolean
  max_events_reached: boolean
  fields: ScanDryRunField[]
  templated_columns: ScanDryRunTemplatedColumn[]
  reserved_columns: string[]
  unmapped_columns: string[]
  warnings: string[]
  /** Name-format failures: reported, not raised — the job still completes. */
  errors: string[]
}

/** The draft a dry run is computed from. Mirrors ScanDryRunRequest. */
export interface ScanDryRunRequest {
  data_source_id: string
  base_query: string
  event_type_id?: string | null
  event_type_column?: string | null
  time_column?: string | null
  event_name_format?: string | null
  event_group_rules?: EventGroupRule[]
  json_value_paths?: string[]
  cardinality_threshold?: number
  app_version_column?: string | null
  platform_column?: string | null
  scan_lookback_hours?: number | null
  sample_row_limit?: number
}

export interface ScanDryRunJob {
  id: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  started_at: string | null
  completed_at: string | null
  /** Holds a ScanDryRunResponse when status === 'completed'; null otherwise. */
  result_summary: ScanDryRunResponse | null
  error_message: string | null
  created_at: string
  updated_at: string
}

/**
 * Canonical status for the scan run-status pill. A frontend type, not an API
 * one: it lives here rather than in the scans page module so lib/statusLexicon
 * can name it without importing a page (DS-41).
 */
export type RunPillStatus = 'succeeded' | 'failed' | 'running' | 'pending' | 'cancelled' | 'never'
