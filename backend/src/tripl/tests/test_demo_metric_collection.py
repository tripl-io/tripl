"""Worker-integration tests: the REAL metric collectors over the SyntheticAdapter.

The sync worker (``_get_sync_session`` reads ``settings.database_url``) cannot see
the async-test DB and cannot run inside the async provisioning request, so — like
``test_metric_collection.py`` — this suite builds its OWN sync SQLite engine and
monkey-patches ``metric_collect._get_sync_session`` / ``_build_adapter`` /
``_resolve_value_window``. The difference: ``_build_adapter`` returns the REAL
``SyntheticAdapter`` (no fakes, no network), and the metric definitions are the
same demo catalog configs, built through the same authoritative ``*Create``
schemas + ``to_create_values()`` the demo catalog builder uses.

This proves the four collector kinds, per-dimension breakdowns, fact-metric
batching, window idempotency, the unsupported-query error path, and the anomaly
detect path all run against the synthetic dataset the demo carries.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.synthetic import SyntheticAdapter
from tripl.core.analyzers.anomaly_detector import (
    AnomalyDetectionSettings,
    SeriesPoint,
    detect_anomalies,
)
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricStatus,
    ScanInterval,
)
from tripl.models.event_metric import EventMetric
from tripl.models.fact_table import FactTable
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.schemas.fact_table import (
    FactTableColumnSchema,
    FactTableCreate,
    FactTableRowFilter,
)
from tripl.schemas.metric_definition import (
    EventCompositionMetricCreate,
    FactMetricCreate,
    FactOperand,
    SqlConfig,
    SqlMetricCreate,
)
from tripl.worker.tasks.metrics import detect, metric_collect

# Fixed anchor so the in-memory synthetic dataset is byte-for-byte deterministic.
# The adapter floors the anchor to the start of its UTC day and generates 30 days
# of hourly events + daily orders ending on it.
ANCHOR = datetime(2026, 3, 2, tzinfo=UTC)
# A window that comfortably covers the whole synthetic history (used for the
# fact/sql resume-window monkeypatch).
_WINDOW = (ANCHOR - timedelta(days=31), ANCHOR + timedelta(days=1))
_ADAPTER_SEED = 424242

# The demo monitoring builder's Wave-1 anomaly-detector settings.
_ANOMALY_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=3.0,
    min_expected_count=10,
)

_ORDERS_SQL = "SELECT created_at, amount, currency, user_id, country, status FROM orders"
_ACTIVE_SESSIONS_SQL = (
    "SELECT toStartOfDay(event_time) AS ts, count(DISTINCT session_id) AS value FROM events"
)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'demo_metric_collection.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


# ── deterministic synthetic adapters ─────────────────────────────────────────


def _make_adapter() -> SyntheticAdapter:
    """The REAL synthetic adapter, anchored so its data aligns with ``_WINDOW``."""
    return SyntheticAdapter(seed=_ADAPTER_SEED, anchor=ANCHOR, history_days=30)


class _CountingSyntheticAdapter(SyntheticAdapter):
    """A real synthetic adapter that counts its shared multi-aggregate scans.

    Used to prove the batched collector runs ONE multi-aggregate scan for two
    fact metrics that share a fact table (rather than one scan per metric).
    """

    def __init__(self) -> None:
        super().__init__(seed=_ADAPTER_SEED, anchor=ANCHOR, history_days=30)
        self.multi_aggregate_calls = 0

    def get_time_bucketed_multi_aggregate(self, *args: object, **kwargs: object):  # type: ignore[override]
        self.multi_aggregate_calls += 1
        return super().get_time_bucketed_multi_aggregate(*args, **kwargs)  # type: ignore[arg-type]


def _patch_collectors(
    monkeypatch: MonkeyPatch,
    factory: sessionmaker[Session],
    adapter: SyntheticAdapter,
) -> None:
    monkeypatch.setattr(metric_collect, "_get_sync_session", factory)
    monkeypatch.setattr(metric_collect, "_build_adapter", lambda ds: adapter)
    monkeypatch.setattr(metric_collect, "_resolve_value_window", lambda *a, **k: _WINDOW)


# ── demo-parity seeding (same *Create schemas as the catalog builder) ─────────


@dataclass(frozen=True)
class _Seeded:
    project_id: uuid.UUID
    data_source_id: uuid.UUID
    fact_table_id: uuid.UUID
    scan_config_id: uuid.UUID
    numerator_event_id: uuid.UUID
    denominator_event_id: uuid.UUID
    metric_ids: dict[str, uuid.UUID]


def _seed_demo(session: Session) -> _Seeded:
    project = Project(
        id=uuid.uuid4(),
        name="Demo Metrics",
        slug=f"demo-{uuid.uuid4().hex[:8]}",
        description="",
    )
    # A demo-parity synthetic data source: db_type='synthetic', scoped to the
    # project. build_adapter would return the SyntheticAdapter for it in prod.
    data_source = DataSource(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"Synthetic DS {uuid.uuid4().hex[:8]}",
        db_type="synthetic",
        host="synthetic",
        port=0,
        database_name="synthetic",
        username="",
        password_encrypted="",
    )
    session.add_all([project, data_source])
    session.commit()

    # The source scan grid the event_composition metric's values are keyed to.
    scan_config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Demo scan",
        base_query="SELECT * FROM events",
        time_column="event_time",
        cardinality_threshold=100,
        interval="1h",
    )
    session.add(scan_config)
    session.commit()

    fact_table = _seed_orders_fact_table(session, project.id, data_source.id)

    numerator_event_id = uuid.uuid4()
    denominator_event_id = uuid.uuid4()
    _seed_conversion_event_metrics(session, scan_config, numerator_event_id, denominator_event_id)

    metric_ids = _seed_metric_definitions(
        session,
        project_id=project.id,
        data_source_id=data_source.id,
        fact_table_id=fact_table.id,
        numerator_event_id=numerator_event_id,
        denominator_event_id=denominator_event_id,
    )
    return _Seeded(
        project_id=project.id,
        data_source_id=data_source.id,
        fact_table_id=fact_table.id,
        scan_config_id=scan_config.id,
        numerator_event_id=numerator_event_id,
        denominator_event_id=denominator_event_id,
        metric_ids=metric_ids,
    )


def _seed_orders_fact_table(
    session: Session, project_id: uuid.UUID, data_source_id: uuid.UUID
) -> FactTable:
    create = FactTableCreate(
        name="orders",
        display_name="Orders",
        description="One row per store order, used by the revenue fact metrics.",
        color="#0ea5e9",
        data_source_id=data_source_id,
        sql=_ORDERS_SQL,
        timestamp_column="created_at",
        columns=[
            FactTableColumnSchema(name="created_at", type="timestamp"),
            FactTableColumnSchema(name="amount", type="number"),
            FactTableColumnSchema(name="currency", type="string"),
            FactTableColumnSchema(name="user_id", type="string"),
            FactTableColumnSchema(name="country", type="string"),
            FactTableColumnSchema(name="status", type="string"),
        ],
        identifier_columns=["user_id"],
        row_filters=[FactTableRowFilter(name="completed", sql="status = 'completed'")],
    )
    fact_table = FactTable(project_id=project_id, order=0, **create.to_create_values())
    session.add(fact_table)
    session.commit()
    return fact_table


def _seed_conversion_event_metrics(
    session: Session,
    scan_config: ScanConfig,
    numerator_event_id: uuid.UUID,
    denominator_event_id: uuid.UUID,
) -> None:
    """Seed numerator/denominator event counts so the ratio reads as a fraction."""
    base = ANCHOR - timedelta(days=2)
    for hour in range(24):
        bucket = base + timedelta(hours=hour)
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                event_id=numerator_event_id,
                event_type_id=None,
                bucket=bucket,
                count=8 + hour % 5,
            )
        )
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                event_id=denominator_event_id,
                event_type_id=None,
                bucket=bucket,
                count=100 + hour * 3,
            )
        )
    session.commit()


def _seed_metric_definitions(
    session: Session,
    *,
    project_id: uuid.UUID,
    data_source_id: uuid.UUID,
    fact_table_id: uuid.UUID,
    numerator_event_id: uuid.UUID,
    denominator_event_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """The four demo catalog metrics, built through the same *Create schemas."""
    sql_metric = SqlMetricCreate(
        name="active_sessions",
        display_name="Active Sessions",
        description="Distinct sessions seen per day.",
        color="#6366f1",
        order=0,
        unit="sessions",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        config=SqlConfig(metric_sql=_ACTIVE_SESSIONS_SQL, time_column="ts"),
        data_source_id=data_source_id,
        interval=ScanInterval.d1,
    )
    conversion_metric = EventCompositionMetricCreate(
        name="purchase_conversion",
        display_name="Purchase conversion",
        description="Completed purchases per Home Screen View.",
        color="#22c55e",
        order=1,
        unit="%",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        composition=MetricComposition.ratio,
        numerator_event_id=numerator_event_id,
        denominator_event_id=denominator_event_id,
    )
    revenue_metric = FactMetricCreate(
        name="revenue_completed",
        display_name="Revenue (completed)",
        description="Sum of completed-order amounts per day.",
        color="#f59e0b",
        order=2,
        unit="$",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        composition=MetricComposition.single,
        interval=ScanInterval.d1,
        fact_table_id=fact_table_id,
        aggregation=MetricAggregation.sum,
        measure_column="amount",
        row_filters=["completed"],
    )
    aov_metric = FactMetricCreate(
        name="average_order_value",
        display_name="Average order value",
        description="Completed-order revenue divided by completed-order count.",
        color="#a855f7",
        order=3,
        unit="$",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        composition=MetricComposition.ratio,
        interval=ScanInterval.d1,
        numerator=FactOperand(
            fact_table_id=fact_table_id,
            aggregation=MetricAggregation.sum,
            measure_column="amount",
        ),
        denominator=FactOperand(
            fact_table_id=fact_table_id,
            aggregation=MetricAggregation.count,
        ),
    )

    metric_ids: dict[str, uuid.UUID] = {}
    for create in (sql_metric, conversion_metric, revenue_metric, aov_metric):
        definition = MetricDefinition(project_id=project_id, **create.to_create_values())
        session.add(definition)
        session.flush()
        metric_ids[create.name] = definition.id
    session.commit()
    return metric_ids


def _metric_values(session: Session, metric_id: uuid.UUID) -> list[MetricValue]:
    return list(
        session.execute(select(MetricValue).where(MetricValue.metric_definition_id == metric_id))
        .scalars()
        .all()
    )


# ── each collector kind runs over the synthetic source ────────────────────────


def test_collect_each_kind_over_synthetic_source(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """All four demo metric kinds collect real values from the SyntheticAdapter."""
    with sync_session_factory() as session:
        seeded = _seed_demo(session)

    _patch_collectors(monkeypatch, sync_session_factory, _make_adapter())

    for name in (
        "active_sessions",
        "revenue_completed",
        "average_order_value",
        "purchase_conversion",
    ):
        result = metric_collect.collect_metric_definitions.run(
            str(seeded.metric_ids[name]), force=True
        )
        assert result.get("skipped") is None, name

    with sync_session_factory() as session:
        # sql: distinct sessions per day -- positive integer-ish counts.
        sessions_rows = _metric_values(session, seeded.metric_ids["active_sessions"])
        assert sessions_rows, "sql metric produced no values"
        assert all(row.value > 0 for row in sessions_rows)
        assert all(row.scan_config_id is None for row in sessions_rows)

        # fact single: daily order revenue -- positive dollar sums.
        revenue_rows = _metric_values(session, seeded.metric_ids["revenue_completed"])
        assert revenue_rows, "fact single produced no values"
        assert all(row.value > 0 for row in revenue_rows)
        assert all(row.scan_config_id is None for row in revenue_rows)

        # fact ratio: average order value -- within the synthetic amount range.
        aov_rows = _metric_values(session, seeded.metric_ids["average_order_value"])
        assert aov_rows, "fact ratio produced no values"
        assert all(5.0 <= row.value <= 210.0 for row in aov_rows), [row.value for row in aov_rows]

        # event_composition ratio: a fraction, keyed by the source scan grid.
        conversion_rows = _metric_values(session, seeded.metric_ids["purchase_conversion"])
        assert conversion_rows, "event_composition produced no values"
        assert all(0.0 < row.value < 1.0 for row in conversion_rows)
        assert all(row.scan_config_id == seeded.scan_config_id for row in conversion_rows)

        # Every kind stamped a successful collection lifecycle.
        for name in seeded.metric_ids:
            definition = session.get(MetricDefinition, seeded.metric_ids[name])
            assert definition is not None
            assert definition.last_collection_status == "success", name
            assert definition.last_collected_at is not None, name


# ── per-dimension breakdown from the real collector ───────────────────────────


def test_fact_metric_breakdown_over_synthetic_source(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A breakdown-configured fact metric writes real per-country breakdown rows."""
    with sync_session_factory() as session:
        seeded = _seed_demo(session)
        # A revenue-by-country fact metric (same shape as revenue, plus a
        # country breakdown) built via the demo's FactMetricCreate schema.
        create = FactMetricCreate(
            name="revenue_by_country",
            display_name="Revenue by country",
            description="Daily order revenue, broken down by country.",
            color="#0ea5e9",
            order=4,
            unit="$",
            status=MetricStatus.active,
            composition=MetricComposition.single,
            interval=ScanInterval.d1,
            fact_table_id=seeded.fact_table_id,
            aggregation=MetricAggregation.sum,
            measure_column="amount",
        )
        definition = MetricDefinition(project_id=seeded.project_id, **create.to_create_values())
        definition.breakdown_columns = ["country"]
        definition.breakdown_values_limit = 3
        session.add(definition)
        session.commit()
        breakdown_metric_id = definition.id

    _patch_collectors(monkeypatch, sync_session_factory, _make_adapter())

    result = metric_collect.collect_metric_definitions.run(str(breakdown_metric_id), force=True)
    assert result["values"] > 0
    assert result["breakdown_values"] > 0

    with sync_session_factory() as session:
        rows = list(
            session.execute(
                select(MetricValueBreakdown).where(
                    MetricValueBreakdown.metric_definition_id == breakdown_metric_id
                )
            )
            .scalars()
            .all()
        )
        assert rows, "no breakdown rows written"
        assert all(row.breakdown_column == "country" for row in rows)
        assert all(row.value > 0 for row in rows)
        # The synthetic dataset's countries (top-N + an 'Other' rollup).
        values = {row.breakdown_value for row in rows}
        assert values & {"US", "GB", "DE", "FR", "CA", "JP", "Other"}


