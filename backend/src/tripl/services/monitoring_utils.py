from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

# Default freshness window for an open signal. Projects can override it via
# ProjectAnomalySettings.recent_signal_window_hours, which reaches this module as
# the ``recent_window`` argument below; callers that pass nothing keep this value.
RECENT_SIGNAL_WINDOW = timedelta(hours=24)

# An open signal is only fresh while its anomaly bucket sits within this many
# scan intervals of ``now``. Two things depend on it, and they pull in opposite
# directions:
#
#   * a CAP, so a scan that stops collecting cannot leave its final anomaly —
#     still topping ``max(bucket)`` — classified as open forever, red while the
#     charts are empty;
#   * a FLOOR, so a grid coarser than the wall-clock window is not measured
#     against a window shorter than one of its own buckets.
#
# Both are ``max(recent_window, N * interval)``, so sub-daily scans keep exactly
# the configured window and only long grids move. ``worker.tasks.metrics.signals``
# does NOT import this name: it reaches the constant only through
# ``classify_signal_state``, so there is no second copy to drift. It used to keep
# a mirror, and the mirror drifted twice;
# ``test_monitors_summary`` pins that the name is absent from ``signals`` so a
# re-introduced copy fails loudly.
LATEST_SCAN_STALE_INTERVALS = 3

# ScanInterval enum string (e.g. "1d") -> wall-clock duration. Keyed by string so
# this module stays a pure leaf (no model/enum import); callers pass config.interval.
_SCAN_INTERVAL_DELTAS: dict[str, timedelta] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
}


def _utc_bucket(bucket: datetime) -> datetime:
    """SQLite drops timezone metadata from UTC buckets; restore it for comparisons."""
    if bucket.tzinfo is None:
        return bucket.replace(tzinfo=UTC)
    return bucket.astimezone(UTC)


def scan_interval_to_timedelta(interval: str | None) -> timedelta | None:
    """Map a ScanInterval value (e.g. ``"1d"``) to a ``timedelta``.

    Returns ``None`` for an unknown/absent interval so ``classify_signal_state``
    falls back to the effective recent window as its freshness horizon.
    """
    if interval is None:
        return None
    return _SCAN_INTERVAL_DELTAS.get(str(interval))


def recent_signal_window_from_hours(hours: int | None) -> timedelta | None:
    """Map a project's configured freshness window (in hours) to a ``timedelta``.

    Returns ``None`` for an absent/unset value so callers can hand the result
    straight to ``classify_signal_state``'s ``recent_window`` and land on the
    default ``RECENT_SIGNAL_WINDOW``. Takes an ``int`` rather than an ORM row so
    this module stays a pure leaf.
    """
    if hours is None:
        return None
    return timedelta(hours=int(hours))


def _freshness_horizon(
    interval: timedelta | None,
    recent_window: timedelta = RECENT_SIGNAL_WINDOW,
) -> timedelta:
    """How long an anomaly bucket keeps a signal open, floored at the series' own grid.

    A wall-clock window shorter than one bucket cannot describe freshness: on a
    daily grid the newest anomaly the detector may emit is already a full bucket
    behind the metric head — ingestion settling withholds the newest bucket from
    emission — so a 24-hour window excludes it and the signal reads as closed
    however large it was. Weekly is worse: 24 hours is a seventh of one bucket.
    Flooring at ``LATEST_SCAN_STALE_INTERVALS`` buckets is inert on every grid up
    to 6h (``max(24h, 18h)`` is still 24h) and only bites where the window was
    narrower than the data it measures.

    It does override a project's own ``recent_signal_window_hours`` on a long
    grid — 6 hours on a daily scan becomes 72 — and that is deliberate: the
    setting exists to age out burned-out spikes sooner, not to hide every signal
    a scan can produce.
    """
    if interval is None:
        return recent_window
    return max(recent_window, LATEST_SCAN_STALE_INTERVALS * interval)


