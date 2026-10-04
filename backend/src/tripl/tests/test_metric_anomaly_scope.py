"""Anomaly detection + alerting for the catalog-metric scope.

Covers the metric-scope seam end to end:

* recompute persists a ``MetricAnomaly`` under ``scope_type='metric'`` /
  ``scope_ref=str(metric_definition_id)`` with a NULL ``scan_config_id``;
* an ``AlertRule`` delivers metric anomalies only when ``include_metrics`` is on
  (SAFE OFF by default);
* the value-kind gate keeps a fractional (ratio/sql) series with a mid-window
  gap from producing a false drop;
* the existing event-scope path is unchanged (regression guard);
* ``get_active_signals`` surfaces the metric scope.

Sync sqlite fixtures mirror ``test_metrics_tasks.py`` / ``test_alerting.py``.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.analyzers.anomaly_detector import SCOPE_METRIC, DetectedAnomaly
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
)
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.metrics import detect as metrics_detect
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch
from tripl.worker.tasks.metrics import signals as metrics_signals

# 1h-aligned buckets used by the sync detect/dispatch tests. Anchored to a recent
# wall-clock hour so the seeded spike's bucket stays inside the signal freshness
# horizon (classify keeps a latest_scan signal only while its bucket is newer than
# now - max(24h, 3*interval)). Stored buckets are UTC aware, and all
# relative-to-_BASE assertions use the same convention.
_BASE = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=12)
_SPIKE_HOUR = 9
_EVAL_FROM = _BASE + timedelta(hours=8)
_EVAL_TO = _BASE + timedelta(hours=10)

# 1d-aligned anchor for the daily-grid test, also UTC aware.
_DAY = timedelta(days=1)
_DAY_END = _BASE.replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'metric_scope.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_project(session: Session, *, detect_metrics: bool = True) -> ScanConfig:
    project = Project(
        id=uuid.uuid4(),
        name="Metric Scope",
        slug=f"metric-scope-{uuid.uuid4().hex[:8]}",
        description="",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Scan",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    settings = ProjectAnomalySettings(
        project_id=project.id,
        anomaly_detection_enabled=True,
        detect_metrics=detect_metrics,
        # Pinned so the small crafted series (baseline ~10) stay eligible; these
        # tests exercise scope/cooldown mechanics, not the product defaults.
        sigma_threshold=3.0,
        min_expected_count=10,
    )
    session.add_all([project, data_source, config, settings])
    session.commit()
    return config


def _add_metric(
    session: Session,
    config: ScanConfig,
    *,
    kind: MetricKind,
    name: str,
    aggregation: MetricAggregation | None = None,
    composition: MetricComposition | None = None,
    anomaly_enabled: bool = True,
    interval: str | None = "1h",
    status: MetricStatus = MetricStatus.active,
) -> MetricDefinition:
    metric = MetricDefinition(
        id=uuid.uuid4(),
        project_id=config.project_id,
        name=name,
        display_name=name.upper(),
        kind=kind.value,
        aggregation=aggregation.value if aggregation else None,
        composition=composition.value if composition else None,
        config={},
        data_source_id=config.data_source_id,
        interval=interval,
        status=status.value,
        anomaly_detection_enabled=anomaly_enabled,
    )
    session.add(metric)
    session.commit()
    return metric


def _seed_values_at(
    session: Session,
    metric: MetricDefinition,
    values: dict[datetime, float],
    *,
    scan_config_id: uuid.UUID | None = None,
) -> None:
    for bucket, value in values.items():
        session.add(
            MetricValue(
                id=uuid.uuid4(),
                metric_definition_id=metric.id,
                scan_config_id=scan_config_id,
                bucket=bucket,
                value=value,
            )
        )
    session.commit()


def _seed_values(
    session: Session,
    metric: MetricDefinition,
    values: dict[int, float],
    *,
    scan_config_id: uuid.UUID | None = None,
) -> None:
    _seed_values_at(
        session,
        metric,
        {_BASE + timedelta(hours=hour): value for hour, value in values.items()},
        scan_config_id=scan_config_id,
    )


def _metric_anomalies(session: Session, metric_id: uuid.UUID) -> list[MetricAnomaly]:
    return list(
        session.execute(
            select(MetricAnomaly).where(
                MetricAnomaly.scope_type == "metric",
                MetricAnomaly.scope_ref == str(metric_id),
            )
        ).scalars()
    )


def test_recompute_persists_metric_scope_anomaly(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A count-shaped metric with an injected spike persists a metric-scope row."""
    with sync_session_factory() as session:
        config = _seed_project(session)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="signups",
        )
        # Flat baseline (10) for 9 buckets, a clear spike (100) at the last.
        baseline = {hour: 10.0 for hour in range(_SPIKE_HOUR)}
        baseline[_SPIKE_HOUR] = 100.0
        _seed_values(session, metric, baseline)

        detected = metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()

        anomalies = _metric_anomalies(session, metric.id)
        assert detected >= 1
        assert len(anomalies) == 1
        anomaly = anomalies[0]
        assert anomaly.scope_type == "metric"
        assert anomaly.scope_ref == str(metric.id)
        assert anomaly.scan_config_id is None
        assert anomaly.direction == "spike"
        assert anomaly.bucket == _BASE + timedelta(hours=_SPIKE_HOUR)


