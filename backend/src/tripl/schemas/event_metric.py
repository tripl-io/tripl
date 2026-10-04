import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from tripl.models.domain_enums import (
    AlertInboxStatus,
    AnomalyDirection,
    DistributionDriftBand,
    MetricScopeType,
    ReleaseComparabilityReason,
    ReleaseRegressionKind,
    ScanInterval,
    SignalExpectedReason,
    SignalVerdict,
)
from tripl.models.project_anomaly_settings import DEFAULT_SIGMA_THRESHOLD
from tripl.schemas.alert_owner import AlertOwnerRef

SignalVerdictSource = Literal["signal", "incident"]


class SignalVerdictInfo(BaseModel):
    """What a signal turned out to be (F01, #254).

    ``source`` is ``signal`` when the signal's own verdict row supplies it and
    ``incident`` when it is read off the status of the incident the signal was
    routed into (the incident is the source of truth): acknowledged reads as
    ``real_issue``, resolved as ``expected``, false_positive as
    ``false_positive``. A signal verdict row consistent with the incident's
    status refines that reading (``tracking_bug`` on an acknowledged incident,
    a reason on an expected one) and wins; one the incident has since moved
    away from is ignored.
    """

    verdict: SignalVerdict
    expected_reason: SignalExpectedReason | None = None
    note: str | None = None
    author_name: str | None = None
    # When the verdict was last set.
    created_at: datetime | None = None
    source: SignalVerdictSource


class SignalIncidentBrief(BaseModel):
    """The inbox incident a signal was routed into, for a status chip + link."""

    id: uuid.UUID
    status: AlertInboxStatus


class EventMetricPoint(BaseModel):
    bucket: datetime
    count: int
    expected_count: float | None = None
    # The FLOORED "effective" stddev actually used in the z denominator when the
    # bucket was flagged — served in place of the raw rolling
    # stddev so the UI band (expected ± sigma_threshold * stddev) lines up exactly
    # with the detector's decision: a flagged point sits outside the band. Only
    # populated for buckets with an anomaly row; for the rest the band is undrawn.
    # Falls back to the raw stored stddev when the effective column is absent.
    stddev: float | None = None
    # Which detector path flagged this bucket ("phase" | "rolling" | "trend" |
    # "fractional"); null on non-anomaly buckets. Advisory metadata for the UI.
    detector_kind: str | None = None
    is_anomaly: bool = False
    anomaly_direction: AnomalyDirection | None = None
    z_score: float | None = None
    # The verdict on the signal this flagged bucket raised, for the chart
    # marker's tooltip (F01, #254). Only on the drilldown routes, only on
    # flagged buckets, and omitted from the JSON when NULL.
    verdict: SignalVerdictInfo | None = Field(default=None, exclude_if=lambda value: value is None)
    # The planned event that expected this flagged bucket (F18): the chart
    # mutes the marker and names the event. Omitted from the JSON when NULL.
    planned_event_id: uuid.UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    # The per-bucket baseline the detector scored this bucket against, flagged
    # or not: the expected value and the floored effective
    # stddev, so ``baseline_expected ± sigma_threshold * baseline_stddev`` is the
    # band the chart draws on every scored bucket. NULL where the detector
    # stored none — buckets scored before baselines were persisted, buckets it
    # skipped (too little history, under the volume floor, still settling), and
    # every route other than the event, event-type and project-total drilldowns.
    # On a flagged bucket ``expected_count``/``stddev`` stay authoritative: a
    # trend row's expectation is not the per-bucket one. Omitted from the JSON
    # when NULL: most routes never carry one, and a sparkline or events-window
    # payload has no use for two nulls on every point.
    baseline_expected: float | None = Field(default=None, exclude_if=lambda value: value is None)
    baseline_stddev: float | None = Field(default=None, exclude_if=lambda value: value is None)


class PlatformParityAnomaly(BaseModel):
    bucket: datetime
    actual_share: float
    expected_share: float
    stddev: float
    z_score: float
    direction: AnomalyDirection


AttributionStatus = Literal["ready", "no_breakdown_columns", "not_computed"]