# ── fact-metric batching shares one scan over the synthetic source ────────────


def test_fact_metric_batching_shares_one_synthetic_scan(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """collect_fact_metrics_batch groups compatible fact metrics into ONE scan."""
    with sync_session_factory() as session:
        seeded = _seed_demo(session)

    adapter = _CountingSyntheticAdapter()
    _patch_collectors(monkeypatch, sync_session_factory, adapter)

    result = metric_collect.collect_fact_metrics_batch.run(
        [
            str(seeded.metric_ids["revenue_completed"]),
            str(seeded.metric_ids["average_order_value"]),
        ],
        force=True,
    )

    assert result["metrics"] == 2
    assert result["collected"] == 2
    assert result["errors"] == 0
    assert result["values"] > 0
    # Both fact metrics share the ``orders`` fact table + interval, so the batch
    # runs exactly ONE multi-aggregate scan (the completed-filter spec, the AOV
    # sum spec and the AOV count spec all ride one query).
    assert adapter.multi_aggregate_calls == 1

    with sync_session_factory() as session:
        for name in ("revenue_completed", "average_order_value"):
            metric_id = seeded.metric_ids[name]
            rows = _metric_values(session, metric_id)
            assert rows, f"{name} produced no batched values"
            assert all(row.scan_config_id is None for row in rows)
            definition = session.get(MetricDefinition, metric_id)
            assert definition is not None
            assert definition.last_collection_status == "success", name


def test_batched_collection_is_idempotent_on_rerun(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """Collecting the same window twice replaces rows, never duplicates them."""
    with sync_session_factory() as session:
        seeded = _seed_demo(session)

    metric_ids = [
        str(seeded.metric_ids["revenue_completed"]),
        str(seeded.metric_ids["average_order_value"]),
    ]

    _patch_collectors(monkeypatch, sync_session_factory, _make_adapter())
    metric_collect.collect_fact_metrics_batch.run(metric_ids, force=True)
    with sync_session_factory() as session:
        first_count = len(_metric_values(session, seeded.metric_ids["revenue_completed"]))
        first_values = {
            (row.bucket, row.value)
            for row in _metric_values(session, seeded.metric_ids["revenue_completed"])
        }

    # Re-run the SAME window with a fresh adapter (same seed/anchor -> same data).
    _patch_collectors(monkeypatch, sync_session_factory, _make_adapter())
    metric_collect.collect_fact_metrics_batch.run(metric_ids, force=True)
    with sync_session_factory() as session:
        second_values = {
            (row.bucket, row.value)
            for row in _metric_values(session, seeded.metric_ids["revenue_completed"])
        }

    assert first_count > 0
    assert second_values == first_values, "window-delete-then-upsert must not duplicate rows"


# ── unsupported SQL is an honest error, never a fabricated success ────────────


def test_unsupported_sql_metric_records_error(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """A SQL shape the SyntheticAdapter can't compute records an error, no values."""
    with sync_session_factory() as session:
        seeded = _seed_demo(session)
        # An aggregate the synthetic adapter does not recognise (only the seeded
        # distinct-sessions shape + plain scans are supported): it raises a
        # capability error rather than fabricating a series.
        create = SqlMetricCreate(
            name="unsupported_sql",
            display_name="Unsupported SQL",
            description="A daily amount sum the synthetic adapter cannot compute.",
            color="#ef4444",
            order=5,
            unit="$",
            status=MetricStatus.active,
            config=SqlConfig(
                metric_sql=(
                    "SELECT toStartOfDay(event_time) AS ts, sum(amount) AS value FROM events"
                ),
                time_column="ts",
            ),
            data_source_id=seeded.data_source_id,
            interval=ScanInterval.d1,
        )
        definition = MetricDefinition(project_id=seeded.project_id, **create.to_create_values())
        session.add(definition)
        session.commit()
        unsupported_id = definition.id

    _patch_collectors(monkeypatch, sync_session_factory, _make_adapter())

    # The collector stamps the error and re-raises (the scheduler treats a raised
    # task as a failure); the persisted status/error is what the UI surfaces.
    with pytest.raises(Exception):  # noqa: B017, PT011 - SyntheticCapabilityError re-raised
        metric_collect.collect_metric_definitions.run(str(unsupported_id), force=True)

    with sync_session_factory() as session:
        definition = session.get(MetricDefinition, unsupported_id)
        assert definition is not None
        assert definition.last_collection_status == "error"
        assert definition.last_collection_error, "expected a user-facing error message"
        # No fabricated values were written.
        assert _metric_values(session, unsupported_id) == []


# ── the anomaly detect path runs over the real collected series ───────────────


def test_metric_anomaly_detect_path_over_collected_series(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: MonkeyPatch,
) -> None:
    """The real detector runs over the collected series and flags a clear spike."""
    with sync_session_factory() as session:
        seeded = _seed_demo(session)

    _patch_collectors(monkeypatch, sync_session_factory, _make_adapter())
    metric_collect.collect_metric_definitions.run(
        str(seeded.metric_ids["active_sessions"]), force=True
    )

    with sync_session_factory() as session:
        points = detect._load_metric_value_points(
            session,
            metric_definition_id=seeded.metric_ids["active_sessions"],
            history_from=ANCHOR - timedelta(days=40),
            time_to=ANCHOR + timedelta(days=1),
        )

    assert len(points) >= 10, "expected a real multi-day collected series"
    spike_bucket = points[-1].bucket

    # The detect path runs over the REAL collected series (no injected spike).
    baseline = detect_anomalies(
        points,
        interval=timedelta(days=1),
        evaluation_start=spike_bucket,
        evaluation_end=spike_bucket + timedelta(days=1),
        settings=_ANOMALY_SETTINGS,
        fill_gaps=True,
    ).anomalies
    assert isinstance(baseline, list)

    # An unmistakable spike on the newest bucket is flagged by the real detector,
    # proving a metric-scope anomaly is produced over the collected-shape series.
    peak = max(point.count for point in points)
    spiked = [SeriesPoint(bucket=point.bucket, count=point.count) for point in points[:-1]]
    spiked.append(SeriesPoint(bucket=spike_bucket, count=peak * 50))
    detected = detect_anomalies(
        spiked,
        interval=timedelta(days=1),
        evaluation_start=spike_bucket,
        evaluation_end=spike_bucket + timedelta(days=1),
        settings=_ANOMALY_SETTINGS,
        fill_gaps=True,
    ).anomalies
    assert any(anomaly.bucket == spike_bucket for anomaly in detected), "spike not flagged"


# ── ongoing volume reconciles with the seeded baseline ─────


def test_synthetic_event_defs_cover_every_seeded_event_spec() -> None:
    """The adapter's roster MUST mirror ``event_specs`` EXHAUSTIVELY, in both
    directions.

    ``event_specs`` is the demo's single source of truth for the authored catalog
    and its volumes. Metrics collection deletes the whole collection window before
    re-inserting whatever the warehouse returned, so any seeded event the
    synthetic table does NOT emit loses its recent buckets and the detector reads
    it as "dropped to zero" — which is exactly what an untouched demo did within
    an hour of creation while ``_EVENT_DEFS`` listed only 7 of the 18 seeded
    events (.71). core/ cannot import services/, so the values
    are duplicated in ``_EVENT_DEFS``; this test pins the two together.
    """
    from tripl.core.adapters.synthetic import _EVENT_DEFS
    from tripl.services.demo.builders.plan import event_specs

    specs = event_specs(datetime(2026, 1, 1, tzinfo=UTC))
    spec_by_name = {spec.name: spec for spec in specs}
    synthetic_by_name = {event_def.event_name: event_def for event_def in _EVENT_DEFS}

    assert synthetic_by_name.keys() == spec_by_name.keys(), (
        "every seeded event must be emitted by the synthetic warehouse and vice versa; "
        f"missing from the warehouse: {sorted(spec_by_name.keys() - synthetic_by_name.keys())}, "
        f"unknown to the plan: {sorted(synthetic_by_name.keys() - spec_by_name.keys())}"
    )
    for name, event_def in synthetic_by_name.items():
        spec = spec_by_name[name]
        assert event_def.ongoing_base == spec.base, (name, event_def.ongoing_base, spec.base)
        assert event_def.event_type == spec.event_type, (
            name,
            event_def.event_type,
            spec.event_type,
        )


def test_synthetic_event_defs_match_the_plans_documented_field_values() -> None:
    """Synthetic column values agree with the field values the plan documents.

    A rescan records what the warehouse holds, so a synthetic ``screen_name`` /
    ``button_id`` that disagrees with the authored field value would rewrite the
    curated catalog. ``${var}`` template values are skipped:
    they are the templating showcase and have no single literal.
    """
    from tripl.core.adapters.synthetic import _EVENT_DEFS
    from tripl.services.demo.builders.plan import event_specs

    spec_by_name = {spec.name: spec for spec in event_specs(datetime(2026, 1, 1, tzinfo=UTC))}
    for event_def in _EVENT_DEFS:
        documented = {
            key.split(".", 1)[1]: value
            for key, value in spec_by_name[event_def.event_name].field_values
            if not value.startswith("${")
        }
        for column in ("screen_name", "button_id"):
            if column in documented:
                assert getattr(event_def, column) == documented[column], (
                    event_def.event_name,
                    column,
                )
        if "amount" in documented:
            assert event_def.amount == float(documented["amount"]), event_def.event_name
        if "currency" in documented:
            assert event_def.currency == documented["currency"], event_def.event_name


def test_synthetic_dataset_stays_within_row_budget() -> None:
    """The full-history dataset fits the cap, so the NEWEST hours are never clipped.

    ``_generate_events`` fills oldest-first; a cap that bit would truncate exactly
    the ongoing window a live scan reads. The ongoing window is generated first
    and never truncated, and this pins the total (with the older tail) under the
    budget across every hour-of-week phase.
    """
    from tripl.core.adapters import synthetic as synth

    # A full Sunday..Saturday week of anchors exercises every seasonal phase, so
    # the peak-volume anchor is covered.
    for day in range(1, 8):
        for hour in (2, 8, 14, 20):
            anchor = datetime(2026, 3, day, hour, tzinfo=UTC)
            rows = synth._generate_events(
                synth.DEFAULT_SEED,
                anchor,
                synth.SYNTHETIC_HISTORY_DAYS,
                synth.SYNTHETIC_MAX_ROWS,
            )
            assert len(rows) < synth.SYNTHETIC_MAX_ROWS, (anchor, len(rows))
            newest = anchor - timedelta(hours=1)
            emitted = {row["event_name"] for row in rows if row["event_time"] >= newest}
            # Retired identities (the planted dead event) stay on
            # the roster but emit nothing.
            live_names = {ev.event_name for ev in synth._EVENT_DEFS if not ev.retired}
            assert emitted == live_names, (anchor, sorted(emitted))


def test_synthetic_ongoing_counts_stay_within_detector_drop_band() -> None:
    """Every ongoing per-event count lands inside the anomaly detector's per-bucket
    band of the seeded ``noise.hourly_volume`` baseline, across all hour/weekday
    phases, so scanning an idle demo does not surface a drop.

    The band is the flag condition |actual - expected| < sigma * effective_stddev
    with sigma=3 and the phase baseline's Poisson-aware stddev floor
    (scale ~= 0 for the phase-consistent seeded series).
    """
    from math import sqrt

    from tripl.core.adapters import synthetic as synth
    from tripl.services.demo import noise
    from tripl.services.demo.scenario import DEMO_SEED

    total_buckets = noise.DEMO_HISTORY_DAYS * 24
    bases = {ev.event_name: ev.ongoing_base for ev in synth._EVENT_DEFS}
    # A synthetic seed distinct from DEMO_SEED (as registry derives per source),
    # so the test also proves the shape is robust to a mismatched adapter seed.
    adapter_seed = 424242
    # 2026-03-01..07 is a full Sunday..Saturday week, so every weekday/hour phase
    # of the ongoing window is exercised (the trough phases are the tightest).
    anchors = [
        datetime(2026, 3, day, hour, tzinfo=UTC) for day in range(1, 8) for hour in range(24)
    ]
    for anchor in anchors:
        for hours_back in range(1, synth.SYNTHETIC_ONGOING_HOURS + 1):
            bucket = anchor - timedelta(hours=hours_back)
            idx = total_buckets - hours_back
            for name, base in bases.items():
                noise_seed = noise.derive_seed(DEMO_SEED, name) % 997
                expected = noise.hourly_volume(base, bucket, idx, noise_seed, total_buckets)
                actual = synth._ongoing_hourly_count(adapter_seed, base, name, bucket)
                effective_stddev = max(0.05 * expected, sqrt(expected), 1.0)
                z_score = abs(actual - expected) / effective_stddev
                assert z_score < 3.0, (name, bucket.hour, actual, expected, z_score)