def test_daily_metric_is_evaluated_on_its_own_grid(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A 1d metric under a 1h scan config must get a 30-DAY candidate window.

    ``collect_metrics`` derives ``evaluation_start`` from the SCAN grid
    (``time_to - 1h * 30``). Passed through verbatim, a daily metric got 30
    HOURS: one candidate bucket, and the settling allowance (one whole bucket of
    a daily grid) withheld exactly that one, so no daily metric could emit at
    all. That is the shape of the live ``rd1`` metric — 1d interval, a scan grid
    of 1h, a collapse to 0.0 and no signal.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="daily_signups",
            interval="1d",
        )
        # 60 days of a flat 100/day that collapses to 0 for the last 5. The
        # history reaches past the 21 buckets the daily phase baseline needs.
        _seed_values_at(
            session,
            metric,
            {_DAY_END - _DAY * day: (0.0 if day <= 5 else 100.0) for day in range(1, 61)},
        )

        detected = metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            # Exactly what collect_metrics computes for a 1h scan config.
            evaluation_start=_DAY_END - timedelta(hours=30),
            evaluation_end=_DAY_END,
            settling_delay=timedelta(hours=2),
        )
        session.commit()

        flagged = {a.bucket: a.direction for a in _metric_anomalies(session, metric.id)}

    assert detected >= 1
    collapsed = {_DAY_END - _DAY * day for day in range(2, 6)}
    flagged_collapse = {bucket: flagged[bucket] for bucket in flagged.keys() & collapsed}
    assert flagged_collapse
    assert set(flagged_collapse.values()) == {"drop"}
    # Every one of them predates the 30-hour window the scan grid produced —
    # whose only daily candidate was the newest day, which settling withheld —
    # so none is reachable without the per-metric window.
    assert max(flagged_collapse) < _DAY_END - timedelta(hours=30)
    # And the newest daily bucket does stay withheld: settling is a per-grid
    # count, and 2h of allowance rounds up to one whole day.
    assert _DAY_END - _DAY not in flagged