class AttributionValue(BaseModel):
    """One breakdown value's part of a signal's delta (F02, #255).

    ``delta`` is ``actual - expected`` for the value, where ``expected`` is the
    scope's expected total times the value's baseline share; ``share`` is
    ``delta`` over the signal's delta (signed: a value moving against the
    change is negative).
    """

    value: str
    delta: float
    expected: float
    actual: float
    share: float


class AttributionColumn(BaseModel):
    """One breakdown column's split: its top values and the part of the change
    they explain (0..1)."""

    column: str
    explained_share: float
    values: list[AttributionValue] = Field(default_factory=list)


class AttributionRelease(BaseModel):
    """A release that crossed the activation gate shortly before the signal."""

    version: str
    previous_version: str | None = None
    # The release's share of traffic at the flagged bucket, 0..1.
    share: float
    # The bucket the release activated in.
    reached_at: datetime


class SignalAttribution(BaseModel):
    """Why the signal changed, computed and stored when it was detected."""

    delta: float
    columns: list[AttributionColumn] = Field(default_factory=list)
    release: AttributionRelease | None = None
    # The one-liners alerts carry too, e.g. "92% of the drop comes from
    # platform = ios (−3,120 of −3,390)" and "Release 4.12 reached 38% of
    # traffic 3h before the drop". NULL when there is nothing to say.
    headline: str | None = None
    release_line: str | None = None
    computed_at: datetime | None = None


class AnomalyAttributionResponse(BaseModel):
    """``GET /projects/{slug}/anomalies/{anomaly_id}/attribution``."""

    anomaly_id: uuid.UUID
    scan_config_id: uuid.UUID | None = None
    attribution_status: AttributionStatus
    attribution: SignalAttribution | None = None


