import type { components } from './api.gen'

// Scan payloads, taken from the generated OpenAPI schema (`api.gen.ts`) rather
// than restated, so a backend change is a compile error where it is read. A
// field the backend declares with a `None` default is optional here even
// though the server always sends it: read it with `== null` or `??`.
//
// Three shapes stay hand-written because the backend serves them as an untyped
// dict (`result_summary`): ScanJobResultSummary and the ScanPreview* family.
// ScanDryRunResponse stays hand-written too (its consumers read the lists as
// always present); `types/apiDrift.ts` holds it to the generated type.
type Schemas = components['schemas']

/** A job's `result_summary`: an untyped dict in the schema, read as this. */
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
export type SourceFreshnessStatus = SourceFreshness['status']

/**
 * `lag_seconds` is the seconds between now and `last_event_at`, null without
 * an observed event. `last_event_at` is the start of the newest bucket that
 * had events, as the latest successful metrics collection saw it: bucket
 * resolution, so it can trail the newest event by up to one scan interval.
 * `last_collection_at` is when that collection completed; `expected_by` the
 * moment past which the source counts as late, null when unknown.
 */
export type SourceFreshness = Schemas['SourceFreshness']

/** One scan's row in `GET /projects/{slug}/source-freshness`. */
export type SourceFreshnessItem = Schemas['SourceFreshnessItem']

export type ProjectLatestScanJob = Omit<Schemas['ProjectLatestScanJob'], 'result_summary'> & {
  result_summary: ScanJobResultSummary | null
}

/** `state` is `latest_scan` or `recent` (a plain string in the schema). */
export type ProjectLatestSignal = Schemas['ProjectLatestSignal']

export type IntervalCode = Schemas['ScanInterval']

export type EventGroupCondition = Schemas['EventGroupCondition']
export type EventGroupRule = Schemas['EventGroupRule']

/**
 * A scan as `GET /scans` and `PATCH /scans/{id}` return it.
 *
 * `setup_preset` says how it was set up (F23.4c): `event_properties` names
 * events from `event_name_column` and catalogues every key of
 * `properties_column` as a property, the backend deriving the name format,
 * JSON value paths, Event type column and group rules from those two columns;
 * `custom` takes every field as set. `json_string_columns` are String
 * (ClickHouse) / STRING (BigQuery, Databricks) columns every read of the
 * source parses as JSON (F23.9), so their keys become properties like a JSON
 * column's. `monitoring_enabled` is the server-derived `interval IS NOT NULL`;
 * the views keep `scanModeOf`, which also needs a time column (an interval
 * without one is `misconfigured`, a state this flag alone would report as
 * monitoring). `freshness` is computed on read (F16, #269).
 */
export type ScanConfig = Schemas['ScanConfigResponse']
export type ScanSetupPreset = ScanConfig['setup_preset']

/**
 * `GET /scans/{id}`: a ScanConfig plus when the newest scheduled metrics
 * collection finished and the earliest moment the scheduler considers the scan
 * due again — null for a scan it never collects.
 */
export type ScanConfigDetail = Schemas['ScanConfigDetailResponse']

export type PlatformPresenceRow = Schemas['PlatformPresenceRow']
export type PlatformPresenceResponse = Schemas['PlatformPresenceResponse']

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

/** One key of the properties column, as the preview's sample rows carry it. */
export interface ScanPreviewEventProperty {
  path: string
  /** Share of the event's sample rows that carried the key, 0..1. */
  presence: number
  /** The type a run would infer, or null when the sample's kinds disagree. */
  type: string | null
  sample_values: string[]
}

export interface ScanPreviewEvent {
  name: string
  sample_rows: number
  properties: ScanPreviewEventProperty[]
}

/** What the "event + properties" preset yields from the preview's sample. */
export interface ScanPreviewEventProperties {
  event_name_column: string
  properties_column: string
  sample_rows: number
  events: ScanPreviewEvent[]
  error: string | null
}

export interface ScanConfigPreview {
  columns: ScanPreviewColumn[]
  rows: Record<string, unknown>[]
  json_columns: ScanPreviewJsonColumn[]
  /** Present when the preview was asked with both preset columns. */
  event_properties?: ScanPreviewEventProperties | null
}

/** `result_summary` holds a ScanConfigPreview when `status` is `completed`. */
export type ScanPreviewJob = Omit<Schemas['ScanPreviewJobResponse'], 'result_summary'> & {
  result_summary: ScanConfigPreview | null
}