def test_detect_metrics_disabled_emits_nothing_new(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A pass with the Metrics scope unticked writes no metric-scope row.

    Only that. Nothing is seeded beforehand, so this says nothing about which
    EXISTING rows the disabled pass may delete — that bound is pinned by
    ``test_metric_scope_purge.test_detect_metrics_disabled_keeps_history_older_than_the_reeval_window``.
    """
    with sync_session_factory() as session:
        config = _seed_project(session, detect_metrics=False)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="signups",
        )
        baseline = {hour: 10.0 for hour in range(_SPIKE_HOUR)}
        baseline[_SPIKE_HOUR] = 100.0
        _seed_values(session, metric, baseline)

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()

        assert _metric_anomalies(session, metric.id) == []


def test_fractional_metric_gap_does_not_false_anomaly(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A fractional (sql) series with a mid-window gap must NOT flag a drop.

    The gap bucket (hour 8) is inside the evaluation window. Were it zero-filled
    (the count-shaped assumption), the 3 -> 0 step would score a multi-sigma
    drop; the value-kind gate keeps fractional gaps absent so no anomaly fires.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        metric = _add_metric(session, config, kind=MetricKind.sql, name="ratio")
        # Stable level of 3.0 everywhere EXCEPT hour 8, which is left as a gap.
        values = {hour: 3.0 for hour in range(_SPIKE_HOUR + 1) if hour != 8}
        _seed_values(session, metric, values)

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()

        assert _metric_anomalies(session, metric.id) == []


def _seed_spiked_metric(session: Session) -> tuple[ScanConfig, MetricDefinition]:
    config = _seed_project(session)
    metric = _add_metric(
        session,
        config,
        kind=MetricKind.fact,
        aggregation=MetricAggregation.count,
        composition=MetricComposition.single,
        name="signups",
    )
    baseline = {hour: 10.0 for hour in range(_SPIKE_HOUR)}
    baseline[_SPIKE_HOUR] = 100.0
    _seed_values(session, metric, baseline)
    metrics_detect._recalculate_metric_anomalies(
        session,
        config,
        evaluation_start=_EVAL_FROM,
        evaluation_end=_EVAL_TO,
    )
    session.commit()
    return config, metric


def _add_rule(session: Session, config: ScanConfig, *, include_metrics: bool) -> AlertRule:
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=config.project_id,
        type="slack",
        name="Main Slack",
        enabled=True,
        webhook_url_encrypted="secret",
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Metric Rule",
        enabled=True,
        include_project_total=False,
        include_event_types=False,
        include_events=False,
        include_schema_drifts=False,
        include_distribution_drifts=False,
        include_release_regressions=False,
        include_metrics=include_metrics,
        notify_on_spike=True,
        notify_on_drop=False,
        min_percent_delta=0,
        min_absolute_delta=0,
        min_expected_count=0,
        cooldown_minutes=1440,
    )
    session.add_all([destination, rule])
    session.commit()
    return rule


def test_alert_rule_delivers_metric_anomaly_when_included(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, metric = _seed_spiked_metric(session)
        _add_rule(session, config, include_metrics=True)

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        assert len(delivery_ids) == 1
        delivery = session.execute(select(AlertDelivery)).scalar_one()
        item = session.execute(select(AlertDeliveryItem)).scalar_one()
        assert delivery.matched_count == 1
        assert item.scope_type == "metric"
        assert item.scope_ref == str(metric.id)
        assert item.scope_name == metric.display_name
        assert item.direction == "spike"


def test_alert_rule_skips_metric_anomaly_when_excluded(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        config, _metric = _seed_spiked_metric(session)
        _add_rule(session, config, include_metrics=False)

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)

        assert delivery_ids == []
        assert session.execute(select(AlertDelivery)).scalars().all() == []


def test_event_scope_anomaly_still_detected(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Regression guard: the event-type scope path is unaffected by the metric branch."""
    with sync_session_factory() as session:
        config = _seed_project(session)
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=config.project_id,
            name="page",
            display_name="Page",
            description="",
        )
        session.add(event_type)
        session.flush()
        event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=event_type.id,
            name="event_name=View",
            description="",
            status="implemented",
        )
        session.add(event)
        for hour in range(_SPIKE_HOUR + 1):
            count = 200 if hour == _SPIKE_HOUR else 10
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=None,
                    event_type_id=event_type.id,
                    bucket=_BASE + timedelta(hours=hour),
                    count=count,
                )
            )
        session.commit()

        detected = metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()

        event_type_anomalies = list(
            session.execute(
                select(MetricAnomaly).where(
                    MetricAnomaly.scope_type == "event_type",
                    MetricAnomaly.scan_config_id == config.id,
                )
            ).scalars()
        )
        assert detected >= 1
        assert len(event_type_anomalies) == 1
        assert event_type_anomalies[0].direction == "spike"
        # No catalog metrics exist, so no metric-scope rows were written.
        assert (
            session.execute(select(MetricAnomaly).where(MetricAnomaly.scope_type == "metric"))
            .scalars()
            .all()
            == []
        )


