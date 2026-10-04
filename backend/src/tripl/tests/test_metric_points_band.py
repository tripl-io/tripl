"""Unit tests for the metrics-serving anomaly band.

Covers the read-time serving pieces for the anomaly band (Lane B4):

* ``_build_metric_points`` serves the STORED *effective* (floored) stddev and
  ``detector_kind`` on anomaly buckets — so the chart band
  ``expected ± sigma_threshold * effective_stddev`` matches the detector's flag
  decision — and falls back to the raw stddev when the effective column is
  absent (pre-migration rows / hand-built objects);
These exercise the pure helpers directly (no DB), mirroring the style of
``test_version_activation.py``.
"""

from datetime import datetime
from types import SimpleNamespace

from tripl.services.metrics_service import (
    _build_metric_points,
    _served_detector_kind,
    _served_stddev,
)

DAYS = [datetime(2026, 1, d) for d in range(1, 6)]  # 5 daily buckets
BUCKET = datetime(2026, 1, 2)


def _anomaly(**overrides: object) -> SimpleNamespace:
    base = {
        "bucket": BUCKET,
        "actual_count": 0.0,
        "expected_count": 10.0,
        "stddev": 2.0,
        "z_score": -5.0,
        "direction": "drop",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_served_stddev_prefers_effective_over_raw() -> None:
    anomaly = _anomaly(stddev=2.0, effective_stddev=0.5)
    assert _served_stddev(anomaly) == 0.5


def test_served_stddev_falls_back_to_raw_when_effective_absent() -> None:
    # No effective_stddev attribute at all (pre-migration / test object).
    anomaly = _anomaly(stddev=2.0)
    assert _served_stddev(anomaly) == 2.0


def test_served_stddev_falls_back_to_raw_when_effective_zero() -> None:
    # A stored 0.0 effective stddev is treated as "unset" so the band never
    # collapses to zero width.
    anomaly = _anomaly(stddev=3.0, effective_stddev=0.0)
    assert _served_stddev(anomaly) == 3.0


def test_served_detector_kind_reads_column_or_none() -> None:
    assert _served_detector_kind(_anomaly(detector_kind="trend")) == "trend"
    assert _served_detector_kind(_anomaly()) is None


def test_build_metric_points_serves_effective_stddev_and_kind() -> None:
    anomaly = _anomaly(effective_stddev=0.5, detector_kind="rolling")
    points = _build_metric_points(
        interval=None,
        metric_rows=[(BUCKET, 0)],
        anomalies=[anomaly],
    )
    point = next(p for p in points if p.bucket == BUCKET)
    assert point.is_anomaly is True
    # The served stddev is the effective (floored) one, NOT the raw 2.0.
    assert point.stddev == 0.5
    assert point.detector_kind == "rolling"
    assert point.expected_count == 10.0


def test_build_metric_points_band_matches_detector_flag() -> None:
    # With effective stddev 2 and a 3-sigma threshold the band is [4, 16];
    # the flagged actual (0) sits outside it, matching |z| >= 3.
    anomaly = _anomaly(actual_count=0.0, expected_count=10.0, effective_stddev=2.0)
    points = _build_metric_points(
        interval=None,
        metric_rows=[(BUCKET, 0)],
        anomalies=[anomaly],
    )
    point = next(p for p in points if p.bucket == BUCKET)
    sigma_threshold = 3.0
    lower = point.expected_count - sigma_threshold * point.stddev
    assert point.count < lower  # actual outside the band == flagged


def test_build_metric_points_normal_buckets_have_no_band_fields() -> None:
    points = _build_metric_points(
        interval=None,
        metric_rows=[(DAYS[0], 5), (BUCKET, 0)],
        anomalies=[_anomaly(effective_stddev=2.0)],
    )
    normal = next(p for p in points if p.bucket == DAYS[0])
    assert normal.is_anomaly is False
    assert normal.stddev is None
    assert normal.detector_kind is None
