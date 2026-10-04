"""End-to-end metrics pipeline integration tests.

Exercises the FULL catalog-metric pipeline for each kind:

    definition -> collection -> anomaly recompute -> alert dispatch

using the real services with the sync-sqlite fixture style of
``test_metric_collection.py`` / ``test_metric_anomaly_scope.py`` /
``test_alerting.py`` / ``test_metrics_tasks.py``: a file-backed SQLite engine
from ``Base.metadata.create_all`` with the collection module's
``_get_sync_session`` / ``_build_adapter`` / ``_resolve_value_window`` globals
monkey-patched per test, and fake warehouse adapters / seeded ``event_metrics``
rows feeding the collectors.

Each kind builds a real spiking series, collects it into ``metric_values``,
recomputes the metric-scope anomaly (the ``detect.py`` metric branch), and then
dispatches an ``AlertRule`` both with ``include_metrics=True`` (a delivery is
produced) and ``include_metrics=False`` (NONE), proving the SAFE-OFF gate.

Per-dialect adapter aggregation coverage lives in ``test_adapter_aggregations.py``;
the wiring assertion here is intentionally light (collection invokes
``get_time_bucketed_aggregate`` for ``fact``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.base import ColumnInfo
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
    ScanInterval,
)
from tripl.models.event_metric import EventMetric
from tripl.models.fact_table import FactTable
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.worker.tasks.metrics import detect as metrics_detect
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch
from tripl.worker.tasks.metrics import metric_collect

# 1h-aligned grid shared by every test. A flat baseline for hours 0..8 and a
# clear spike at hour 9; the evaluation window covers the spike bucket. Anchored
# to a recent wall-clock hour so the dispatched spike's bucket stays inside the
# alert-candidate freshness horizon (classify keeps a signal open only while its
# bucket is newer than now - max(24h, 3*interval)).
_BASE = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=12)
_SPIKE_HOUR = 9
_EVAL_FROM = _BASE + timedelta(hours=8)
_EVAL_TO = _BASE + timedelta(hours=10)
# Collection window fed to the (monkey-patched) ``_resolve_value_window``; with
# the default NULL ``replay_chunk_interval`` this is a single query chunk, so the
# fake adapter's rows are written verbatim (no per-chunk re-upsert double count).
_VALUE_WINDOW = (_BASE, _BASE + timedelta(hours=10))


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'metrics_pipeline_e2e.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


# ── seeding helpers ──────────────────────────────────────────────────────────


def _seed_pipeline_base(session: Session) -> tuple[Project, DataSource, ScanConfig]:
    """Seed project + data source + scan config + anomaly settings.

    Anomaly detection is enabled with ONLY the metric scope on, so the
    project-total / event-type / event branches stay quiet and the pipeline's
    output is the metric-scope row alone.
    """
    project = Project(
        id=uuid.uuid4(),
        name="Pipeline Project",
        slug=f"pipeline-{uuid.uuid4().hex[:8]}",
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
        name="Source Scan",
        base_query="SELECT ts, user_id FROM events",
        time_column="ts",
        cardinality_threshold=100,
        interval="1h",
    )
    settings = ProjectAnomalySettings(
        project_id=project.id,
        anomaly_detection_enabled=True,
        detect_project_total=False,
        detect_event_types=False,
        detect_events=False,
        detect_metrics=True,
        # Pinned so the small crafted series (baseline ~10) stay eligible; these
        # tests exercise pipeline mechanics, not the product defaults.
        sigma_threshold=3.0,
        min_expected_count=10,
    )
    session.add_all([project, data_source, config, settings])
    session.commit()
    return project, data_source, config


def _seed_fact_table(session: Session, project: Project, data_source: DataSource) -> FactTable:
    """A fact table over the warehouse revenue rowset (source of ``fact`` metrics)."""
    fact_table = FactTable(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"ft-{uuid.uuid4().hex[:6]}",
        display_name="Revenue Facts",
        data_source_id=data_source.id,
        sql="SELECT ts, amount FROM revenue",
        timestamp_column="ts",
        columns=[
            {"name": "ts", "type": "timestamp"},
            {"name": "amount", "type": "number"},
        ],
        identifier_columns=[],
        row_filters=[],
    )
    session.add(fact_table)
    session.commit()
    return fact_table


def _spike_rows(base_value: float, spike_value: float) -> list[tuple[datetime, float]]:
    """Interval-bucketed (bucket, value) rows: flat baseline + a hour-9 spike."""
    rows = [(_BASE + timedelta(hours=hour), base_value) for hour in range(_SPIKE_HOUR)]
    rows.append((_BASE + timedelta(hours=_SPIKE_HOUR), spike_value))
    return rows


def _seed_event_metric_series(
    session: Session,
    config: ScanConfig,
    event_id: uuid.UUID,
    counts: dict[datetime, float],
) -> None:
    for bucket, count in counts.items():
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=event_id,
                event_type_id=None,
                bucket=bucket,
                count=count,
            )
        )
    session.commit()


def _add_rule(session: Session, config: ScanConfig, *, include_metrics: bool) -> AlertRule:
    """Slack rule that subscribes ONLY to metric-scope spikes (mirrors _alerting)."""
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


def _metric_anomalies(session: Session, metric_id: uuid.UUID) -> list[MetricAnomaly]:
    return list(
        session.execute(
            select(MetricAnomaly).where(
                MetricAnomaly.scope_type == "metric",
                MetricAnomaly.scope_ref == str(metric_id),
            )
        ).scalars()
    )


def _recompute_metric_scope(session: Session, config: ScanConfig) -> int:
    detected = metrics_detect._recalculate_metric_anomalies(
        session,
        config,
        evaluation_start=_EVAL_FROM,
        evaluation_end=_EVAL_TO,
    )
    session.commit()
    return detected


def _assert_metric_anomaly_persisted(session: Session, metric_id: uuid.UUID) -> None:
    """The metric branch persists ONE project-global metric-scope spike row."""
    anomalies = _metric_anomalies(session, metric_id)
    assert len(anomalies) == 1
    anomaly = anomalies[0]
    assert anomaly.scope_type == "metric"
    assert anomaly.scope_ref == str(metric_id)
    assert anomaly.scan_config_id is None
    assert anomaly.direction == "spike"
    assert anomaly.bucket == _BASE + timedelta(hours=_SPIKE_HOUR)


def _assert_include_metrics_gate(
    session: Session, config: ScanConfig, metric: MetricDefinition
) -> None:
    """include_metrics=True delivers the metric anomaly; flipping it off => NONE."""
    rule = _add_rule(session, config, include_metrics=True)

    delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
    assert len(delivery_ids) == 1
    delivery = session.execute(select(AlertDelivery)).scalar_one()
    item = session.execute(select(AlertDeliveryItem)).scalar_one()
    assert delivery.matched_count == 1
    assert item.scope_type == "metric"
    assert item.scope_ref == str(metric.id)
    assert item.scope_name == metric.display_name
    assert item.direction == "spike"

    # SAFE OFF: with include_metrics disabled the same anomaly produces NONE.
    rule.include_metrics = False
    session.commit()
    none_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
    assert none_ids == []
    # No second delivery was created (the original include=True one still stands).
    assert len(session.execute(select(AlertDelivery)).scalars().all()) == 1


# ── warehouse adapter fakes (mirror test_metric_collection.py) ─────────────────


class _FactAdapter:
    """Fake adapter whose ``get_time_bucketed_aggregate`` returns canned rows."""

    def __init__(self, rows: list[tuple[datetime, float]]) -> None:
        self._rows = rows

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [
            ColumnInfo(name="ts", type_name="DateTime"),
            ColumnInfo(name="amount", type_name="Float64"),
        ]

    def get_time_bucketed_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        return ([], [], list(self._rows))

    def close(self) -> None:
        return None


class _RecordingFactAdapter(_FactAdapter):
    """Fact adapter that records the aggregate-call args for the wiring test."""

    def __init__(self, rows: list[tuple[datetime, float]]) -> None:
        super().__init__(rows)
        self.aggregate_calls: list[MetricAggregation] = []

    def get_time_bucketed_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self.aggregate_calls.append(agg_fn)
        return ([], [], list(self._rows))


class _SqlAdapter:
    """Fake adapter whose ``get_preview_rows`` returns a canned per-bucket SELECT."""

    def __init__(self, column_names: list[str], rows: list[tuple[object, ...]]) -> None:
        self._column_names = column_names
        self._rows = rows

    def test_connection(self) -> bool:
        return True

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        return self._column_names, list(self._rows)

    def close(self) -> None:
        return None


class _DistinctUserAdapter:
    """Fake adapter for the per_distinct_user denominator (count_distinct)."""

    def __init__(self, rows: list[tuple[datetime, float]]) -> None:
        self._rows = rows

    def test_connection(self) -> bool:
        return True

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        # The columns the source scan's base_query projects: the collector
        # introspects them to arm the adapter allowlist before it can hand
        # the distinct-user column to the aggregate.
        return [
            ColumnInfo(name="ts", type_name="DateTime"),
            ColumnInfo(name="user_id", type_name="String"),
        ]

    def get_time_bucketed_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        assert agg_fn is MetricAggregation.count_distinct
        assert measure_column == "user_id"
        return ([], [], list(self._rows))

    def close(self) -> None:
        return None


def _patch_collection(
    monkeypatch: MonkeyPatch,
    factory: sessionmaker[Session],
    *,
    adapter: object | None = None,
) -> None:
    monkeypatch.setattr(metric_collect, "_get_sync_session", factory)
    monkeypatch.setattr(
        metric_collect,
        "_resolve_value_window",
        lambda *a, **k: _VALUE_WINDOW,
    )
    if adapter is not None:
        monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)


# ── 1. fact ────────────────────────────────────────────────────────────────────


def test_fact_pipeline_end_to_end(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        project, data_source, config = _seed_pipeline_base(session)
        fact_table = _seed_fact_table(session, project, data_source)
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name=f"fact-{uuid.uuid4().hex[:6]}",
            display_name="Revenue",
            kind=MetricKind.fact,
            composition=MetricComposition.single,
            aggregation=MetricAggregation.sum,
            fact_table_id=fact_table.id,
            config={"measure_column": "amount"},
            interval=ScanInterval.h1,
            status=MetricStatus.active,
            anomaly_detection_enabled=True,
        )
        session.add(metric)
        session.commit()
        def_id = str(metric.id)
        metric_id = metric.id
        config_id = config.id

    # ── collection ──
    _patch_collection(
        monkeypatch, sync_session_factory, adapter=_FactAdapter(_spike_rows(10.0, 100.0))
    )
    result = metric_collect.collect_metric_definitions.run(def_id)
    assert result["values"] == 10

    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == metric_id)
            )
            .scalars()
            .all()
        )
        # fact values are project-global (scan_config_id NULL).
        assert all(row.scan_config_id is None for row in rows)
        stored = {(row.bucket, row.value) for row in rows}
        expected = {(_BASE + timedelta(hours=hour), 10.0) for hour in range(_SPIKE_HOUR)}
        expected.add((_BASE + timedelta(hours=_SPIKE_HOUR), 100.0))
        assert stored == expected

    # ── anomaly recompute + alert dispatch ──
    with sync_session_factory() as session:
        config = session.get(ScanConfig, config_id)
        assert config is not None
        detected = _recompute_metric_scope(session, config)
        assert detected >= 1
        _assert_metric_anomaly_persisted(session, metric_id)

        metric = session.get(MetricDefinition, metric_id)
        assert metric is not None
        _assert_include_metrics_gate(session, config, metric)


# ── 2. sql ─────────────────────────────────────────────────────────────────────


def test_sql_pipeline_end_to_end(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    with sync_session_factory() as session:
        project, data_source, config = _seed_pipeline_base(session)
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name=f"sql-{uuid.uuid4().hex[:6]}",
            display_name="Active sessions",
            kind=MetricKind.sql,
            config={
                "metric_sql": "SELECT ts AS bucket_ts, count() AS value FROM t GROUP BY bucket_ts",
                "time_column": "bucket_ts",
            },
            data_source_id=data_source.id,
            interval=ScanInterval.h1,
            status=MetricStatus.active,
            anomaly_detection_enabled=True,
        )
        session.add(metric)
        session.commit()
        def_id = str(metric.id)
        metric_id = metric.id
        config_id = config.id

    # ── collection via the preview/select path ──
    _patch_collection(
        monkeypatch,
        sync_session_factory,
        adapter=_SqlAdapter(["bucket_ts", "value"], list(_spike_rows(5.0, 200.0))),
    )
    result = metric_collect.collect_metric_definitions.run(def_id)
    assert result["values"] == 10

    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == metric_id)
            )
            .scalars()
            .all()
        )
        assert all(row.scan_config_id is None for row in rows)
        stored = {(row.bucket, row.value) for row in rows}
        expected = {(_BASE + timedelta(hours=hour), 5.0) for hour in range(_SPIKE_HOUR)}
        expected.add((_BASE + timedelta(hours=_SPIKE_HOUR), 200.0))
        assert stored == expected

    # ── anomaly recompute + alert dispatch ──
    with sync_session_factory() as session:
        config = session.get(ScanConfig, config_id)
        assert config is not None
        detected = _recompute_metric_scope(session, config)
        assert detected >= 1
        _assert_metric_anomaly_persisted(session, metric_id)

        metric = session.get(MetricDefinition, metric_id)
        assert metric is not None
        _assert_include_metrics_gate(session, config, metric)


# ── 3a. event_composition: single ──────────────────────────────────────────────


def test_event_composition_single_pipeline_end_to_end(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    numerator_event_id = uuid.uuid4()
    with sync_session_factory() as session:
        project, _ds, config = _seed_pipeline_base(session)
        counts = {bucket: value for bucket, value in _spike_rows(10.0, 100.0)}
        _seed_event_metric_series(session, config, numerator_event_id, counts)
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name=f"single-{uuid.uuid4().hex[:6]}",
            display_name="Logins",
            kind=MetricKind.event_composition,
            composition=MetricComposition.single,
            numerator_event_id=numerator_event_id,
            status=MetricStatus.active,
            anomaly_detection_enabled=True,
        )
        session.add(metric)
        session.commit()
        def_id = str(metric.id)
        metric_id = metric.id
        config_id = config.id

    # event_composition reads stored event_metrics; no warehouse adapter needed.
    _patch_collection(monkeypatch, sync_session_factory)
    result = metric_collect.collect_metric_definitions.run(def_id)
    assert result["values"] == 10

    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == metric_id)
            )
            .scalars()
            .all()
        )
        # event_composition values are keyed by the source scan grid.
        assert all(row.scan_config_id == config_id for row in rows)
        stored = {(row.bucket, row.value) for row in rows}
        expected = {(_BASE + timedelta(hours=hour), 10.0) for hour in range(_SPIKE_HOUR)}
        expected.add((_BASE + timedelta(hours=_SPIKE_HOUR), 100.0))
        assert stored == expected

    with sync_session_factory() as session:
        config = session.get(ScanConfig, config_id)
        assert config is not None
        detected = _recompute_metric_scope(session, config)
        assert detected >= 1
        _assert_metric_anomaly_persisted(session, metric_id)

        metric = session.get(MetricDefinition, metric_id)
        assert metric is not None
        _assert_include_metrics_gate(session, config, metric)


# ── 3b. event_composition: ratio (zero-denominator bucket is ABSENT) ───────────


def test_event_composition_ratio_pipeline_end_to_end(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    numerator_event_id = uuid.uuid4()
    denominator_event_id = uuid.uuid4()
    zero_denom_hour = 8
    with sync_session_factory() as session:
        project, _ds, config = _seed_pipeline_base(session)
        # Numerator: flat 30 then a hour-9 spike of 300.
        numerator = {bucket: value for bucket, value in _spike_rows(30.0, 300.0)}
        _seed_event_metric_series(session, config, numerator_event_id, numerator)
        # Denominator: 10 everywhere EXCEPT hour 8, which is 0 (divide-by-zero).
        denominator = {
            _BASE + timedelta(hours=hour): (0.0 if hour == zero_denom_hour else 10.0)
            for hour in range(_SPIKE_HOUR + 1)
        }
        _seed_event_metric_series(session, config, denominator_event_id, denominator)
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name=f"ratio-{uuid.uuid4().hex[:6]}",
            display_name="Conversion",
            kind=MetricKind.event_composition,
            composition=MetricComposition.ratio,
            numerator_event_id=numerator_event_id,
            denominator_event_id=denominator_event_id,
            status=MetricStatus.active,
            anomaly_detection_enabled=True,
        )
        session.add(metric)
        session.commit()
        def_id = str(metric.id)
        metric_id = metric.id
        config_id = config.id

    _patch_collection(monkeypatch, sync_session_factory)
    metric_collect.collect_metric_definitions.run(def_id)

    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == metric_id)
            )
            .scalars()
            .all()
        )
        stored = {row.bucket: row.value for row in rows}
        # Zero-denominator bucket (hour 8) maps to None => ABSENT, never 0.0.
        assert _BASE + timedelta(hours=zero_denom_hour) not in stored
        # Baseline ratio 3.0 on hours 0..7, spike 30.0 at hour 9.
        for hour in range(zero_denom_hour):
            assert stored[_BASE + timedelta(hours=hour)] == 3.0
        assert stored[_BASE + timedelta(hours=_SPIKE_HOUR)] == 30.0

    with sync_session_factory() as session:
        config = session.get(ScanConfig, config_id)
        assert config is not None
        detected = _recompute_metric_scope(session, config)
        assert detected >= 1
        _assert_metric_anomaly_persisted(session, metric_id)

        metric = session.get(MetricDefinition, metric_id)
        assert metric is not None
        _assert_include_metrics_gate(session, config, metric)


# ── 3c. event_composition: per_distinct_user ───────────────────────────────────


def test_event_composition_per_distinct_user_pipeline_end_to_end(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    numerator_event_id = uuid.uuid4()
    with sync_session_factory() as session:
        project, _ds, config = _seed_pipeline_base(session)
        # Numerator events: flat 30 then a hour-9 spike of 300.
        numerator = {bucket: value for bucket, value in _spike_rows(30.0, 300.0)}
        _seed_event_metric_series(session, config, numerator_event_id, numerator)
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name=f"per-user-{uuid.uuid4().hex[:6]}",
            display_name="Events per user",
            kind=MetricKind.event_composition,
            composition=MetricComposition.per_distinct_user,
            numerator_event_id=numerator_event_id,
            status=MetricStatus.active,
            anomaly_detection_enabled=True,
        )
        session.add(metric)
        session.commit()
        def_id = str(metric.id)
        metric_id = metric.id
        config_id = config.id

    # Denominator is a fresh warehouse count_distinct(user_id) series: 10 users
    # per bucket so the composed ratio is 3.0 baseline / 30.0 spike.
    distinct_rows = [(_BASE + timedelta(hours=hour), 10.0) for hour in range(_SPIKE_HOUR + 1)]
    _patch_collection(
        monkeypatch, sync_session_factory, adapter=_DistinctUserAdapter(distinct_rows)
    )
    metric_collect.collect_metric_definitions.run(def_id)

    with sync_session_factory() as session:
        rows = (
            session.execute(
                select(MetricValue).where(MetricValue.metric_definition_id == metric_id)
            )
            .scalars()
            .all()
        )
        stored = {row.bucket: row.value for row in rows}
        for hour in range(_SPIKE_HOUR):
            assert stored[_BASE + timedelta(hours=hour)] == 3.0
        assert stored[_BASE + timedelta(hours=_SPIKE_HOUR)] == 30.0

    with sync_session_factory() as session:
        config = session.get(ScanConfig, config_id)
        assert config is not None
        detected = _recompute_metric_scope(session, config)
        assert detected >= 1
        _assert_metric_anomaly_persisted(session, metric_id)

        metric = session.get(MetricDefinition, metric_id)
        assert metric is not None
        _assert_include_metrics_gate(session, config, metric)


# ── adapter aggregation wiring (light) ─────────────────────────────────────────


def test_fact_collect_invokes_time_bucketed_aggregate(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """Light wiring check: a fact collect drives the adapter's
    ``get_time_bucketed_aggregate`` with the metric's aggregation. The exhaustive
    per-dialect aggregation coverage lives in ``test_adapter_aggregations.py``.
    """
    with sync_session_factory() as session:
        project, data_source, _config = _seed_pipeline_base(session)
        fact_table = _seed_fact_table(session, project, data_source)
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project.id,
            name=f"fact-wire-{uuid.uuid4().hex[:6]}",
            display_name="Revenue",
            kind=MetricKind.fact,
            composition=MetricComposition.single,
            aggregation=MetricAggregation.sum,
            fact_table_id=fact_table.id,
            config={"measure_column": "amount"},
            interval=ScanInterval.h1,
            status=MetricStatus.active,
            anomaly_detection_enabled=True,
        )
        session.add(metric)
        session.commit()
        def_id = str(metric.id)

    adapter = _RecordingFactAdapter(_spike_rows(10.0, 100.0))
    _patch_collection(monkeypatch, sync_session_factory, adapter=adapter)

    metric_collect.collect_metric_definitions.run(def_id)

    assert adapter.aggregate_calls == [MetricAggregation.sum]