@pytest.mark.asyncio
async def test_get_active_signals_surfaces_metric_scope(client: AsyncClient) -> None:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Signal Metric", "slug": "signal-metric", "description": ""},
    )
    assert project_resp.status_code == 201
    project_id = uuid.UUID(project_resp.json()["id"])

    metric_id = uuid.uuid4()
    # Recent, hour-aligned bucket so the seeded anomaly on the latest value bucket
    # classifies as an open "latest_scan" signal (inside the freshness horizon).
    bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    async with TestSessionLocal() as session:
        session.add(
            MetricDefinition(
                id=metric_id,
                project_id=project_id,
                name="conv",
                display_name="Conversions",
                kind=MetricKind.fact.value,
                aggregation=MetricAggregation.count.value,
                composition=MetricComposition.single.value,
                config={},
                interval="1h",
                status=MetricStatus.active.value,
                anomaly_detection_enabled=True,
            )
        )
        session.add(
            MetricValue(
                id=uuid.uuid4(),
                metric_definition_id=metric_id,
                scan_config_id=None,
                bucket=bucket,
                value=100.0,
            )
        )
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=None,
                scope_type="metric",
                scope_ref=str(metric_id),
                event_id=None,
                event_type_id=None,
                bucket=bucket,
                actual_count=100,
                expected_count=10.0,
                stddev=1.0,
                z_score=9.0,
                direction="spike",
            )
        )
        await session.commit()

    resp = await client.get("/api/v1/projects/signal-metric/anomalies/signals")
    assert resp.status_code == 200, resp.text
    signals = resp.json()
    metric_signals = [signal for signal in signals if signal["scope_type"] == "metric"]
    assert len(metric_signals) == 1
    assert metric_signals[0]["scope_ref"] == str(metric_id)
    assert metric_signals[0]["scan_config_id"] is None
    assert metric_signals[0]["state"] == "latest_scan"
    assert metric_signals[0]["direction"] == "spike"


def _metric_anomaly_row(
    metric_id: uuid.UUID, bucket: datetime, **overrides: object
) -> MetricAnomaly:
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "scan_config_id": None,
        "scope_type": "metric",
        "scope_ref": str(metric_id),
        "event_id": None,
        "event_type_id": None,
        "bucket": bucket,
        "actual_count": 100,
        "expected_count": 10.0,
        "stddev": 1.0,
        "z_score": 9.0,
        "direction": "spike",
    }
    fields.update(overrides)
    return MetricAnomaly(**fields)


def test_metric_scope_partial_unique_index_blocks_duplicate_rows(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Two project-global metric rows for the same (scope, bucket) must collide.

    The composite ``uq_metric_anomaly_scope_bucket`` includes ``scan_config_id``,
    whose NULLs SQL treats as DISTINCT, so it cannot dedupe ``metric``-scope rows.
    The partial unique index ``uq_metric_anomaly_metric_scope`` does. Without the
    fix this insert succeeds and silently duplicates.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="signups",
        )
        bucket = _BASE + timedelta(hours=_SPIKE_HOUR)

        session.add(_metric_anomaly_row(metric.id, bucket))
        session.commit()

        session.add(_metric_anomaly_row(metric.id, bucket))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_recompute_metric_scope_is_idempotent_across_runs(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A second recompute over the same window keeps exactly one row per bucket."""
    with sync_session_factory() as session:
        config, metric = _seed_spiked_metric(session)  # first detection run

        metrics_detect._recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=_EVAL_FROM,
            evaluation_end=_EVAL_TO,
        )
        session.commit()

        assert len(_metric_anomalies(session, metric.id)) == 1


def test_metric_scope_upsert_absorbs_conflicting_row(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """ON CONFLICT DO UPDATE dedupes a metric-scope row the DELETE did not clear.

    Simulates the concurrent-run race: a ``(NULL, 'metric', ref, bucket)`` row
    already exists when a second run inserts the same row. The DELETE window here
    deliberately excludes ``bucket`` so the existing row survives and the INSERT
    collides on it — exercising the partial-index ON CONFLICT branch directly.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            composition=MetricComposition.single,
            name="signups",
        )
        bucket = _BASE + timedelta(hours=_SPIKE_HOUR)
        session.add(_metric_anomaly_row(metric.id, bucket, actual_count=1, z_score=1.0))
        session.commit()

        metrics_detect._replace_scope_anomalies(
            session,
            scan_config_id=None,
            scope_type=SCOPE_METRIC,
            scope_ref=str(metric.id),
            evaluation_start=bucket + timedelta(hours=1),
            evaluation_end=bucket + timedelta(hours=2),
            event_id=None,
            event_type_id=None,
            anomalies=[
                DetectedAnomaly(
                    bucket=bucket,
                    actual_count=100,
                    expected_count=10.0,
                    stddev=2.0,
                    z_score=9.0,
                    direction="spike",
                )
            ],
        )
        session.commit()

        anomalies = _metric_anomalies(session, metric.id)
        assert len(anomalies) == 1
        assert anomalies[0].actual_count == 100
        assert anomalies[0].z_score == 9.0


