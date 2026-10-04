"""Which buckets a scan config's collection actually covered.

Lifted out of ``tasks.py`` when a second caller appeared: the catalog-metric
detection pass resolves coverage PER METRIC, from the scan config whose grid the
metric's values were collected on — which is not necessarily the config the
running scan belongs to (see
``detect._recalculate_project_metric_anomalies``).

Pure leaf (models + the timestamp helper) so both the orchestrator and the
detector import it without a cycle.

Bucket convention
-----------------
**Every bucket this module returns is TZ-AWARE UTC**, and so is every bound it
compares one against. That is the same convention
``worker.analyzers.metric_composition.normalize_series`` already imposes where
two independently sourced series meet, and the one
``core.bucketing`` states for the whole pipeline: a naive datetime IS UTC, it
just has not said so.

It has to be stated, because this set is built from halves that disagree by
nature:

* the job-window half parses ``result_summary`` through ``_parse_task_datetime``
  and is aware UTC no matter what;
* the presence half is a raw ``event_metrics.bucket`` read, which yields AWARE
  values on PostgreSQL (``timestamptz``) and NAIVE ones on SQLite;
* the caller's ``current_window``/``history_from`` are whatever the orchestrator
  minted, which the same split reaches through ``max(EventMetric.bucket)``.

Scope of the defect this convention closes, stated exactly: the set was only ever
mixed on SQLITE. On PostgreSQL every half was already aware — ``timestamptz``
columns, ``_parse_task_datetime``, and an orchestrator window minted from an
aware clock — and Python compares and hashes aware datetimes BY INSTANT, so even
a set holding two different offsets keys alike. Production was NOT under-reporting
coverage. On SQLite the series buckets the detector loads come back naive too, so
only the stored-bucket half of the union could ever match them.

The warning that survives is about the consumer and applies to any producer on
any backend: ``anomaly_detector.expand_series`` does a plain
``bucket not in covered`` membership test, and ``datetime(t)`` and
``datetime(t, tzinfo=UTC)`` are neither equal nor equal-hashing. A producer that
skips the boundary silently contributes dead entries — nothing raises, coverage
under-reports, and an uncovered bucket is EXCLUDED from the series rather than
zero-filled, so genuinely-zero buckets vanish from every baseline with no error
anywhere.

**A new producer conforms by calling ``to_utc`` at the point its value ENTERS
this module** — never at the membership test, which is a set lookup and cannot
convert. The matching obligation on the consumer side lives in
``detect._load_scope_points`` / ``_load_metric_value_points`` /
``_load_breakdown_scope_points``, which stamp the series buckets the same way.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.bucketing import to_utc
from tripl.models.event_metric import EventMetric
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.worker.tasks.metrics._helpers import _parse_task_datetime


def covered_buckets_from_scan_jobs(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    delta: timedelta,
    history_from: datetime,
    current_window: tuple[datetime, datetime] | None = None,
    presence_before: datetime | None = None,
) -> set[datetime]:
    """Buckets a successful collection actually covered, on the interval grid.

    A collection gap (an interval that a failed/never-run job left uncollected)
    is otherwise zero-filled by the detector and masquerades as a 'drop'
    anomaly. We union the ``[time_from, time_to)`` windows of every COMPLETED
    scan job (recorded in ``result_summary``) with the window this run just
    wrote, and hand the enumerated set to ``detect_anomalies`` as
    ``covered_buckets`` so a MISSING bucket is only zero-filled when it fell
    inside real coverage; missing-and-uncovered buckets are excluded instead of
    read as zeros.

    Every bucket that already carries stored data is unioned in as well: data
    only exists because a collection produced it, so a present observation is by
    definition covered. This keeps the baseline intact even for buckets whose
    originating job window is no longer recorded, and never marks a genuine gap
    (which has no row) as covered.

    ``history_from`` bounds the whole computation to the oldest bucket the
    caller's detector pass can consult, and is REQUIRED: both reads are over a
    config's entire lifetime otherwise (an hourly config a year old carries
    ~8,800 completed jobs, and the ``event_metrics`` DISTINCT is one row per
    event per bucket on top of that), while ``expand_series`` starts at the
    oldest point the pass loaded and never tests a bucket below its own
    ``history_from``. The bound must stay TIME-based: a recent-N row limit would
    leave every older bucket uncovered, and an uncovered bucket is EXCLUDED from
    the series rather than zero-filled, so genuinely-zero buckets would silently
    drop out of every baseline.

    ``current_window`` is the window the caller's run just wrote, unioned in
    because a bucket the scan observed as zero has no stored row and no
    completed job summary yet. It is absent when the caller is asking about a
    FOREIGN config, which this run wrote nothing for; ``presence_before`` then
    supplies the upper bound for the stored-bucket read.

    Every returned bucket is tz-aware UTC regardless of what the caller passed in
    or what the database column yielded — see the module docstring for why that
    is a contract and not an implementation detail.
    """
    # The three values that ENTER from the caller, stamped once here so nothing
    # below this line has to ask what backend or clock they came from.
    history_from = to_utc(history_from)
    if presence_before is not None:
        present_before = to_utc(presence_before)
    elif current_window is not None:
        present_before = to_utc(current_window[1])
    else:
        msg = "covered_buckets_from_scan_jobs needs current_window or presence_before"
        raise ValueError(msg)

    windows: list[tuple[datetime, datetime]] = []
    if current_window is not None:
        windows.append((to_utc(current_window[0]), to_utc(current_window[1])))
    summaries = session.execute(
        select(ScanJob.result_summary).where(
            ScanJob.scan_config_id == scan_config_id,
            ScanJob.status == ScanJobStatus.completed.value,
            ScanJob.created_at >= history_from,
        )
    ).scalars()
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        raw_from = summary.get("time_from")
        raw_to = summary.get("time_to")
        if not isinstance(raw_from, str) or not isinstance(raw_to, str):
            continue
        try:
            window_from = _parse_task_datetime(raw_from)
            window_to = _parse_task_datetime(raw_to)
        # result_summary is free-form JSON, so a recorded bound can be malformed
        # (ValueError) or not a string at all (TypeError).
        except ValueError, TypeError:
            continue
        if window_from < window_to:
            windows.append((window_from, window_to))

    covered: set[datetime] = set()
    for window_from, window_to in windows:
        bucket = window_from
        # The ``created_at`` filter above does not bound this loop on its own: a
        # replay job has a recent created_at and a multi-year recorded window.
        # Advance in whole ``delta`` steps so every emitted bucket stays on the
        # grid the unbounded loop produced; the horizon itself need not sit on
        # that grid (the slack is a flat day, a weekly grid is not), so round the
        # step count UP rather than letting floor division land below it.
        if bucket < history_from:
            steps = (history_from - bucket) // delta
            if bucket + delta * steps < history_from:
                steps += 1
            bucket += delta * steps
        while bucket < window_to:
            covered.add(bucket)
            bucket += delta

    present_buckets = session.execute(
        select(EventMetric.bucket)
        .where(
            EventMetric.scan_config_id == scan_config_id,
            EventMetric.bucket >= history_from,
            EventMetric.bucket < present_before,
        )
        .distinct()
    ).scalars()
    # The one read whose awareness is decided by the BACKEND rather than by this
    # process: timestamptz on PostgreSQL, naive on SQLite. Stamped on the way in
    # so the union is homogeneous on both.
    covered.update(to_utc(bucket) for bucket in present_buckets if bucket is not None)
    return covered