class MetricSignalResponse(BaseModel):
    # NULL for ``metric``-scope signals (catalog MetricDefinition series are
    # project-global and not tied to a single scan config).
    scan_config_id: uuid.UUID | None = None
    scope_type: MetricScopeType
    scope_ref: str
    state: str
    event_id: uuid.UUID | None = None
    event_type_id: uuid.UUID | None = None
    bucket: datetime
    actual_count: float
    expected_count: float
    stddev: float
    z_score: float
    direction: AnomalyDirection
    # How big this signal is relative to what was expected — the value the
    # "Significant" magnitude filter and the sidebar badge both gate on. Computed
    # here rather than mirrored client-side: the formula lived in two places and
    # drifted twice, and only the server knows
    # whether a catalog metric's series is count-shaped, which decides whether
    # the denominator is floored at 1. Always a FINITE number: an
    # unbounded move off a zero baseline is reported as
    # ``metrics_insights_service.MAX_RELATIVE_EFFECT``, because ``inf`` serializes
    # to JSON null and the client then re-derives a magnitude below the gate.
    # NULL only on a path that did not compute it; a client seeing
    # NULL should fall back to its own count-shaped estimate rather than treat the
    # signal as having no magnitude.
    relative_effect: float | None = None
    # Display name of the scope that fired — the event name, the event type's
    # display name, or the catalog metric's display name. Carried here so a
    # client can label the row from the signal alone: the AnomaliesPage used to
    # download the whole event catalog (2641 rows / 1.7s on acme-ios) purely to
    # build an id -> name map, and rendered "Spike on Event d4c684dd" until it
    # landed, while the activity rail called the same incident by its real name.
    # NULL means the name could not be resolved — the entity was
    # deleted out from under the anomaly row, or the scope is ``project_total``,
    # which is named by the project, not by a lookup. Clients must not fall back
    # to ``scope_ref``: a hex prefix reads as a name.
    scope_name: str | None = None
    # True when this row is a child scope (event_type/event) folded under a
    # co-firing project_total incident on the same scan/bucket/direction. The
    # expanded AnomaliesPage keeps children visible but tags them; the default
    # (collapsed) list drops them entirely, so they never carry this flag there.
    incident_child: bool = False
    # Display unit of a ``metric``-scope signal's catalog metric (``"%"``,
    # ``"ms"``…) so the client can print "4.2 %" rather than a bare number.
    # NULL for every other scope, for a unitless metric and for one whose
    # definition is gone (MON-34).
    unit: str | None = None
    # When the detector wrote this anomaly — distinct from ``bucket``, which is
    # when the anomalous period STARTED. Lets a list say "detected 3m ago" next
    # to a bucket that began an hour earlier (MON-40). NULL only on a path that
    # did not build the signal from a stored anomaly row.
    detected_at: datetime | None = None
    # The Alerting Inbox incident (correlation group) this signal was routed
    # into, and its effective status there, so the Anomalies row can link to
    # the incident card (JR-6). Both NULL when no rule delivered it. Filled on
    # the EXPANDED list only, and after the signals cache: triage changes an
    # incident's status without touching any signal.
    incident_id: uuid.UUID | None = None
    incident_status: AlertInboxStatus | None = None
    # Triage of a signal NO rule routed to an incident (MO-4 / JR-5); a signal
    # with ``incident_id`` is triaged in the inbox and never carries these.
    # Filled after the signals cache, like the incident fields, so a click shows
    # up on the next fetch. ``muted`` is the live state (a lapsed mute reads
    # False); ``muted_until`` is NULL both when unmuted and when muted until
    # someone unmutes, so read it together with ``muted``. ``hidden`` is the one
    # flag every count gates on: muted or expected. The collapsed list drops
    # hidden signals outright; the expanded list keeps them so the Anomalies
    # page can offer "Show hidden".
    acknowledged_at: datetime | None = None
    muted: bool = False
    muted_until: datetime | None = None
    expected: bool = False
    expected_note: str | None = None
    hidden: bool = False
    # The signal's verdict and, when a rule routed it, its incident (F01, #254).
    # Filled on both lists and after the cache, like the triage fields. The
    # sidebar badge and the Overview headline count only signals whose
    # ``verdict`` is NULL (and that are not ``hidden``); ``acknowledged_at`` is
    # not a verdict.
    verdict: SignalVerdictInfo | None = None
    incident: SignalIncidentBrief | None = None
    # The stored anomaly row this signal was built from, for the lazy
    # attribution route. NULL on a path that did not build it from a row.
    anomaly_id: uuid.UUID | None = None
    # "Why did it change?" (F02, #255): the contribution breakdown stored with
    # the anomaly at detection time, filled after the signals cache like the
    # triage fields. ``attribution_status`` says why ``attribution`` is NULL:
    # ``no_breakdown_columns`` when the scan has no breakdown column to split by
    # (the app-version column does not count), ``not_computed`` when it has one
    # but no split is stored (a catalog-metric signal, a bucket scored before
    # attributions existed, a scope with no breakdown series).
    attribution: SignalAttribution | None = None
    attribution_status: AttributionStatus = "not_computed"
    # Owners of the signal's event type / catalog metric (F07, #260), for the
    # Signal card's owners line and its "Notify owners" action. Filled after
    # the signals cache on the expanded list and on the drilldown's
    # ``latest_signal``; empty elsewhere and for an unowned scope.
    owners: list[AlertOwnerRef] = Field(default_factory=list)


class SeasonalityCell(BaseModel):
    """One cell of the 7×24 hour-of-day × weekday seasonality heatmap.

    `weekday` is ISO-style with Monday=0..Sunday=6 to match Python's
    `datetime.weekday()`. `count` is the total volume observed in that
    slot across the queried time range; `anomaly_count` is the number of
    detected anomalies whose bucket fell into the same slot.
    """

    weekday: int
    hour: int
    count: int
    anomaly_count: int


class SeasonalityHeatmapResponse(BaseModel):
    scan_config_id: uuid.UUID
    scope_type: MetricScopeType
    scope_ref: str
    cells: list[SeasonalityCell]
    max_count: int
    total_count: int
    #: The scan interval the cells were binned from, and whether that interval
    #: actually resolves an hour (an interval of one hour or finer). A daily or
    #: weekly scan puts EVERY bucket in hour 0, and a 6h scan fills only 4 of
    #: 24 columns, so most cells are structurally empty — a 7x24 grid
    #: then reads as missing data instead of as a coarser interval
    #:. Clients render the weekday strip alone when this is
    #: false rather than drawing a grid that can never fill.
    interval: str
    hourly_resolution: bool


class BreakdownTimelinePoint(BaseModel):
    bucket: datetime
    count: int


