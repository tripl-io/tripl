import type { AlertInboxStatus } from './alerting'

// Exhaustive scope-type union mirroring the backend MetricScopeType enum
// (backend/src/tripl/models/domain_enums.py). Keep all eight members in sync;
// narrowing this to a subset silently mis-routes the omitted scopes.
export type MetricScopeType =
  | 'project_total'
  | 'event_type'
  | 'event'
  | 'schema'
  | 'distribution'
  | 'release_regression'
  | 'metric'
  | 'variable_value_drift'

export interface EventMetricPoint {
  bucket: string
  count: number
  expected_count: number | null
  stddev: number | null
  is_anomaly: boolean
  anomaly_direction: 'spike' | 'drop' | null
  z_score: number | null
  detector_kind?: string | null
  /**
   * The baseline the detector scored this bucket against, flagged or not: the
   * expected value and the floored effective stddev (the band is
   * `baseline_expected ± sigma_threshold * baseline_stddev`). Null where no
   * baseline was stored — older buckets, unscored ones and routes other than
   * the event, event-type and project-total drilldowns.
   */
  baseline_expected?: number | null
  baseline_stddev?: number | null
}

export interface TopEvent {
  event_id: string
  name: string
  event_type_id: string
  total_count: number
  // The project's volume over the same window, identical on every row, so a
  // row can show its event's share without a second request (MO-25). Counted
  // the project-total way, so the shares need not add up to 100%.
  window_total_count: number
}

export interface OverviewKpiSeries {
  days: number
  // Events CREATED per day on the main branch. Named `active_events` until
  // tripl-jfm3.22, which is what led the Overview sparkline to announce itself
  // as "Active events by day" while plotting creations across every branch.
  new_events: number[]
}

export interface MonitoringSignal {
  // NULL for `metric`-scope signals: catalog MetricDefinition series are
  // project-global and belong to no single scan config. The backend has always
  // said so (MetricSignalResponse.scan_config_id is `uuid.UUID | None`); this
  // declaration claimed otherwise, so a consumer could treat it as a string,
  // typecheck, and meet a null at runtime.
  scan_config_id: string | null
  scope_type: MetricScopeType
  scope_ref: string
  state: 'latest_scan' | 'recent'
  event_id: string | null
  event_type_id: string | null
  bucket: string
  actual_count: number
  expected_count: number
  stddev: number
  z_score: number
  direction: 'spike' | 'drop'
  // How big this signal is relative to what was expected — what the Significant
  // gate and the sidebar badge both read. Computed server-side because only it
  // knows whether a catalog metric's series is count-shaped, which decides
  // whether the denominator is floored at 1 (tripl-yf8c). Null on a payload
  // that did not compute it; `relativeEffect` falls back to the count-shaped
  // estimate there rather than treating the signal as having no magnitude.
  relative_effect?: number | null
  // Display name of the scope that fired, resolved server-side. Null when it
  // could not be resolved (the entity was deleted) or when the scope names
  // itself — `project_total`. Never substitute `scope_ref`: a hex prefix reads
  // as a name, and the page then disagrees with the activity rail about what
  // fired (tripl-y4wt). Optional like `scan_config_name`: the server always
  // sends it, but locally-synthesised signals need not fabricate one.
  scope_name?: string | null
  // True when this row is a child scope (event_type/event) folded under a
  // co-firing project_total incident. Only the expanded AnomaliesPage fetch
  // sets it; collapsed callers drop children so it is always false there.
  incident_child: boolean
  // Display unit of a `metric`-scope signal's catalog metric ("%", "ms"…);
  // null for every other scope and for a unitless metric (MON-34), and on a
  // locally-synthesised signal.
  unit: string | null
  // When the detector wrote the anomaly — distinct from `bucket`, when the
  // anomalous period STARTED (MON-40). Null on a payload that predates it.
  detected_at: string | null
  // The Alerting Inbox incident this signal was routed into and its status
  // there, so an Anomalies row can link to the incident (JR-6). Null when no
  // rule delivered it; filled on the expanded list only.
  incident_id?: string | null
  incident_status?: AlertInboxStatus | null
  // Triage of a signal NO rule routed to an incident (MO-4 / JR-5); a signal
  // with `incident_id` is triaged in the inbox and never carries these. Read
  // `muted_until` together with `muted`: null means both "not muted" and
  // "muted until someone unmutes". `hidden` (muted or expected) is what every
  // open-signal count gates on; the collapsed list drops hidden signals, the
  // expanded list keeps them so the Anomalies page can offer "Show hidden".
  // Optional: locally-synthesised signals need not carry them.
  acknowledged_at?: string | null
  muted?: boolean
  muted_until?: string | null
  expected?: boolean
  expected_note?: string | null
  hidden?: boolean
}

