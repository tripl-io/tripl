"""The "Test rule" replay and the live send format a distribution drift alike.

Both used to carry their own byte-identical copy of the drift sample formatter,
of its ``_mover_float`` and of the 500-character alert-text trimmer, and nothing
tied the copies together: an edit to one would have made the replay preview a
sample text the real Slack or email alert never carries. Now there is one of
each, and these pin that both paths call it and what it writes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from tripl.models.distribution_drift import DistributionDrift, mover_float
from tripl.services import alerting_rendering, alerting_service, metrics_insights_service
from tripl.worker.tasks.metrics import lifecycle_alerts, signals, urls


def _drift(top_movers: list[dict[str, object]] | None) -> DistributionDrift:
    return DistributionDrift(
        id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        field_name="platform",
        bucket=datetime(2026, 9, 1, tzinfo=UTC),
        psi=0.42,
        band="significant",
        baseline_total=100,
        current_total=120,
        top_movers=top_movers,
    )


def test_replay_and_send_call_the_same_formatter_and_trimmer() -> None:
    formatter = alerting_rendering.format_distribution_drift_sample
    trimmer = alerting_rendering.trim_alert_text
    assert signals.format_distribution_drift_sample is formatter
    assert alerting_service.format_distribution_drift_sample is formatter
    assert signals.trim_alert_text is trimmer
    assert lifecycle_alerts.trim_alert_text is trimmer
    assert alerting_service.trim_alert_text is trimmer
    assert metrics_insights_service.mover_float is mover_float
    # The retired copies stay retired.
    assert not hasattr(signals, "_format_distribution_drift_sample")
    assert not hasattr(signals, "_mover_float")
    assert not hasattr(urls, "_trim_alert_text")
    assert not hasattr(alerting_rendering, "_mover_float")
    assert not hasattr(metrics_insights_service, "_mover_float")


def test_the_sample_is_the_psi_and_the_first_three_movers() -> None:
    drift = _drift(
        [
            {"value": "ios", "baseline_share": 0.1, "current_share": 0.3},
            {"value": "android", "baseline_share": "0.5", "current_share": 0.25},
            {"value": "web", "baseline_share": None, "current_share": 0.2},
            {"value": "tv", "baseline_share": 0.01, "current_share": 0.02},
        ]
    )

    assert alerting_rendering.format_distribution_drift_sample(drift) == (
        "psi=0.420; ios 10.0%->30.0%, android 50.0%->25.0%, web 0.0%->20.0%"
    )


def test_a_drift_without_movers_is_its_psi_alone() -> None:
    assert alerting_rendering.format_distribution_drift_sample(_drift([])) == "psi=0.420"
    assert alerting_rendering.format_distribution_drift_sample(_drift(None)) == "psi=0.420"


def test_a_long_sample_is_cut_to_the_alert_limit() -> None:
    drift = _drift([{"value": "x" * 600, "baseline_share": 0.1, "current_share": 0.2}])

    sample = alerting_rendering.format_distribution_drift_sample(drift)

    assert len(sample) == 500
    assert sample.endswith("...")


def test_mover_float_reads_numbers_and_numeric_text_and_zeroes_the_rest() -> None:
    assert mover_float(0.25) == 0.25
    assert mover_float(3) == 3.0
    assert mover_float("0.5") == 0.5
    assert mover_float(None) == 0.0
    assert mover_float({"share": 1}) == 0.0