class BreakdownTimelineResponse(BaseModel):
    scan_config_id: uuid.UUID
    scope_type: MetricScopeType
    scope_ref: str
    breakdown_column: str
    breakdown_value: str
    is_other: bool
    interval: ScanInterval | None
    data: list[BreakdownTimelinePoint]


class ForecastPoint(BaseModel):
    """One-step-ahead expected value emitted alongside the historical series.

    `bucket` is the timestamp of the bucket *being forecast* (i.e. one
    interval past the last actual point). `expected_count` and `stddev` come
    from the same STL/MSTL decomposition used for anomaly detection.
    """

    bucket: datetime
    expected_count: float
    stddev: float


class EventMetricsResponse(BaseModel):
    scope: str
    scan_config_id: uuid.UUID | None = None
    # Display name of the scan config the series is scoped to. The
    # project-total and events-total series chart ONE scan config (summing
    # every config double-counts events a legacy/backfill scan also collected),
    # so the UI must be able to name the scan instead of calling a 2.4 %-of-
    # project series "project total".
    scan_config_name: str | None = None
    event_id: uuid.UUID | None = None
    event_type_id: uuid.UUID | None = None
    interval: ScanInterval | None = None
    latest_signal: MetricSignalResponse | None = None
    # The anomaly sigma threshold — the ``k`` the UI multiplies the per-point
    # (effective) stddev by to draw the confidence band, so "outside the band"
    # equals "flagged". Read from ``ProjectAnomalySettings`` and narrowed by this
    # scope's false-positive override, which is what the detector scored with;
    # NOT from ``ScanConfig.sigma_threshold``, which no API has ever written and
    # which has no reader left (``metrics_service._get_project_sigma_threshold``).
    # The default below is the system default, and it is served as-is by the one
    # route that does not resolve a sigma — ``get_events_metrics``, whose points
    # carry no ``expected_count``/``stddev``, so no band is drawn from it
    # .
    sigma_threshold: float = DEFAULT_SIGMA_THRESHOLD
    # When the scan's newest completed metrics collection finished, and the
    # earliest moment the scheduler will dispatch the next one — the bucket half
    # of its due check (``schedule.scan_config_collection_schedule``), so a live
    # job, the failure backoff or a demo's cooldown can still defer it. Equal to
    # the response time when collection is due now. Both NULL on a path with no
    # scan config or no interval; ``last_collected_at`` is also NULL before the
    # first scheduled or manual collection completes (L4).
    last_collected_at: datetime | None = None
    next_collection_at: datetime | None = None
    # The Events tab's series only (``scope == "events_total"``): its volume over
    # the 7 days ending at the requested upper bound (or now) and the 7 before,
    # independent of the chart's range, for "612K in 7d · +4% vs prior week"
    # (EV-21). NULL on every other scope.
    week_total: int | None = None
    prior_week_total: int | None = None
    data: list[EventMetricPoint]
    forecast: list[ForecastPoint] = []


class EventMetricBreakdownSeries(BaseModel):
    breakdown_value: str
    is_other: bool = False
    total_count: int
    data: list[EventMetricPoint]
    parity_anomalies: list[PlatformParityAnomaly] = []


class EventMetricBreakdownsResponse(BaseModel):
    event_id: uuid.UUID
    scan_config_id: uuid.UUID | None = None
    interval: ScanInterval | None = None
    columns: list[str]
    selected_column: str | None = None
    series: list[EventMetricBreakdownSeries]


class AppVersionInfo(BaseModel):
    version: str
    is_other: bool = False
    is_latest: bool = False
    # True once the release takes a real share of traffic (activation gate),
    # distinguishing an active release from a merely "newest seen" dev build.
    is_active: bool = False


class AppVersionMetricSeries(BaseModel):
    version: str
    is_other: bool = False
    is_latest: bool = False
    is_active: bool = False
    total_count: int
    data: list[EventMetricPoint]


class AppVersionSeriesResponse(BaseModel):
    scan_config_id: uuid.UUID
    scope_type: MetricScopeType
    scope_ref: str
    event_id: uuid.UUID | None = None
    event_type_id: uuid.UUID | None = None
    app_version_column: str | None = None
    interval: ScanInterval | None = None
    latest_version: str | None = None
    # See EventMetricsResponse.sigma_threshold — the confidence-band multiplier.
    sigma_threshold: float = DEFAULT_SIGMA_THRESHOLD
    versions: list[AppVersionInfo]
    series: list[AppVersionMetricSeries]