def scan_liveness_cutoff(
    *,
    interval: str | None,
    recent_window: timedelta | None,
    now: datetime | None = None,
) -> datetime:
    """Oldest bucket that can still prove a collector is alive.

    Callers that must ASK the database for a scan's newest bucket (rather than
    deriving it from rows they already hold) use this to bound the query: a
    bucket older than this cutoff would fail ``_bucket_is_recent`` inside
    ``_outage_is_still_running`` anyway, so filtering it out in SQL discards
    nothing and keeps the read off an unbounded ``event_metrics`` scan. Derived
    from the same horizon rule as the classification itself, because a probe
    measured against a different window than the decision it feeds is exactly
    how the two signal paths drifted before.
    """
    reference = _utc_bucket(now) if now is not None else datetime.now(UTC)
    window = recent_window if recent_window is not None else RECENT_SIGNAL_WINDOW
    return reference - _freshness_horizon(scan_interval_to_timedelta(interval), window)


def latest_bucket_by_scan(
    rows: Iterable[tuple[uuid.UUID | None, datetime | None]],
) -> dict[uuid.UUID, datetime]:
    """Newest bucket collected per scan config — the "is this collector alive" probe.

    Fed to ``classify_signal_state``'s ``scan_latest_bucket``. Shared because the
    Anomalies page and the sidebar badge read it out of differently-shaped
    metric-bucket maps, and those surfaces only agree while both derive scan
    liveness by literally the same rule. Taking the max across every scope of a
    scan (not just its project total) keeps the probe honest on a scan whose
    per-event rows run ahead of its per-type rollup.
    """
    latest: dict[uuid.UUID, datetime] = {}
    for scan_config_id, bucket in rows:
        if scan_config_id is None or bucket is None:
            continue
        current = latest.get(scan_config_id)
        if current is None or _utc_bucket(bucket) > _utc_bucket(current):
            latest[scan_config_id] = bucket
    return latest


def _outage_is_still_running(
    *,
    anomaly_actual_count: float | None,
    anomaly_expected_count: float | None = None,
    scan_latest_bucket: datetime | None,
    cutoff: datetime,
) -> bool:
    """Whether a persisted outage anchor still describes the CURRENT series.

    Only meaningful from inside ``classify_signal_state``'s
    ``anomaly_bucket >= latest_metric_bucket`` branch, which already establishes
    the second half of the question: the scope has stored nothing newer than the
    anomaly, so it has emitted nothing since. This adds the facts that turn that
    into "still down":

      * the ANCHOR ROW says the scope was at zero when it was announced
        (``actual_count == 0``). ``_collapse_outage_runs`` gives an outage
        exactly one such row and never re-emits it, so this row is the whole
        announcement — there will not be a fresher one to age against;
      * the SCAN is still collecting something (``scan_latest_bucket`` inside
        the same freshness horizon), so the silence belongs to this scope rather
        than to a collector that stopped. Without it the very cap the
        latest-scan branch exists for would be gone, and a switched-off scan
        would pin its final anomaly red forever;
      * and the anchor row says there was something to LOSE
        (``expected_count > 0``). A scope that was expected to emit nothing and
        emitted nothing is not an incident, and it is the one shape this
        predicate alone can end: an empty scope stores no metric row, so its own
        head stays frozen AT the anchor and the latest-scan branch above is true
        for it forever. Production carried 14 such rows — "spike, 0 actual vs 0
        expected", written by ``_detect_trend_shift`` on a project running
        ``min_expected_count = 0`` — open since the day they were detected,
        invisible below the magnitude gate but permanently inflating the
        denominator the Anomalies page states out loud.

    The three inputs are deliberately NOT symmetrical in how ``None`` is read.
    The first two are evidence FOR an outage, so an unanswered one cannot prove
    it and returns False. ``anomaly_expected_count`` is a DISQUALIFIER, so an
    unanswered one cannot disqualify and is ignored: a caller that supplies the
    pair but not the expectation keeps exactly today's answer instead of quietly
    closing an incident that is still running — the regression tripl-l429.15/.26
    fixed, and by far the more expensive of the two ways to be wrong here. Every
    caller that can answer it should, and all of them do.

    Callers that cannot answer the pair — catalog ``metric`` scopes have no scan
    to ask, and a fractional series' 0.0 is a value rather than "emitted nothing"
    — pass neither and land exactly where they did before. COUNT-shaped,
    scan-backed scopes only, matching ``_collapse_outage_runs``.
    """
    if anomaly_actual_count is None or anomaly_actual_count > 0:
        return False
    if anomaly_expected_count is not None and anomaly_expected_count <= 0:
        return False
    if scan_latest_bucket is None:
        return False
    return _bucket_is_recent(scan_latest_bucket, cutoff)


