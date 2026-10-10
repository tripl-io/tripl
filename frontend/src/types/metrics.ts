import type { components } from './api.gen'

// Metric, signal and chart payloads, taken from the generated OpenAPI schema
// (`api.gen.ts`) rather than restated, so a backend change to any of them is a
// compile error where it is read instead of a silent drift.
//
// A field the backend declares with a `None` default is optional here
// (`field?: T | null`) even though the server always sends it, null when
// unset. Read such a field with `== null`, `!= null` or `??`, never with
// `=== null`: TypeScript treats it as possibly undefined.
type Schemas = components['schemas']

/**
 * Every scope a signal or alert can be about (backend MetricScopeType):
 * `project_total`, `event_type`, `event`, `schema`, `distribution`,
 * `release_regression`, `metric`, `variable_value_drift`, `source_freshness`
 * (scope_ref is the scan config id), `lifecycle` (an open lifecycle finding,
 * #258) and `property_drift` (scope_ref is the drift row id, F23).
 */
export type MetricScopeType = Schemas['MetricScopeType']

/**
 * One bucket of an event-volume series. `baseline_expected` and
 * `baseline_stddev` are the baseline the detector scored the bucket against,
 * flagged or not (the band is `baseline_expected ± sigma_threshold *
 * baseline_stddev`), null where none was stored. Only a flagged bucket
 * carries `verdict` (#254) and, when a planned event expected it,
 * `planned_event_id` (F18).
 */
export type EventMetricPoint = Schemas['EventMetricPoint']

/**
 * What a person decided a signal was (#254). `expected` keeps its older
 * behaviour — a chart annotation on the bucket and the signal hidden — and
 * its reason only documents it; `false_positive` tunes detection the way an
 * incident's does; `tracking_bug` and `real_issue` record the finding.
 *
 * The generated schema calls this enum `SignalVerdict`. It is
 * `SignalVerdictKind` here, and the verdict object is `SignalVerdictInfo`,
 * so no name means both.
 */
export type SignalVerdictKind = Schemas['SignalVerdict']

/** Why an `expected` signal was expected; documents only, suppresses nothing. */
export type SignalExpectedReason = Schemas['SignalExpectedReason']

/**
 * A signal's verdict as the read payloads carry it. `source: 'incident'`
 * means the signal belongs to an incident and the verdict shown is the
 * incident's state: the incident is the source of truth, so the signal's own
 * verdict cannot be cleared from here. `author_name` is null once the
 * author's account is gone and on a status the system changed; `created_at`
 * is null when the time is not known.
 */
export type SignalVerdictInfo = Schemas['SignalVerdictInfo']

/** The incident a signal was routed into, as the verdict payloads name it. */
export type SignalIncidentRef = Schemas['SignalIncidentBrief']

/** `POST /projects/{slug}/signals/verdict`: a verdict on one signal. */
export type SignalVerdictRequest = Schemas['SignalVerdictRequest']

/**
 * One row of the Overview's top events. `window_total_count` is the
 * project's volume over the same window, counted the project-total way, so
 * the shares need not add up to 100%.
 */
export type TopEvent = Schemas['TopEventResponse']

/** Events created per day on the main branch, for the Overview sparkline. */
export type OverviewKpiSeries = Schemas['OverviewKpiSeriesResponse']

/**
 * An open signal (`GET /anomalies/signals`, the metric payloads'
 * `latest_signal`).
 *
 * - `scan_config_id` is null for a catalog `metric` scope, which belongs to
 *   no single scan config.
 * - `relative_effect` is how big the signal is next to what was expected,
 *   computed server-side because only the server knows whether a catalog
 *   metric is count-shaped.
 * - `scope_name` is the display name of the scope that fired, null when it
 *   could not be resolved or the scope names itself (`project_total`). Never
 *   substitute `scope_ref`: a hex prefix reads as a name.
 * - `incident_child` marks a child scope folded under a co-firing
 *   project-total incident (expanded fetch only).
 * - `incident_id`/`incident_status`: the inbox incident a rule routed it
 *   into. A routed signal is triaged in the inbox; the triage fields
 *   (`acknowledged_at`, `muted`, `muted_until`, `expected`, `expected_note`,
 *   `hidden`) belong to an unrouted one. `hidden` (muted or expected) is
 *   what every open-signal count gates on.
 * - `verdict` (#254) is the signal's own, or its incident's state.
 * - `attribution` (#255) splits the flagged bucket's delta across the
 *   scan's breakdown columns; `anomaly_id` is what the attribution endpoint
 *   takes.
 * - `owners` (F07, #260) are the owners of its event type or catalog metric.
 */
export type MonitoringSignal = Schemas['MetricSignalResponse']

/**
 * Whether a signal has an attribution (#255): `ready` carries one;
 * `no_breakdown_columns` is a volume signal whose scan splits by nothing, so
 * there is nothing to attribute; `not_computed` is everything else — a signal
 * older than the feature, or a scope attribution does not cover.
 */
export type SignalAttributionStatus = MonitoringSignal['attribution_status']

/**
 * One breakdown value's part of the flagged bucket's delta. `share` is its
 * delta over the scope's delta, SIGNED and not clipped: never render it as a
 * raw 0..1 share.
 */