class AppVersionAdoptionResponse(AppVersionSeriesResponse):
    totals: list[BreakdownTimelinePoint]


class ReleaseRegressionItem(BaseModel):
    """One event (or event type) that regressed in the latest active release."""

    scope_type: MetricScopeType
    scope_ref: str
    scope_name: str
    event_id: uuid.UUID | None = None
    event_type_id: uuid.UUID | None = None
    kind: ReleaseRegressionKind
    version: str
    previous_version: str
    observed_count: int
    expected_count: float
    ratio: float
    share_prev: float
    share_new: float
    release_share: float
    window_from: datetime
    window_to: datetime


class ReleaseComparabilityItem(BaseModel):
    """Whether one detection pass could judge the latest release at all.

    Served alongside ``items`` because an empty list means two different things.
    A caller that only reads ``items`` cannot tell a healthy release from one
    whose findings were withheld, and both were served as an empty list until
    this was carried through.
    """

    scope_type: MetricScopeType
    comparable: bool
    reason: ReleaseComparabilityReason
    version: str | None = None
    previous_version: str | None = None
    emerging_share: float
    max_emerging_share: float


class ReleaseRegressionsResponse(BaseModel):
    scan_config_id: uuid.UUID
    app_version_column: str | None = None
    latest_version: str | None = None
    # One entry per scope the scan evaluated (filtered by the ``scope_type``
    # query parameter when one is given). Empty means no pass has run.
    comparability: list[ReleaseComparabilityItem]
    items: list[ReleaseRegressionItem]


class PlatformPresenceRow(BaseModel):
    """One event and the platform values it has stored breakdown data for."""

    event_id: uuid.UUID
    event_name: str
    present_platforms: list[str]


class PlatformPresenceResponse(BaseModel):
    """Per-event platform presence matrix derived from EventMetricBreakdown rows
    on the scan's designated ``platform_column``. Empty when the column is unset."""

    scan_config_id: uuid.UUID
    platform_column: str | None = None
    platforms: list[str]
    items: list[PlatformPresenceRow]


class TopMoverItem(BaseModel):
    """One row of "what moved this anomaly" — backed by MetricBreakdownAnomaly."""

    breakdown_column: str
    breakdown_value: str
    is_other: bool
    actual_count: float
    expected_count: float
    stddev: float
    z_score: float
    direction: AnomalyDirection


class DistributionDriftTopMover(BaseModel):
    value: str
    baseline_share: float
    current_share: float
    contribution: float


class DistributionDriftPoint(BaseModel):
    id: uuid.UUID
    scan_config_id: uuid.UUID
    event_type_id: uuid.UUID | None = None
    field_name: str
    bucket: datetime
    psi: float
    band: DistributionDriftBand
    baseline_total: int
    current_total: int
    top_movers: list[DistributionDriftTopMover]


class DistributionDriftsResponse(BaseModel):
    scope: str
    scan_config_id: uuid.UUID | None = None
    event_type_id: uuid.UUID | None = None
    fields: list[str]
    data: list[DistributionDriftPoint]


class EventWindowMetricsRequest(BaseModel):
    event_ids: list[uuid.UUID]
    time_from: datetime | None = None
    time_to: datetime | None = None


class ActiveSignalsQuery(BaseModel):
    event_ids: list[uuid.UUID] = []


class SignalTriageScope(BaseModel):
    """The scope a triage verdict is about, keyed like the signal itself.

    ``scan_config_id`` is NULL for a catalog ``metric`` scope and required for
    every other one. ``bucket`` names the signal the user acted on: the
    per-signal verdicts pin it, and a mute uses it to refuse a signal that was
    routed to an incident (that one is triaged in the inbox).
    """

    scan_config_id: uuid.UUID | None = None
    scope_type: MetricScopeType
    scope_ref: str = Field(min_length=1, max_length=64)
    bucket: datetime


SignalMuteDuration = Literal["24h", "7d", "until_unmuted"]


class SignalMuteRequest(SignalTriageScope):
    duration: SignalMuteDuration