def _add_second_config(session: Session, config: ScanConfig) -> ScanConfig:
    second = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=config.data_source_id,
        project_id=config.project_id,
        name="Scan 2",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    session.add(second)
    session.commit()
    return second


def test_metric_scope_cooldown_shared_across_scan_configs(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Metric anomalies fire ONE alert per project, not one per scan config.

    Metric-scope rows are project-global. Keying AlertRuleState on each config's
    id would give each config its own cooldown clock and re-deliver the same
    anomaly N times. A NULL scan_config_id — the project-global key — makes the cooldown global.
    """
    with sync_session_factory() as session:
        config1, _metric = _seed_spiked_metric(session)
        config2 = _add_second_config(session, config1)
        _add_rule(session, config1, include_metrics=True)

        first = metrics_dispatch._prepare_alert_deliveries(session, config1, scan_job_id=None)
        assert len(first) == 1

        metric_states = list(
            session.execute(
                select(AlertRuleState).where(AlertRuleState.scope_type == "metric")
            ).scalars()
        )
        assert len(metric_states) == 1
        assert metric_states[0].scan_config_id is None

        # The send step would stamp last_notified_at; simulate it so cooldown applies.
        metric_states[0].last_notified_at = datetime.now(UTC)
        session.commit()

        second = metrics_dispatch._prepare_alert_deliveries(session, config2, scan_job_id=None)
        assert second == []

        states_after = list(
            session.execute(
                select(AlertRuleState).where(AlertRuleState.scope_type == "metric")
            ).scalars()
        )
        assert len(states_after) == 1
        assert states_after[0].scan_config_id is None


def test_sending_stamps_the_project_global_metric_state(
    sync_session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """The cooldown above is only real if the send step can find the state.

    Its sibling simulates the stamp — "the send step would stamp
    last_notified_at; simulate it so cooldown applies" — which is exactly why
    this went unnoticed. The real path looked the state up by the DELIVERY's
    scan_config_id, while a metric-scope state stores no scan config at all, so
    matching on the delivery's own config found nothing.
    last_notified_at stayed NULL and the re-send gate reads NULL as "never told
    them", so the cooldown was permanently elapsed for metric scopes.
    """
    from tripl.worker.tasks import alerts as alerts_task

    with sync_session_factory() as session:
        config1, _metric = _seed_spiked_metric(session)
        config2 = _add_second_config(session, config1)
        # Deliver from either config: a metric-scope state is anchored on
        # neither, so the lookup has to find it without one.
        sending_config = config2
        _add_rule(session, config1, include_metrics=True)

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(
            session, sending_config, scan_job_id=None
        )
        assert len(delivery_ids) == 1
        state = session.execute(
            select(AlertRuleState).where(AlertRuleState.scope_type == "metric")
        ).scalar_one()
        assert state.scan_config_id is None
        assert state.last_notified_at is None
        session.commit()

    monkeypatch.setitem(
        alerts_task.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        sync_session_factory,
    )
    monkeypatch.setitem(
        alerts_task.send_alert_delivery.run.__globals__,
        "_post_json",
        lambda *args, **kwargs: None,
    )
    # The shared fixture stores a placeholder secret; the send path validates the
    # decrypted value as a real Slack webhook before posting.
    monkeypatch.setitem(
        alerts_task.send_alert_delivery.run.__globals__,
        "_decrypt_secret",
        lambda _value: "https://hooks.slack.com/services/T0/B0/xxxxxxxx",
    )

    result = alerts_task.send_alert_delivery.run(str(delivery_ids[0]))

    assert result["status"] == "sent"
    with sync_session_factory() as session:
        stamped = session.execute(
            select(AlertRuleState).where(AlertRuleState.scope_type == "metric")
        ).scalar_one()
        assert stamped.last_notified_at is not None


def test_percent_metric_items_render_scaled_values_via_batched_unit_lookup(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Live send path: _build_items_text resolves unit='%' for metric-scope
    items with one batched MetricDefinition query and renders the stored
    fractions ×100 with a '%' suffix; the cached units keep the session-less
    fallback render (MarkdownV2 → plain) byte-identical."""
    from tripl.alert_templates import get_default_items_template
    from tripl.worker.tasks import alerts_messages

    with sync_session_factory() as session:
        config = _seed_project(session)
        percent_metric = _add_metric(
            session,
            config,
            kind=MetricKind.sql,
            name="conversion_rate",
        )
        percent_metric.unit = "%"
        plain_metric = _add_metric(
            session,
            config,
            kind=MetricKind.sql,
            name="latency_ms",
        )
        plain_metric.unit = "ms"
        session.commit()

        def _item(metric: MetricDefinition, *, actual: float, expected: float) -> AlertDeliveryItem:
            return AlertDeliveryItem(
                id=uuid.uuid4(),
                delivery_id=uuid.uuid4(),
                scope_type="metric",
                scope_ref=str(metric.id),
                scope_name=metric.display_name,
                event_id=None,
                event_type_id=None,
                bucket=_BASE + timedelta(hours=9),
                direction="spike",
                actual_count=actual,
                expected_count=expected,
                absolute_delta=abs(actual - expected),
                percent_delta=100.0,
            )

        items = [
            _item(percent_metric, actual=0.08, expected=0.04),
            _item(plain_metric, actual=120.5, expected=60.0),
        ]
        units_cache: dict[str, str | None] = {}
        text = alerts_messages._build_items_text(
            items,
            message_format="plain",
            items_template=get_default_items_template("plain"),
            session=session,
            scan_config_id=None,
            metric_units_cache=units_cache,
        )
        assert "actual=8%" in text
        assert "expected=4%" in text
        assert "delta=4% (100.0%)" in text
        # Non-percent metric on the same delivery stays raw.
        assert "actual=120.5" in text
        assert "expected=60," in text
        assert units_cache == {str(percent_metric.id): "%", str(plain_metric.id): "ms"}

        # Fallback re-render without a session reuses the cache: same text.
        fallback = alerts_messages._build_items_text(
            items,
            message_format="plain",
            items_template=get_default_items_template("plain"),
            session=None,
            scan_config_id=None,
            metric_units_cache=units_cache,
        )
        assert fallback == text


# ---------------------------------------------------------------------------
# A catalog metric's alert candidacy is a property of the METRIC
# ---------------------------------------------------------------------------

# 30 hours back on the naive footing the sync fixtures use: outside a bare 24h
# freshness window, inside the daily grid's own max(24h, 3 * 1d) = 72h horizon.
_STALE_ON_AN_HOURLY_GRID = datetime.now(UTC).replace(
    minute=0, second=0, microsecond=0, tzinfo=None
) - timedelta(hours=30)


def _add_daily_config(session: Session, config: ScanConfig) -> ScanConfig:
    daily = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=config.data_source_id,
        project_id=config.project_id,
        name="Daily Scan",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1d",
    )
    session.add(daily)
    session.commit()
    return daily