/**
 * A project's anomaly detection settings. `id`, `created_at` and `updated_at`
 * are null on a project that never saved its settings: the GET answers with
 * the defaults and stores no row. Catalog metrics (`detect_metrics`) are
 * scored on their own series, independent of the event-scope flags.
 * `recent_signal_window_hours` is how long a detected anomaly keeps counting
 * as an open signal; `anomaly_ingestion_settling_minutes` the wall-clock
 * allowance for the warehouse to finish delivering a bucket before it is
 * scored; `holiday_country` the ISO 3166-1 alpha-2 code whose public holidays
 * become expected windows, null for none.
 */
export type ProjectAnomalySettings = Schemas['ProjectAnomalySettingsResponse']

/**
 * One scope the false-positive ratchet has made stricter.
 *
 * Marking an alert a false positive raises `sigma_threshold` and
 * `min_expected_count` for the scope it fired on — and only that scope. The
 * values here are ABSOLUTE and replace the project settings for that scope.
 * The ratchet never decays, so deleting the override is the only way back.
 * `scan_config_id` is null for `metric` scopes: catalog metric series are
 * project-global.
 */
export type AnomalyScopeOverride = Schemas['AnomalyScopeOverrideResponse']
export type AnomalyScopeOverrideList = Schemas['AnomalyScopeOverrideListResponse']

export type ScanJob = Omit<Schemas['ScanJobResponse'], 'result_summary'> & {
  result_summary: ScanJobResultSummary | null
}

/**
 * The Scans list's per-scan figures, aggregated in SQL over the whole history.
 * `failing_streak` counts consecutive failed runs, newest first, looking past
 * queued/running jobs. `rows_read_24h` mixes two units: `warehouse_rows_24h`
 * (rows metrics runs read) plus `catalog_combinations_24h` (the GROUP BY ALL
 * combinations catalog runs read back) over the same window (#247).
 */
export type ScanActivityItem = Omit<Schemas['ScanActivityItem'], 'latest_job'> & {
  latest_job: ScanJob | null
}

export type ScanActivityResponse = Omit<Schemas['ScanActivityResponse'], 'items'> & {
  items: ScanActivityItem[]
}

// ─── Dry run: "what would this scan create?" ──────────────────────────────────
// The backend's ScanDryRun* Pydantic models (schemas/scan_config.py). The
// payload is computed by the SAME planner a real run uses, so the names below
// are the names a run would write — never a second implementation of
// generation in TypeScript.

/**
 * One event the config would produce, and how much of the sample it is.
 *
 * Identified by `(event_type, source_name)`, never by the name alone: a run
 * writes one Event per event type, so a grouped scan whose name format collapses
 * to the same string under two event types creates two events.
 */
export type ScanDryRunEvent = Schemas['ScanDryRunEvent']

/**
 * A field the config would add to the plan. `type` is json-or-string and nothing
 * else — that is the entire type inference a scan performs, and promising
 * `integer`/`timestamp` would be a claim about something it does not do.
 */
export type ScanDryRunField = Schemas['ScanDryRunField']

/** A column the cardinality rule collapsed into a `{column}` template. */
export type ScanDryRunTemplatedColumn = Schemas['ScanDryRunTemplatedColumn']

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
  /** Problems with the names a run would create (F12, #265). Best-effort:
   *  empty never means "checked and clean" when the check could not run.
   *  Optional so an answer from before the field existed still renders. */
  name_warnings?: ScanDryRunNameWarning[]
}

/** The planned event a would-be-new name looks like. */
export type ScanDryRunDuplicateOf = Schemas['ScanDryRunDuplicateOf']

/**
 * `combinatorial_explosion`: `count` new names under one event type differ
 * only in one slot (`slot` / `slot_label`; `pattern` shows the fixed parts
 * with `*`), i.e. a high-cardinality value is part of the name.
 * `duplicate`: the new event `name` looks like `duplicate_of`, already planned.
 */
export type ScanDryRunNameWarning = Schemas['ScanDryRunNameWarning']

/** Fields the backend defaults, which the generated type marks required. */
type ScanDryRunDefaulted = 'setup_preset' | 'cardinality_threshold' | 'sample_row_limit'

/** The draft a dry run is computed from. */
export type ScanDryRunRequest = Omit<Schemas['ScanDryRunRequest'], ScanDryRunDefaulted> &
  Partial<Pick<Schemas['ScanDryRunRequest'], ScanDryRunDefaulted>>

export type ScanDryRunJob = Omit<Schemas['ScanDryRunJobResponse'], 'result_summary'> & {
  result_summary: ScanDryRunResponse | null
}

/**
 * Canonical status for the scan run-status pill. A frontend type, not an API
 * one: it lives here rather than in the scans page module so lib/statusLexicon
 * can name it without importing a page.
 */
export type RunPillStatus = 'succeeded' | 'failed' | 'running' | 'pending' | 'cancelled' | 'never'
