"""UTC bucket comparisons across SQLite-naive and PostgreSQL-aware values."""

import uuid
from datetime import UTC, datetime, timedelta, timezone

from tripl.alerting_matching import simulate_rule_firings
from tripl.models.alert_rule import AlertRule
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.services.monitoring_utils import classify_signal_state, latest_bucket_by_scan
from tripl.worker.tasks.metrics.dispatch import _bucket_is_newer, _latest_bucket


def test_dispatch_keeps_latest_instant_and_original_representation() -> None:
    stored = datetime(2026, 9, 23, 14)
    same_instant = datetime(2026, 9, 23, 16, tzinfo=timezone(timedelta(hours=2)))
    newer = datetime(2026, 9, 23, 14, 5, tzinfo=UTC)

    assert not _bucket_is_newer(same_instant, stored)
    assert _latest_bucket(same_instant, stored) is stored
    assert _bucket_is_newer(newer, stored)
    assert _latest_bucket(newer, stored) is newer


def test_signal_classification_accepts_a_mixed_timezone_metric_head() -> None:
    now = datetime(2026, 9, 23, 14, 10, tzinfo=UTC)
    assert (
        classify_signal_state(
            anomaly_bucket=now - timedelta(minutes=10),
            latest_metric_bucket=datetime(2026, 9, 23, 14),
            now=now,
        )
        == "latest_scan"
    )


def test_latest_bucket_by_scan_compares_instant_but_returns_input() -> None:
    scan = uuid.uuid4()
    old = datetime(2026, 9, 23, 14)
    new = datetime(2026, 9, 23, 16, 5, tzinfo=timezone(timedelta(hours=2)))
    assert latest_bucket_by_scan([(scan, old), (scan, new)]) == {scan: new}


def test_replay_orders_mixed_timezones_and_applies_cooldown_by_instant() -> None:
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        name="UTC replay",
        enabled=True,
        include_events=True,
        notify_on_spike=True,
        min_percent_delta=0,
        min_absolute_delta=0,
        min_expected_count=0,
        cooldown_minutes=60,
    )
    rule.filters = []
    scan = uuid.uuid4()
    event = str(uuid.uuid4())

    def candidate(bucket: datetime) -> MetricAnomaly:
        return MetricAnomaly(
            id=uuid.uuid4(),
            scan_config_id=scan,
            scope_type="event",
            scope_ref=event,
            event_id=None,
            event_type_id=None,
            bucket=bucket,
            actual_count=100,
            expected_count=10,
            direction="spike",
        )

    early = candidate(datetime(2026, 9, 23, 14))
    within_cooldown = candidate(datetime(2026, 9, 23, 16, 30, tzinfo=timezone(timedelta(hours=2))))
    after_cooldown = candidate(datetime(2026, 9, 23, 15, 10, tzinfo=UTC))
    assert simulate_rule_firings(rule, [after_cooldown, within_cooldown, early]) == [
        early,
        after_cooldown,
    ]