def test_metric_alert_candidacy_ignores_which_scan_dispatches(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """An ``event_composition`` metric is judged on ITS grid, not the caller's.

    ``_prepare_alert_deliveries`` runs once per scan config and every run sees the
    same project-global metric anomalies, sharing ONE AlertRuleState row. Judging
    an interval-less catalog metric on ``config.interval`` therefore made the same
    metric a candidate under the daily scan and a non-candidate under the hourly
    one — the two dispatch runs open and close the same alert state in turn.
    The metric's real grid is the daily scan its values are stamped with, so both
    runs must agree on the daily answer.
    """
    with sync_session_factory() as session:
        hourly = _seed_project(session)
        daily = _add_daily_config(session, hourly)
        metric = _add_metric(
            session,
            hourly,
            kind=MetricKind.event_composition,
            composition=MetricComposition.single,
            name="checkout_starts",
            # event_composition carries no grid of its own; the stamp on its
            # values below is where the grid actually lives.
            interval=None,
        )
        _seed_values_at(
            session,
            metric,
            {_STALE_ON_AN_HOURLY_GRID: 99.0},
            scan_config_id=daily.id,
        )
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=None,
                scope_type="metric",
                scope_ref=str(metric.id),
                event_id=None,
                event_type_id=None,
                bucket=_STALE_ON_AN_HOURLY_GRID,
                actual_count=99,
                expected_count=40,
                stddev=5,
                z_score=8,
                direction="spike",
            )
        )
        session.commit()

        under_hourly = metrics_signals._get_active_metric_anomaly_candidates(session, hourly)
        under_daily = metrics_signals._get_active_metric_anomaly_candidates(session, daily)

    assert set(under_daily) == {("metric", str(metric.id))}
    assert set(under_hourly) == set(under_daily)