def classify_signal_state(
    *,
    anomaly_bucket: datetime,
    latest_metric_bucket: datetime | None,
    now: datetime | None = None,
    interval: timedelta | None = None,
    recent_window: timedelta | None = None,
    anomaly_actual_count: float | None = None,
    anomaly_expected_count: float | None = None,
    scan_latest_bucket: datetime | None = None,
    emission_lag: timedelta = timedelta(0),
) -> str | None:
    """Classify one anomaly as ``"latest_scan"``, ``"recent"`` or closed (``None``).

    The single classifier for BOTH signal paths — the request path that renders
    the Anomalies page, the badge, the catalog and the drilldown, and the
    worker's alert-candidacy pass in ``worker.tasks.metrics.signals``. It used to
    be two hand-maintained copies, on the stated grounds that the worker must not
    import the services layer; this module imports no ``tripl`` module at all, so
    that never applied, and the copies drifted twice inside one PR,
    each time showing a signal open on the page
    while the alerting path treated it as closed.

    The optional inputs below are the ones only some callers can answer, and each
    defaults to the value that reproduces the behaviour of a caller that cannot:

    * ``emission_lag`` — how far behind the metric head the newest EMITTABLE
      anomaly can sit. Detection withholds the newest ``settling_buckets`` of a
      series from emission, so a scope that is still emitting can never carry an
      anomaly at or after its own head; the alert path measures the head that far
      back so a live scope can reach the latest-scan branch at all. It is exactly
      a shift of the head, i.e. ``emission_lag=L`` answers what
      ``latest_metric_bucket - L`` would. The display path passes nothing, keeping
      the raw head and the classification the API has always rendered;
    * ``anomaly_actual_count`` / ``scan_latest_bucket`` — the outage re-check, see
      ``_outage_is_still_running``. Callers that cannot answer both (a catalog
      metric has no scan to probe, and a fractional series' ``0.0`` is a value
      rather than silence) pass neither and land where they did before;
    * ``anomaly_expected_count`` — the third input to that same re-check, and the
      only one whose ``None`` means "ignore" rather than "cannot conclude". Every
      caller that passes the pair above should pass this too; the polarity is
      explained in ``_outage_is_still_running``.
    """
    # No stored metric values means there is no live scan to anchor recency on, so
    # there is nothing to keep open — treat the signal as closed.
    if latest_metric_bucket is None:
        return None

    reference = _utc_bucket(now) if now is not None else datetime.now(UTC)
    # Absent per-project override, every branch below behaves exactly as it did
    # when the 24h constant was read directly.
    window = recent_window if recent_window is not None else RECENT_SIGNAL_WINDOW
    horizon = _freshness_horizon(interval, window)

    if _utc_bucket(anomaly_bucket) >= _utc_bucket(latest_metric_bucket) - emission_lag:
        latest_scan_cutoff = reference - horizon
        if _bucket_is_recent(anomaly_bucket, latest_scan_cutoff):
            return "latest_scan"
        # An outage that never recovered has no fresher row to be judged on: its
        # anchor was announced once, at onset, and deliberately never re-emitted.
        # Re-check it against the series instead of its own age — the scope's
        # newest data point is still that zero, so the incident is still running
        # and "latest_scan" is still literally true.
        if _outage_is_still_running(
            anomaly_actual_count=anomaly_actual_count,
            anomaly_expected_count=anomaly_expected_count,
            scan_latest_bucket=scan_latest_bucket,
            cutoff=latest_scan_cutoff,
        ):
            return "latest_scan"
        # A stopped scan's final anomaly still tops max(bucket) but is stale in
        # wall-clock terms; fall through to the recent-window / closed checks.

    # Same horizon as the branch above, and for the same reason. Ingestion
    # settling withholds the newest bucket(s) from EMISSION, so a scope that is
    # still emitting can never carry an anomaly at or after its own metric head
    # — the branch above is unreachable for anything alive, and this one decides
    # every live signal. Measuring it against a bare 24 hours closed every signal
    # on a daily or weekly scan outright, since the newest emittable anomaly
    # there is already a whole bucket old.
    recent_cutoff = reference - horizon
    if _bucket_is_recent(anomaly_bucket, recent_cutoff):
        return "recent"

    return None