/** The scope a triage verdict is about, keyed like the signal (MO-4 / JR-5). */
export interface SignalTriageScope {
  // Null for a catalog `metric` scope; required for every other one.
  scan_config_id: string | null
  scope_type: MetricScopeType
  scope_ref: string
  bucket: string
}

export type SignalMuteDuration = '24h' | '7d' | 'until_unmuted'

/** A signal's triage fields after a write, as the lists will show them. */
export interface SignalTriageState {
  acknowledged_at: string | null
  muted: boolean
  muted_until: string | null
  expected: boolean
  expected_note: string | null
  hidden: boolean
}

export interface TopMoverItem {
  breakdown_column: string
  breakdown_value: string
  is_other: boolean
  actual_count: number
  expected_count: number
  stddev: number
  z_score: number
  direction: 'spike' | 'drop'
}

export type DistributionDriftBand = 'stable' | 'minor' | 'significant'

export interface DistributionDriftTopMover {
  value: string
  baseline_share: number
  current_share: number
  contribution: number
}

export interface DistributionDriftPoint {
  id: string
  scan_config_id: string
  event_type_id: string | null
  field_name: string
  bucket: string
  psi: number
  band: DistributionDriftBand
  baseline_total: number
  current_total: number
  top_movers: DistributionDriftTopMover[]
}

export interface DistributionDriftsResponse {
  scope: 'project_total' | 'event_type' | 'event'
  scan_config_id: string | null
  event_type_id: string | null
  fields: string[]
  data: DistributionDriftPoint[]
}

export interface ForecastPoint {
  bucket: string
  expected_count: number
  stddev: number
}

/**
 * Who made an annotation: a person in the form (`manual`), the metrics worker
 * when an app version activated (`release`), or a deploy script through the
 * API or CLI (`api`).
 */
export type ChartAnnotationSource = 'manual' | 'release' | 'api'

export interface ChartAnnotation {
  id: string
  project_id: string
  scope_type: 'project_total' | 'event_type' | 'event' | 'metric' | null
  scope_ref: string | null
  bucket: string
  label: string
  description: string | null
  color: string
  source: ChartAnnotationSource
  /** A release note, deploy or changelog link (http/https only). */
  url: string | null
  created_by_user_id: string | null
  created_at: string
}

export interface SeasonalityCell {
  weekday: number
  hour: number
  count: number
  anomaly_count: number
}

export interface SeasonalityHeatmap {
  scan_config_id: string
  scope_type: MetricScopeType
  scope_ref: string
  cells: SeasonalityCell[]
  max_count: number
  total_count: number
  // The scan interval the cells were binned from. A daily/weekly scan puts every
  // bucket in hour 0, so the 7×24 grid can never fill and must not be drawn
  // (tripl-jfm3.128).
  interval: string
  hourly_resolution: boolean
}

export interface BreakdownTimelinePoint {
  bucket: string
  count: number
}

export interface BreakdownTimeline {
  scan_config_id: string
  scope_type: MetricScopeType
  scope_ref: string
  breakdown_column: string
  breakdown_value: string
  is_other: boolean
  interval: string | null
  data: BreakdownTimelinePoint[]
}