class SignalExpectedRequest(SignalTriageScope):
    # Also the chart annotation's description, so the marker explains itself.
    note: str | None = Field(default=None, max_length=2000)


class SignalTriageState(BaseModel):
    """The triage fields of one signal after a write, as the list would show them."""

    acknowledged_at: datetime | None = None
    muted: bool = False
    muted_until: datetime | None = None
    expected: bool = False
    expected_note: str | None = None
    hidden: bool = False


class SignalVerdictRequest(SignalTriageScope):
    """``POST /projects/{slug}/signals/verdict`` (F01, #254).

    ``expected_reason`` belongs to ``expected`` only. ``note`` is free text on
    every verdict; on ``expected`` it is also the chart annotation's text.
    """

    verdict: SignalVerdict
    expected_reason: SignalExpectedReason | None = None
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason_only_on_expected(self) -> SignalVerdictRequest:
        if self.expected_reason is not None and self.verdict != SignalVerdict.expected:
            raise ValueError("expected_reason is only valid with the 'expected' verdict")
        return self


class SignalVerdictResponse(SignalTriageState):
    """The signal's triage fields, verdict and incident after a verdict write."""

    verdict: SignalVerdictInfo | None = None
    incident: SignalIncidentBrief | None = None


class SignalVerdictCountsResponse(BaseModel):
    """Verdicts over the project's open signals (the expanded Anomalies list).

    ``needs_verdict`` counts signals with no verdict that no mute or
    ``expected`` hides; the other four count signals by verdict, whether the
    signal's own row or its incident's status supplied it (F15 health score).
    """

    needs_verdict: int = 0
    expected: int = 0
    tracking_bug: int = 0
    false_positive: int = 0
    real_issue: int = 0


# Longest batch ``POST /anomalies/signals/series`` accepts. The Anomalies page
# asks for the rows it renders; a flooded project has ~200 open signals.
SIGNAL_SERIES_MAX_SCOPES = 500


class SignalSeriesScope(BaseModel):
    """One open signal whose recent series a row sparkline draws (MO-19)."""

    scan_config_id: uuid.UUID
    scope_type: MetricScopeType
    scope_ref: str
    bucket: datetime


class SignalSeriesQuery(BaseModel):
    scopes: list[SignalSeriesScope] = Field(max_length=SIGNAL_SERIES_MAX_SCOPES)


class SignalSeriesResponse(BaseModel):
    """Up to ``SIGNAL_SERIES_BUCKETS`` buckets around one signal's flagged bucket.

    The window opens 20 buckets before ``bucket`` and closes 4 after it, so the
    row shows the run-up and whether the move held. Gaps inside the stored range
    are zero-filled (an absent row is a zero count); buckets past the newest
    stored one are simply absent.
    """

    scan_config_id: uuid.UUID
    scope_type: MetricScopeType
    scope_ref: str
    bucket: datetime
    interval: ScanInterval | None = None
    data: list[BreakdownTimelinePoint]


class EventWindowMetricsResponse(BaseModel):
    event_id: uuid.UUID
    scan_config_id: uuid.UUID | None = None
    interval: ScanInterval | None = None
    total_count: int
    data: list[EventMetricPoint]


class TopEventResponse(BaseModel):
    """One row of the Overview "Top events by volume" widget."""

    event_id: uuid.UUID
    name: str
    event_type_id: uuid.UUID
    total_count: int
    # The PROJECT's volume over the same window — identical on every row — so a
    # row can show its event's share without a second request (MO-25). Counted
    # the project-total way (type-level rows only; see
    # ``metrics_service.get_top_events_by_volume``), so unmatched traffic is in
    # the denominator and the shares need not add up to 100%.
    window_total_count: int = 0


class OverviewKpiSeriesResponse(BaseModel):
    """Real daily series behind Overview KPI sparklines.

    Only ``new_events`` (events created per day on the main branch, from
    Event.created_at) has genuine history; other KPIs (active events, open
    signals, review-pending) have no time series until snapshotting is added,
    so they are intentionally omitted rather than fabricated. The field was
    named ``active_events`` until it was renamed — it never held active-event
    counts, and the Overview sparkline repeated that false claim in its label.
    """

    days: int
    new_events: list[int]