def _bucket_is_recent(bucket: datetime, cutoff: datetime) -> bool:
    """Compare SQLite and PostgreSQL buckets as UTC instants."""
    return _utc_bucket(bucket) >= _utc_bucket(cutoff)


class _MonitorState(Protocol):
    is_active: bool
    last_anomaly_bucket: datetime | None
    last_notified_at: datetime | None


# Resolves the grid one alert state's series is scored on (its scan config's
# interval, or a catalog metric's own grid), or None when it has none.
# Typed on ``Any`` because callers hand in their concrete ORM row type, which a
# callable parameterised on the protocol would not accept (contravariance).
MonitorStateInterval = Callable[[Any], timedelta | None]


@dataclass(frozen=True)
class MonitorRollup:
    status: str  # "firing" | "warning" | "healthy"
    active_scope_count: int
    firing_scope_count: int
    last_anomaly_at: datetime | None
    last_notified_at: datetime | None


def firing_monitor_states[StateT: _MonitorState](
    states: Sequence[StateT],
    *,
    now: datetime,
    interval_of: MonitorStateInterval | None = None,
) -> list[StateT]:
    """The states ``summarize_monitor_states`` counts as firing, themselves.

    Split out so the monitor detail can LIST the scopes firing now (MO-36) by
    the very rule that produced its ``firing_scope_count`` — a second copy of
    the horizon test is how the count and the list would come to disagree.
    """
    return [
        state
        for state in states
        if state.is_active
        and state.last_anomaly_bucket is not None
        and _bucket_is_recent(
            state.last_anomaly_bucket,
            now
            - _freshness_horizon(
                interval_of(state) if interval_of is not None else None,
                RECENT_SIGNAL_WINDOW,
            ),
        )
    ]


def summarize_monitor_states(
    states: Sequence[_MonitorState],
    *,
    now: datetime,
    interval_of: MonitorStateInterval | None = None,
) -> MonitorRollup:
    """Roll a rule's per-scope alert states into a single monitor status.

    * firing  — at least one active scope with an anomaly inside the freshness
      horizon alert dispatch judges it by
    * warning — active scopes exist, but none have a recent anomaly (stale/open)
    * healthy — no active scopes

    Deliberately NOT narrowed by the project's configured open-signal window:
    this summarizes ALERT state, and alert dispatch ignores that setting too
    (see ``worker.tasks.metrics.signals._get_latest_active_anomalies``), so a
    monitor must not read "healthy" while its rule is still delivering.

    It DOES follow dispatch's interval floor: dispatch keeps a scope open for
    ``max(24h, 3 x interval)`` of its own grid, so a daily or weekly anomaly it
    is still delivering sits well past a bare 24 hours. Judging that against
    ``now - 24h`` read "warning" — firing_count 0, the sidebar badge off —
    while the alert was being sent. ``interval_of`` resolves
    each state's grid; a caller that passes nothing, or a state it cannot
    resolve, falls back to the bare 24-hour window.

    It does NOT follow dispatch's still-running-outage re-check
    (``_outage_is_still_running``): dispatch keeps a zero-actual outage anchor
    live for as long as its scan keeps collecting, while this rollup sees only
    ``last_anomaly_bucket`` (the onset, which never advances). A monitor on an
    outage older than the horizon therefore reads "warning" while dispatch still
    holds the state active. Closing that gap needs the anchor's counts and the
    scan's latest bucket, which ``AlertRuleState`` does not carry.
    """
    active = [state for state in states if state.is_active]
    firing = firing_monitor_states(states, now=now, interval_of=interval_of)
    if firing:
        status = "firing"
    elif active:
        status = "warning"
    else:
        status = "healthy"

    last_anomaly_at = max(
        (state.last_anomaly_bucket for state in states if state.last_anomaly_bucket is not None),
        default=None,
    )
    last_notified_at = max(
        (state.last_notified_at for state in states if state.last_notified_at is not None),
        default=None,
    )
    return MonitorRollup(
        status=status,
        active_scope_count=len(active),
        firing_scope_count=len(firing),
        last_anomaly_at=last_anomaly_at,
        last_notified_at=last_notified_at,
    )