export interface EventMetricsResponse {
  scope: 'project_total' | 'event_type' | 'event' | 'events_total'
  scan_config_id: string | null
  // Display name of the single scan config the `project_total` / `events_total`
  // series is scoped to, so the chart can name its scope instead of implying
  // it covers every scan in the project (tripl-jfm3.20). Optional like
  // `sigma_threshold`: the server always sends it (null for scopes that have no
  // single scan), but locally-synthesised responses need not fabricate one.
  scan_config_name?: string | null
  event_id: string | null
  event_type_id: string | null
  interval: string | null
  latest_signal: MonitoringSignal | null
  sigma_threshold?: number
  // When the scan's newest completed collection finished, and the earliest
  // moment the scheduler dispatches the next one (equal to the response time
  // when collection is due now). Null without a scan config or interval (L4).
  last_collected_at?: string | null
  next_collection_at?: string | null
  // The Events tab's series only: volume over the 7 days ending at the request's
  // upper bound and the 7 before, whatever the chart's range (EV-21).
  week_total?: number | null
  prior_week_total?: number | null
  data: EventMetricPoint[]
  forecast: ForecastPoint[]
}

/** One signal a row sparkline asks `POST /anomalies/signals/series` for (MO-19). */
export interface SignalSeriesScope {
  scan_config_id: string
  scope_type: 'project_total' | 'event_type' | 'event'
  scope_ref: string
  bucket: string
}

/**
 * Up to 24 buckets around one signal's flagged bucket: 19 before it, the
 * bucket, and up to 4 after. Interior gaps are zero-filled; buckets past the
 * newest stored one are absent.
 */
export interface SignalSeries extends SignalSeriesScope {
  interval: string | null
  data: { bucket: string; count: number }[]
}

export interface EventMetricBreakdownSeries {
  breakdown_value: string
  is_other: boolean
  total_count: number
  data: EventMetricPoint[]
  parity_anomalies: PlatformParityAnomaly[]
}

export interface PlatformParityAnomaly {
  bucket: string
  actual_share: number
  expected_share: number
  stddev: number
  z_score: number
  direction: 'spike' | 'drop'
}

export interface EventMetricBreakdownsResponse {
  event_id: string
  scan_config_id: string | null
  interval: string | null
  columns: string[]
  selected_column: string | null
  series: EventMetricBreakdownSeries[]
}

export interface AppVersionInfo {
  version: string
  is_other: boolean
  is_latest: boolean
  is_active: boolean
}

export interface AppVersionMetricSeries {
  version: string
  is_other: boolean
  is_latest: boolean
  is_active: boolean
  total_count: number
  data: EventMetricPoint[]
}

export interface AppVersionSeriesResponse {
  scan_config_id: string
  scope_type: MetricScopeType
  scope_ref: string
  event_id: string | null
  event_type_id: string | null
  app_version_column: string | null
  interval: string | null
  latest_version: string | null
  sigma_threshold?: number
  versions: AppVersionInfo[]
  series: AppVersionMetricSeries[]
}

export interface AppVersionAdoptionResponse extends AppVersionSeriesResponse {
  totals: BreakdownTimelinePoint[]
}

export interface ReleaseRegressionItem {
  scope_type: MetricScopeType
  scope_ref: string
  scope_name: string
  event_id: string | null
  event_type_id: string | null
  kind: 'missing' | 'volume_drop'
  version: string
  previous_version: string
  observed_count: number
  expected_count: number
  ratio: number
  share_prev: number
  share_new: number
  release_share: number
  window_from: string
  window_to: string
}

export type ReleaseComparabilityReason =
  | 'comparable'
  | 'no_baseline'
  | 'baseline_no_volume'
  | 'population_mismatch'

/**
 * Verdict of one release-regression pass. An empty `items` means "nothing
 * regressed" only when the matching verdict says `comparable`; otherwise the
 * findings were withheld and the release cannot be judged yet.
 */
export interface ReleaseComparabilityItem {
  scope_type: MetricScopeType
  comparable: boolean
  reason: ReleaseComparabilityReason
  version: string | null
  previous_version: string | null
  emerging_share: number
  max_emerging_share: number
}

export interface ReleaseRegressionsResponse {
  scan_config_id: string
  app_version_column: string | null
  latest_version: string | null
  comparability: ReleaseComparabilityItem[]
  items: ReleaseRegressionItem[]
}

export interface EventWindowMetrics {
  event_id: string
  scan_config_id: string | null
  interval: string | null
  total_count: number
  data: EventMetricPoint[]
}