# A settled head on the hourly grid: value and anomaly share a bucket two hours
# back, so the head test (anomaly_bucket >= latest_value_bucket - emission_lag)
# holds by construction and only the freshness horizon max(24h, 3 * 1h) could
# close it. That is exactly the state a metric freezes in when it stops
# collecting, which is why archiving it had to be what closes it.
_SETTLED_HEAD = _BASE + timedelta(hours=10)


@pytest.mark.parametrize("parked", [MetricStatus.archived, MetricStatus.draft])
def test_a_metric_that_left_active_stops_being_an_alert_candidate(
    sync_session_factory: sessionmaker[Session],
    parked: MetricStatus,
) -> None:
    """Archiving a metric must switch its alerts off, not merely age them out.

    Detection has always required ``status == active`` AND the flag; alert
    candidacy required only the flag, so the consumer was WIDER than its own
    producer. That is not harmless dead code, because leaving ``active`` deletes
    nothing: ``_purge_project_metric_anomalies`` runs only when the project
    MASTER switch ``anomaly_detection_enabled`` is off (unticking the Metrics
    scope box runs ``_purge_disabled_metric_scope`` instead, which clears only
    each metric's own re-evaluation window and leaves older rows as history),
    ``_age_out_config_anomalies`` matches on ``scan_config_id``
    and metric-scope rows are NULL there, and the catalog's bulk archive is a
    bare ``UPDATE ... SET status``. Collection stops as well, so the metric's
    newest value bucket freezes and its last anomaly keeps testing as the settled
    head — leaving a live alert candidate that could NEWLY send to Telegram or
    Slack about a metric the user had just archived, for up to
    max(recent_window, 3 x interval) afterwards (three weeks on a 1w grid).

    ``draft`` answers the same way: ``check_metric_definitions_due`` dispatches
    only ``active``, so a metric parked in draft is just as frozen as an
    archived one.
    """
    with sync_session_factory() as session:
        config = _seed_project(session)
        metric = _add_metric(
            session,
            config,
            kind=MetricKind.fact,
            aggregation=MetricAggregation.count,
            name="checkout_completed",
        )
        _seed_values_at(session, metric, {_SETTLED_HEAD: 99.0})
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=None,
                scope_type=SCOPE_METRIC,
                scope_ref=str(metric.id),
                event_id=None,
                event_type_id=None,
                bucket=_SETTLED_HEAD,
                actual_count=99,
                expected_count=40,
                stddev=5,
                z_score=8,
                direction="spike",
            )
        )
        session.commit()

        # Non-vacuous: while active this really is an open alert candidate.
        while_active = metrics_signals._get_active_metric_anomaly_candidates(session, config)

        metric.status = parked.value
        session.commit()
        after_parking = metrics_signals._get_active_metric_anomaly_candidates(session, config)

    assert set(while_active) == {(SCOPE_METRIC, str(metric.id))}
    assert set(after_parking) == set()