export type SignalAttributionValue = Schemas['AttributionValue']

/**
 * One breakdown column's split of the delta, its top values first.
 * `explained_share` is the top values' same-sign contribution, clipped 0..1.
 */
export type SignalAttributionColumn = Schemas['AttributionColumn']

/** An app version that crossed the activation gate within the anomaly's window. */
export type SignalAttributionRelease = Schemas['AttributionRelease']

/**
 * Why a signal changed (#255). `headline` and `release_line` are worded by
 * the backend, the same lines the alert carries: render them verbatim.
 */
export type SignalAttribution = Schemas['SignalAttribution']

/** The scope a triage verdict is about, keyed like the signal. */
export type SignalTriageScope = Schemas['SignalTriageScope']

export type SignalMuteDuration = Schemas['SignalMuteRequest']['duration']

/** A signal's triage fields after a write, as the lists will show them. */
export type SignalTriageState = Schemas['SignalTriageState']

export type TopMoverItem = Schemas['TopMoverItem']

export type DistributionDriftBand = Schemas['DistributionDriftBand']
export type DistributionDriftTopMover = Schemas['DistributionDriftTopMover']
export type DistributionDriftPoint = Schemas['DistributionDriftPoint']
export type DistributionDriftsResponse = Schemas['DistributionDriftsResponse']

export type ForecastPoint = Schemas['ForecastPoint']

/**
 * Who made an annotation: a person in the form (`manual`), the metrics worker
 * when an app version activated (`release`), or a deploy script through the
 * API or CLI (`api`).
 */
export type ChartAnnotationSource = Schemas['ChartAnnotationSource']

/**
 * A chart annotation. `url` is a release note, deploy or changelog link
 * (http/https only); `scope_name`, the scoped series' name, is set on list
 * responses and null when project-wide or gone.
 */
export type ChartAnnotation = Schemas['ChartAnnotationResponse']

/**
 * A window in which the project expects its numbers to move (F18): a campaign,
 * a sale, a holiday. Anomalies inside it are drawn but raise no alert.
 * `direction` null expects either way; a null scope covers every chart.
 * `source` is `manual` or `holiday`; holiday rows come from the project's
 * holiday calendar and are read-only.
 */
export type PlannedEvent = Schemas['PlannedEventResponse']

/**
 * A recurring window people keep marking expected (#271): the same series at
 * the same UTC weekday (0 = Monday) and hour. Accepting it is creating
 * `windows` as planned events.
 */
export type PlannedWindowSuggestion = Schemas['PlannedWindowSuggestionResponse']

export type SeasonalityCell = Schemas['SeasonalityCell']

/**
 * A weekday × hour heatmap. `interval` is the scan interval the cells were
 * binned from: a daily or weekly scan puts every bucket in hour 0, so
 * without `hourly_resolution` the 7×24 grid can never fill and is not drawn.
 */
export type SeasonalityHeatmap = Schemas['SeasonalityHeatmapResponse']

export type BreakdownTimelinePoint = Schemas['BreakdownTimelinePoint']
export type BreakdownTimeline = Schemas['BreakdownTimelineResponse']

/**
 * An event-volume series. `scope` is `project_total`, `event_type`, `event`
 * or `events_total`. `scan_config_name` names the single scan a
 * `project_total`/`events_total` series is scoped to. `last_collected_at` and
 * `next_collection_at` are when the scan's newest collection finished and
 * the earliest the scheduler dispatches the next one. `week_total` and
 * `prior_week_total` (the Events tab's series only) are the volume over the
 * 7 days ending at the request's upper bound and the 7 before.
 */
export type EventMetricsResponse = Schemas['EventMetricsResponse']

/** One signal a row sparkline asks `POST /anomalies/signals/series` for. */
export type SignalSeriesScope = Schemas['SignalSeriesScope']

/**
 * Up to 24 buckets around one signal's flagged bucket: 19 before it, the
 * bucket, and up to 4 after. Interior gaps are zero-filled; buckets past the
 * newest stored one are absent.
 */
export type SignalSeries = Schemas['SignalSeriesResponse']

export type EventMetricBreakdownSeries = Schemas['EventMetricBreakdownSeries']
export type PlatformParityAnomaly = Schemas['PlatformParityAnomaly']
export type EventMetricBreakdownsResponse = Schemas['EventMetricBreakdownsResponse']

export type AppVersionInfo = Schemas['AppVersionInfo']
export type AppVersionMetricSeries = Schemas['AppVersionMetricSeries']
export type AppVersionSeriesResponse = Schemas['AppVersionSeriesResponse']
export type AppVersionAdoptionResponse = Schemas['AppVersionAdoptionResponse']

export type ReleaseRegressionItem = Schemas['ReleaseRegressionItem']

export type ReleaseComparabilityReason = Schemas['ReleaseComparabilityReason']

/**
 * Verdict of one release-regression pass. An empty `items` means "nothing
 * regressed" only when the matching verdict says `comparable`; otherwise the
 * findings were withheld and the release cannot be judged yet.
 */
export type ReleaseComparabilityItem = Schemas['ReleaseComparabilityItem']

export type ReleaseRegressionsResponse = Schemas['ReleaseRegressionsResponse']

export type EventWindowMetrics = Schemas['EventWindowMetricsResponse']
