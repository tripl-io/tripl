"""Gate 4: the metrics PIPELINE, executed end to end against real warehouses.

The sibling gates prove one adapter CALL at a time. This one proves the thing the
product actually runs: a scan that generates events from a real warehouse, a replay
that collects event metrics from it, catalog metrics (fact single / ratio / batched,
plus the event compositions) built on top, and the anomaly pass over the result.

Why it exists
-------------
``tests/test_metrics_pipeline_e2e.py`` drives these same worker entrypoints against
FAKE adapters — canned rows keyed by nothing, returned from a class that never parses
the SQL it is handed. A fake adapter cannot disagree with the code that calls it, so
that test can only prove the pipeline's *plumbing*. Every warehouse defect this epic
shipped (``TIMESTAMP_BIN``, ``GROUP BY <ARRAY>``, ``0/0``, ``date_trunc``) was green
under exactly that kind of test. Here the SQL runs, and the numbers come back from
PostgreSQL and ClickHouse.

What is asserted, and against what
----------------------------------
Never "it ran". Every series is compared against a reference computed in
``dataset.py`` from ``core.bucketing.floor_to_bucket`` — the product's own bucketing
function — and PostgreSQL and ClickHouse are then compared against EACH OTHER
(``test_postgres_and_clickhouse_agree``). Two engines and one pure-Python reference
have to say the same thing, so a shared warehouse-side mistake cannot pass.

The application database is a real PostgreSQL, not SQLite
---------------------------------------------------------
See the ``app_db`` fixture. Short version: SQLite hands back naive datetimes and
takes the ``sqlite_insert`` UPSERT branch, so a SQLite-hosted pipeline test proves
neither the bucket alignment nor the UPSERT that production runs.

The emulator path in this module is ANALYSIS-ONLY
--------------------------------------------------
``test_bigquery_pipeline_sql_analyzes`` drives the SAME worker code paths with a
BigQuery data source and a client that CAPTURES the generated GoogleSQL, then asserts
the emulator's real ZetaSQL analyzer accepts every statement. It asserts NOTHING about
values, and must never be "strengthened" to. The emulator's computed results are not
BigQuery's — demonstrated: ``DATETIME_TRUNC(DATETIME '2026-04-08 13:00:00',
WEEK(MONDAY))`` returns ``2026-04-06T13:00:00`` there, keeping the time component,
where real BigQuery returns ``2026-04-06T00:00:00``. A value assertion against the
emulator would either fail on correct SQL or — far worse — certify a bucket contract
that BigQuery does not honour. Values are proven on the two engines that execute.
The credentialed gates execute adapter values and this same full worker pipeline in
``test_bigquery_value_conformance.py`` and
``test_bigquery_pipeline_value_conformance.py``. They run only for trusted release
tags; this always-on emulator path remains the credential-free PR guard for the full
generated-SQL surface.

A note on the fixture's NULL measures
-------------------------------------
Every ``view`` row carries ``amount = NULL``, so SUM/AVG must skip it while COUNT(*)
must not. An all-NULL breakdown GROUP is now safe to construct: both the per-metric
pass (``_collect_fact_breakdown_rows``) and the batched one (``_assemble_single_metric``)
skip a NULL aggregate cell and record the group as ABSENT, where the per-metric pass
used to do ``float(row[-1])`` unguarded and raise ``TypeError`` inside the collector.
The fixture may therefore grow one; it no longer has to be kept out by hand.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

import pytest
from google.cloud import bigquery
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.adapters.bigquery import BigQueryAdapter
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    FieldDefinitionType,
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
    ScanInterval,
)
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.fact_table import FactTable
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.tests.conformance.bigquery_values import PIPELINE_BASE
from tripl.tests.conformance.conftest import PipelineWarehouse
from tripl.tests.conformance.dataset import (
    PIPELINE_EVENT_NAMES,
    PIPELINE_FROM,
    PIPELINE_TO,
    pipeline_amount_sums,
    pipeline_buckets,
    pipeline_count_ratio,
    pipeline_distinct_users,
    pipeline_event_composition_ratio,
    pipeline_event_counts,
    pipeline_events_per_distinct_user,
    pipeline_platform_sums,
    pipeline_row_counts,
    pipeline_spike_bucket,
)
from tripl.worker.tasks import scan as scan_task
from tripl.worker.tasks.metrics import detect as metrics_detect
from tripl.worker.tasks.metrics import metric_collect
from tripl.worker.tasks.metrics import tasks as metrics_tasks

#: The replay is chunked so ``_iter_window_chunks`` actually splits the window and the
#: per-chunk delete/UPSERT path runs more than once against a live warehouse.
REPLAY_CHUNK = "6h"

#: Metric keys inside a ``PipelineRun``. Named so an assertion reads as the thing it
#: is checking rather than as an index.
FACT_SUM = "fact_sum"
FACT_COUNT = "fact_count"
FACT_DISTINCT = "fact_distinct"
FACT_RATIO = "fact_ratio"
COMP_SINGLE = "comp_single"
COMP_RATIO = "comp_ratio"
COMP_PER_USER = "comp_per_user"

#: The fact metrics the batched collector must reproduce exactly.
BATCHED_METRICS = (FACT_SUM, FACT_COUNT, FACT_DISTINCT, FACT_RATIO)


# ── the run ───────────────────────────────────────────────────────────────────


@dataclass
class PipelineRun:
    """Everything one warehouse's pipeline produced. Built once, asserted many times."""

    db_type: str
    scan_summary: dict[str, object]
    replay_summary: dict[str, object]
    event_names: set[str]
    #: ``{event identity: {bucket: count}}`` read back out of ``event_metrics``.
    event_series: dict[str, dict[datetime, float]]
    #: The same, after a second identical replay — idempotency, not a fresh number.
    event_series_rerun: dict[str, dict[datetime, float]]
    #: ``{metric key: {bucket: value}}`` from the PER-METRIC collector.
    metric_series: dict[str, dict[datetime, float]]
    #: The same fact metrics, recollected by the BATCHED collector.
    batched_series: dict[str, dict[datetime, float]]
    batch_totals: dict[str, int]
    #: ``{(bucket, platform): value}`` from ``metric_value_breakdowns``.
    breakdowns: dict[tuple[datetime, str], float]
    #: ``(scope_type, scope_name, direction, bucket)`` for every detected anomaly.
    anomalies: set[tuple[str, str, str, datetime]] = field(default_factory=set)


@dataclass(frozen=True)
class _Seeded:
    project_id: uuid.UUID
    scan_config_id: uuid.UUID
    fact_table_id: uuid.UUID


def _seed_catalog(session: Session, warehouse: PipelineWarehouse) -> _Seeded:
    """Project + data source + scan config + fact table, bound to one warehouse.

    ``db_type`` is the real one, so the dialect every filter/condition is compiled for
    is the dialect the SQL is then executed on.
    """
    project = Project(
        id=uuid.uuid4(),
        name=f"Pipeline {warehouse.db_type}",
        slug=f"pipeline-{uuid.uuid4().hex[:8]}",
        description="",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"conformance-{uuid.uuid4().hex[:8]}",
        db_type=warehouse.db_type,
        host="localhost",
        port=0,
        database_name="conformance",
        username="conformance",
        password_encrypted="",
    )
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name="pipeline",
        display_name="Pipeline",
        description="",
    )
    # Only ``event_name`` gets a field definition, so it is the only column that
    # contributes to the event identity. ``user_id`` is projected by the scan query
    # (per_distinct_user counts it) but has no field definition, so generation skips
    # it — asserted in test_scan_generates_events_from_the_warehouse.
    field_definition = FieldDefinition(
        id=uuid.uuid4(),
        event_type_id=event_type.id,
        name="event_name",
        display_name="Event name",
        field_type=FieldDefinitionType.string.value,
        is_required=True,
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        event_type_id=event_type.id,
        name="Pipeline scan",
        base_query=warehouse.events_query,
        time_column="ts",
        cardinality_threshold=100,
        interval=ScanInterval.h1.value,
        replay_chunk_interval=REPLAY_CHUNK,
    )
    settings = ProjectAnomalySettings(
        project_id=project.id,
        anomaly_detection_enabled=True,
        # Event + metric scopes only, so the assertions are about the two scopes this
        # gate is for. Thresholds are pinned (not product defaults) so the small
        # fixture stays eligible: min_expected_count=10 is why the baseline is 12
        # and not 2, and sigma_threshold=3.0 keeps the crafted deviations firing.
        sigma_threshold=3.0,
        min_expected_count=10,
        detect_project_total=False,
        detect_event_types=False,
        detect_events=True,
        detect_metrics=True,
    )
    fact_table = FactTable(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"facts-{uuid.uuid4().hex[:6]}",
        display_name="Pipeline facts",
        data_source_id=data_source.id,
        sql=warehouse.facts_query,
        timestamp_column="ts",
        columns=[
            {"name": "ts", "type": "timestamp"},
            {"name": "event_name", "type": "string"},
            {"name": "user_id", "type": "string"},
            {"name": "amount", "type": "number"},
            {"name": "platform", "type": "string"},
        ],
        identifier_columns=["user_id"],
        row_filters=[],
    )
    session.add_all([project, data_source, event_type, field_definition, config, settings])
    session.commit()
    session.add(fact_table)
    session.commit()
    return _Seeded(
        project_id=project.id,
        scan_config_id=config.id,
        fact_table_id=fact_table.id,
    )


def _fact_metric(
    seeded: _Seeded,
    *,
    name: str,
    aggregation: MetricAggregation,
    config: dict[str, object],
    breakdown_columns: list[str] | None = None,
) -> MetricDefinition:
    return MetricDefinition(
        id=uuid.uuid4(),
        project_id=seeded.project_id,
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        display_name=name,
        kind=MetricKind.fact,
        composition=MetricComposition.single,
        aggregation=aggregation,
        fact_table_id=seeded.fact_table_id,
        config=config,
        interval=ScanInterval.h1,
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        breakdown_columns=breakdown_columns or [],
    )


def _ratio_fact_metric(seeded: _Seeded) -> MetricDefinition:
    """count(*) / count(*) WHERE event_name = 'buy'.

    The denominator is ZERO in the first bucket (the fixture has no buy row there), so
    this metric is the divide-by-zero contract: that bucket must come back ABSENT.
    The condition is a STRUCTURED one, so it is compiled for the source's dialect —
    the code path a free-text filter would bypass.
    """
    return MetricDefinition(
        id=uuid.uuid4(),
        project_id=seeded.project_id,
        name=f"fact-ratio-{uuid.uuid4().hex[:6]}",
        display_name="Rows per purchase",
        kind=MetricKind.fact,
        composition=MetricComposition.ratio,
        fact_table_id=seeded.fact_table_id,
        config={
            "numerator": {
                "fact_table_id": str(seeded.fact_table_id),
                "aggregation": MetricAggregation.count.value,
            },
            "denominator": {
                "fact_table_id": str(seeded.fact_table_id),
                "aggregation": MetricAggregation.count.value,
                "conditions": [
                    {"column": "event_name", "operator": "eq", "value": "buy"},
                ],
            },
        },
        interval=ScanInterval.h1,
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
    )


def _composition_metric(
    seeded: _Seeded,
    *,
    name: str,
    composition: MetricComposition,
    numerator_event_id: uuid.UUID,
    denominator_event_id: uuid.UUID | None = None,
) -> MetricDefinition:
    return MetricDefinition(
        id=uuid.uuid4(),
        project_id=seeded.project_id,
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        display_name=name,
        kind=MetricKind.event_composition,
        composition=composition,
        numerator_event_id=numerator_event_id,
        denominator_event_id=denominator_event_id,
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
    )


def _event_series(session: Session, scan_config_id: uuid.UUID) -> dict[str, dict[datetime, float]]:
    """``event_metrics`` read back as ``{event identity: {bucket: count}}``."""
    rows = session.execute(
        select(Event.name, EventMetric.bucket, EventMetric.count)
        .join(Event, Event.id == EventMetric.event_id)
        .where(EventMetric.scan_config_id == scan_config_id)
    ).all()
    series: dict[str, dict[datetime, float]] = {}
    for name, bucket, count in rows:
        series.setdefault(name, {})[bucket] = float(count)
    return series


def _value_series(session: Session, definition_id: uuid.UUID) -> dict[datetime, float]:
    rows = (
        session.execute(
            select(MetricValue).where(MetricValue.metric_definition_id == definition_id)
        )
        .scalars()
        .all()
    )
    return {row.bucket: row.value for row in rows}


def _breakdown_series(
    session: Session, definition_id: uuid.UUID
) -> dict[tuple[datetime, str], float]:
    rows = (
        session.execute(
            select(MetricValueBreakdown).where(
                MetricValueBreakdown.metric_definition_id == definition_id
            )
        )
        .scalars()
        .all()
    )
    return {(row.bucket, row.breakdown_value): row.value for row in rows}


def _anomalies(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    metric_refs: list[str],
    names: dict[str, str],
) -> set[tuple[str, str, str, datetime]]:
    """Detected anomalies as ``(scope_type, human name, direction, bucket)``.

    Two different keys, because the model has two: an EVENT-scope row belongs to a
    scan config, while a METRIC-scope row is project-global and carries a NULL
    ``scan_config_id`` (it is keyed by ``scope_ref``, the metric definition id). The
    app database is shared across this file's runs, so both halves are pinned to THIS
    run's ids — not to "everything in the table".

    ``names`` maps the raw uuid ``scope_ref`` back to something an assertion can be
    read against: an event identity, or one of this module's metric keys.
    """
    rows = (
        session.execute(
            select(MetricAnomaly).where(
                or_(
                    MetricAnomaly.scan_config_id == scan_config_id,
                    MetricAnomaly.scope_ref.in_(metric_refs),
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        (row.scope_type, names.get(row.scope_ref, row.scope_ref), row.direction, row.bucket)
        for row in rows
    }


def _run_pipeline(
    session_factory: sessionmaker[Session], warehouse: PipelineWarehouse
) -> PipelineRun:
    """Drive the REAL worker tasks against one real warehouse, start to finish.

    Nothing here re-implements what the worker does: ``run_scan``,
    ``collect_metrics``, ``collect_metric_definitions``, ``collect_fact_metrics_batch``
    and ``_recalculate_metric_anomalies`` are the shipped entrypoints. The only things
    patched are the two seams every worker test patches — the session factory and the
    adapter builder — so the tasks talk to THIS database and THIS warehouse.
    """
    window = (PIPELINE_FROM.isoformat(), PIPELINE_TO.isoformat())

    with pytest.MonkeyPatch.context() as patch:
        for module in (scan_task, metrics_tasks, metric_collect):
            patch.setattr(module, "_get_sync_session", session_factory)
            # A FRESH adapter per call: the tasks close theirs in a ``finally``.
            patch.setattr(
                module,
                "_build_adapter",
                lambda _ds, _wh=warehouse: _wh.new_adapter(),  # type: ignore[misc]
            )

        with session_factory() as session:
            seeded = _seed_catalog(session, warehouse)
            job = ScanJob(
                id=uuid.uuid4(),
                scan_config_id=seeded.scan_config_id,
                status=ScanJobStatus.pending.value,
            )
            session.add(job)
            session.commit()
            job_id = str(job.id)

        # ── 1. scan: cardinality + event generation, off the real warehouse ──
        scan_summary = scan_task.run_scan.run(str(seeded.scan_config_id), job_id)

        # ── 2. replay: event metrics for exactly the fixture's window ──
        #
        # REPLAY (an explicit window), not the scheduler's resume window, on purpose.
        # The resume window reaches back 30 buckets from ``now`` and would mark ~20
        # empty buckets as COVERED, which the detector then zero-fills — dragging the
        # baseline under ``min_expected_count`` and quietly detecting nothing. An
        # explicit window makes coverage exactly the fixture's ten buckets. Event
        # GENERATION is not skipped by doing this: step 1 above is the same
        # analyze_cardinality + generate_events pipeline catalog sync would run.
        replay_summary = metrics_tasks.collect_metrics.run(
            str(seeded.scan_config_id), None, *window
        )
        with session_factory() as session:
            events = list(
                session.execute(
                    select(Event).where(Event.project_id == seeded.project_id)
                ).scalars()
            )
            event_ids = {event.name: event.id for event in events}
            event_names = set(event_ids)
            event_series = _event_series(session, seeded.scan_config_id)

        # ── 3. the same replay again: re-collecting a window must not change it ──
        metrics_tasks.collect_metrics.run(str(seeded.scan_config_id), None, *window)
        with session_factory() as session:
            event_series_rerun = _event_series(session, seeded.scan_config_id)

        # ── 4. catalog metrics, collected one at a time ──
        with session_factory() as session:
            definitions = {
                FACT_SUM: _fact_metric(
                    seeded,
                    name="Revenue",
                    aggregation=MetricAggregation.sum,
                    config={"measure_column": "amount"},
                    breakdown_columns=["platform"],
                ),
                FACT_COUNT: _fact_metric(
                    seeded, name="Rows", aggregation=MetricAggregation.count, config={}
                ),
                FACT_DISTINCT: _fact_metric(
                    seeded,
                    name="Users",
                    aggregation=MetricAggregation.count_distinct,
                    config={"distinct_column": "user_id"},
                ),
                FACT_RATIO: _ratio_fact_metric(seeded),
                COMP_SINGLE: _composition_metric(
                    seeded,
                    name="Clicks",
                    composition=MetricComposition.single,
                    numerator_event_id=event_ids["event_name=click"],
                ),
                COMP_RATIO: _composition_metric(
                    seeded,
                    name="Clicks per purchase",
                    composition=MetricComposition.ratio,
                    numerator_event_id=event_ids["event_name=click"],
                    denominator_event_id=event_ids["event_name=buy"],
                ),
                # This one is the reason the gate exists. On its first real run it
                # crashed right here with ``TypeError: can't compare offset-naive and
                # offset-aware datetimes`` (now fixed): ClickHouse hands
                # back NAIVE bucket cells where PostgreSQL hands back AWARE ones, and
                # ``_collect_distinct_user_series`` was the one collection path that did
                # not launder its cells through ``_coerce_bucket``, so the naive
                # denominator met the aware numerator read out of ``event_metrics``.
                #
                # Every pre-existing test missed it because the fake adapter returns
                # naive datetimes AND SQLite reads naive buckets back — both sides naive,
                # so it matched. Only two real engines that disagree could surface it.
                COMP_PER_USER: _composition_metric(
                    seeded,
                    name="Clicks per user",
                    composition=MetricComposition.per_distinct_user,
                    numerator_event_id=event_ids["event_name=click"],
                ),
            }
            session.add_all(list(definitions.values()))
            session.commit()
            ids = {key: definition.id for key, definition in definitions.items()}

        for definition_id in ids.values():
            # event_composition ignores the window (it re-derives from the stored
            # event-metric series); fact honours it as a manual backfill.
            metric_collect.collect_metric_definitions.run(str(definition_id), *window)

        with session_factory() as session:
            metric_series = {key: _value_series(session, ids[key]) for key in ids}
            breakdowns = _breakdown_series(session, ids[FACT_SUM])

        # ── 5. drift: recompute the metric-scope anomalies over what just landed ──
        with session_factory() as session:
            config = session.get(ScanConfig, seeded.scan_config_id)
            assert config is not None
            metrics_detect._recalculate_metric_anomalies(  # noqa: SLF001
                session,
                config,
                evaluation_start=PIPELINE_FROM,
                evaluation_end=PIPELINE_TO,
            )
            session.commit()
            names = {str(event_id): name for name, event_id in event_ids.items()}
            names.update({str(definition_id): key for key, definition_id in ids.items()})
            anomalies = _anomalies(
                session,
                scan_config_id=seeded.scan_config_id,
                metric_refs=[str(definition_id) for definition_id in ids.values()],
                names=names,
            )

        # ── 6. the batched fact collector: one shared scan, identical values ──
        batch_totals = metric_collect.collect_fact_metrics_batch.run(
            [str(ids[key]) for key in BATCHED_METRICS], *window
        )
        with session_factory() as session:
            batched_series = {key: _value_series(session, ids[key]) for key in BATCHED_METRICS}

    return PipelineRun(
        db_type=warehouse.db_type,
        scan_summary=scan_summary,
        replay_summary=replay_summary,
        event_names=event_names,
        event_series=event_series,
        event_series_rerun=event_series_rerun,
        metric_series=metric_series,
        batched_series=batched_series,
        batch_totals=dict(batch_totals),
        breakdowns=breakdowns,
        anomalies=anomalies,
    )


@pytest.fixture(scope="session")
def pg_run(app_db: sessionmaker[Session], pg_pipeline: PipelineWarehouse) -> PipelineRun:
    return _run_pipeline(app_db, pg_pipeline)


@pytest.fixture(scope="session")
def ch_run(app_db: sessionmaker[Session], ch_pipeline: PipelineWarehouse) -> PipelineRun:
    return _run_pipeline(app_db, ch_pipeline)


@pytest.fixture(params=("pg", "ch"))
def run(request: pytest.FixtureRequest) -> PipelineRun:
    """One executing warehouse's pipeline run.

    ``getfixturevalue`` is lazy, so a developer with only one warehouse up gets that
    one's parametrization and a SKIP (a CI FAILURE, per ``conftest.unavailable``) for
    the other — rather than both collapsing because one container is missing.
    """
    return request.getfixturevalue(f"{request.param}_run")  # type: ignore[no-any-return]


# ── scan + event generation ───────────────────────────────────────────────────


def test_scan_generates_events_from_the_warehouse(run: PipelineRun) -> None:
    assert run.event_names == set(PIPELINE_EVENT_NAMES)
    assert run.scan_summary["events_created"] == len(PIPELINE_EVENT_NAMES)
    # One column analyzed: ``event_name``. ``user_id`` is in the scan query (the
    # per_distinct_user denominator counts it) but has no FieldDefinition, so the
    # generator must skip it rather than fold it into the event identity.
    assert run.scan_summary["columns_analyzed"] == 1
    details = run.scan_summary["details"]
    assert isinstance(details, list)
    assert any("user_id" in str(detail) for detail in details), details


# ── event metrics ─────────────────────────────────────────────────────────────


def test_event_metrics_match_floor_to_bucket(run: PipelineRun) -> None:
    assert run.event_series == pipeline_event_counts()


def test_event_metrics_land_on_the_fixtures_buckets(run: PipelineRun) -> None:
    # The first row of each bucket sits exactly ON the boundary and the last sits one
    # microsecond BEFORE the next one; both must land in the same bucket, so the
    # bucket set is exactly the fixture's — no off-by-one bucket at either end.
    assert sorted(run.event_series["event_name=click"]) == pipeline_buckets()


def test_replaying_the_same_window_is_idempotent(run: PipelineRun) -> None:
    assert run.event_series_rerun == run.event_series


def test_the_replay_was_actually_chunked(run: PipelineRun) -> None:
    # A replay that silently ran as one query would still pass every value assertion
    # above, so assert the chunking really happened: 10 buckets at a 6h chunk = 2.
    assert run.replay_summary["mode"] == "metrics_replay"
    assert run.replay_summary["catalog_sync_skipped"] is True
    assert run.replay_summary["replay_chunk_interval"] == REPLAY_CHUNK
    assert run.replay_summary["replay_chunks_total"] == 2
    assert run.replay_summary["replay_chunks_completed"] == 2


# ── fact metrics: single, ratio, breakdowns ───────────────────────────────────


def test_fact_single_sum_matches_the_reference(run: PipelineRun) -> None:
    # NULL amounts (every ``view`` row) must be SKIPPED by SUM, not read as zero.
    assert run.metric_series[FACT_SUM] == pipeline_amount_sums()


def test_fact_single_count_and_distinct_match_the_reference(run: PipelineRun) -> None:
    assert run.metric_series[FACT_COUNT] == pipeline_row_counts()
    assert run.metric_series[FACT_DISTINCT] == pipeline_distinct_users()


def test_fact_ratio_matches_the_reference(run: PipelineRun) -> None:
    assert run.metric_series[FACT_RATIO] == pipeline_count_ratio()


def test_fact_ratio_drops_the_divide_by_zero_bucket(run: PipelineRun) -> None:
    """The first bucket has no ``buy`` row. It must be ABSENT — not 0, not inf."""
    first_bucket = pipeline_buckets()[0]
    assert first_bucket not in run.metric_series[FACT_RATIO]
    # ...and it is genuinely a zero-denominator bucket, not simply a bucket with no
    # data: the numerator series (count of ALL rows) does have it.
    assert first_bucket in run.metric_series[FACT_COUNT]


def test_fact_breakdown_rows_match_the_reference(run: PipelineRun) -> None:
    assert run.breakdowns == pipeline_platform_sums()


# ── fact metrics: the batched collector ───────────────────────────────────────


def test_batched_fact_collection_reproduces_the_per_metric_values(run: PipelineRun) -> None:
    """One shared multi-aggregate scan must compute exactly what N scans computed.

    This is the assertion the batched path's whole design rests on: a conditional
    aggregate (``countIf`` / ``FILTER (WHERE ...)``) has to equal the same aggregate
    over a row-filtered subquery. Only a real warehouse can settle that.
    """
    for key in BATCHED_METRICS:
        assert run.batched_series[key] == run.metric_series[key], key


def test_the_batch_actually_collected_every_metric(run: PipelineRun) -> None:
    assert run.batch_totals["metrics"] == len(BATCHED_METRICS)
    assert run.batch_totals["collected"] == len(BATCHED_METRICS)
    assert run.batch_totals["errors"] == 0


# ── event_composition metrics ─────────────────────────────────────────────────


def test_event_composition_single_matches_the_event_metrics(run: PipelineRun) -> None:
    assert run.metric_series[COMP_SINGLE] == pipeline_event_counts()["event_name=click"]


def test_event_composition_ratio_matches_the_reference(run: PipelineRun) -> None:
    assert run.metric_series[COMP_RATIO] == pipeline_event_composition_ratio()
    # Bucket 0 has clicks but no purchases: a zero denominator, dropped.
    assert pipeline_buckets()[0] not in run.metric_series[COMP_RATIO]


def test_per_distinct_user_matches_the_reference(run: PipelineRun) -> None:
    """The composition that mixes a STORED series with a FRESH warehouse query.

    This is the shape that broke. The numerator comes out of ``event_metrics``
    (PostgreSQL, tz-AWARE); the denominator is a ``count_distinct`` the collector runs
    against the warehouse *right now* — and ClickHouse returns that bucket tz-NAIVE.
    ``_collect_distinct_user_series`` was the one collection path that did not normalize
    its cells through ``_coerce_bucket``, so the two met unnormalized in
    ``evaluate_composition`` and ``sorted()`` raised ``TypeError: can't compare
    offset-naive and offset-aware datetimes``. Every per_distinct_user metric on a
    ClickHouse source failed collection outright.

    This gate found it on its first real run, and no pre-existing test could have: the
    fake adapter returns naive datetimes AND SQLite reads naive buckets back, so both
    sides were naive and matched. It takes two real engines that genuinely disagree.
    """
    assert run.metric_series[COMP_PER_USER] == pipeline_events_per_distinct_user()


# ── drift / anomalies ─────────────────────────────────────────────────────────


def test_the_event_spike_is_detected(run: PipelineRun) -> None:
    events = {anomaly for anomaly in run.anomalies if anomaly[0] == "event"}
    assert events == {("event", "event_name=click", "spike", pipeline_spike_bucket())}


@pytest.mark.parametrize("metric", [FACT_SUM, FACT_COUNT, FACT_RATIO, COMP_SINGLE, COMP_RATIO])
def test_the_metric_spike_is_detected(run: PipelineRun, metric: str) -> None:
    assert ("metric", metric, "spike", pipeline_spike_bucket()) in run.anomalies


def test_a_flat_series_is_not_an_anomaly(run: PipelineRun) -> None:
    """count_distinct(user_id) is 3 in every bucket, spike bucket included.

    A detector that flagged it would be inventing drift out of a constant series —
    the false-positive twin of missing the real spike above.
    """
    assert not [anomaly for anomaly in run.anomalies if anomaly[1] == FACT_DISTINCT]


# ── BigQuery: the same pipeline, ANALYZED (never valued) ──────────────────────
#
# Read the module docstring before touching anything below. The emulator is
# authoritative on whether GoogleSQL is VALID and on nothing else. These tests assert
# that every statement the PIPELINE generates for a BigQuery source analyzes — the
# ratio operands' dialect-compiled conditions, the batched conditional aggregates, the
# bucketed scans. They assert no values, and must never be changed to.

#: A table-less GoogleSQL source: the analyzer gets a fully typed relation to resolve
#: every generated column against, with no dataset, no seeding and no credentials. It
#: carries the SAME five columns as the executing warehouses' pipeline table, so one
#: schema serves both the scan query and the fact query.
BQ_SOURCE = PIPELINE_BASE

#: The adapter's own connection probe, answered (not analyzed) by the capturing client.
_CONNECTION_PROBE = "SELECT 1 AS ok"

_BQ_SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("ts", "TIMESTAMP"),
    bigquery.SchemaField("event_name", "STRING"),
    bigquery.SchemaField("user_id", "STRING"),
    bigquery.SchemaField("amount", "FLOAT64"),
    bigquery.SchemaField("platform", "STRING"),
]


class _BigQueryCapture:
    """Builds real ``BigQueryAdapter``s whose client only records the SQL it is given.

    ``__init__`` is bypassed because it would construct a live ``bigquery.Client`` from
    service-account credentials — the exact thing this gate exists to not need. A fresh
    adapter is handed out per ``_build_adapter`` call (the worker closes its adapters),
    but all of them append to ONE ``sql`` list, so the whole pipeline's output lands in
    a single place.
    """

    def __init__(self) -> None:
        self.sql: list[str] = []

    def new_adapter(self) -> BigQueryAdapter:
        adapter = object.__new__(BigQueryAdapter)
        adapter._client = _CapturingClient(self.sql)  # type: ignore[assignment]
        adapter._project = "tripl-test"
        adapter._dataset = "wh"
        adapter._timeout_seconds = None
        adapter._maximum_bytes_billed = None
        adapter._dataset_allowlist = None
        # The rest of ``__init__``'s state, EMPTY — exactly as a freshly built adapter
        # has it. It matters that these start empty rather than pre-seeded: the first
        # thing the worker does with a fresh adapter is the schema probe
        # (``_collect_fact_single`` / ``_collect_fact_ratio`` call ``get_columns`` once
        # per metric/operand, whatever the aggregation needs), so that probe and the
        # adapter's own lazy ``_ensure_column_types`` are part of what this gate is
        # here to analyze. Hand-seeding them would skip it.
        adapter._allowed_columns = set()
        adapter._column_types = {}
        adapter._struct_paths = {}
        adapter._repeated_columns = set()
        return adapter


class _CapturingClient:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def query(self, sql: str) -> _Job:
        self._sink.append(sql)
        if sql.endswith("LIMIT 0"):  # the adapter's schema probe
            return _Job([], schema=_BQ_SCHEMA)
        if sql == _CONNECTION_PROBE:
            # Every worker task calls ``test_connection`` first, and the adapter reads
            # that row back BY NAME (``row["ok"]``) — so this one canned row is keyed,
            # not positional. The adapter-call gate never needed it: it drives the
            # adapter directly, not through a task.
            return _Job([(1,)], fields=("ok",))
        # Everything else returns NO rows. This gate asserts SQL is valid, never what
        # it computes — an empty result is exactly right.
        return _Job([])

    def close(self) -> None:
        return None


class _Job:
    def __init__(
        self,
        rows: list[tuple[object, ...]],
        schema: list[bigquery.SchemaField] | None = None,
        fields: tuple[str, ...] = (),
    ) -> None:
        self._rows = rows
        self._schema = schema or []
        self._fields = fields

    def result(self, **_kwargs: object) -> _Result:
        return _Result(self._rows, self._schema, self._fields)


class _Result:
    def __init__(
        self,
        rows: list[tuple[object, ...]],
        schema: list[bigquery.SchemaField],
        fields: tuple[str, ...],
    ) -> None:
        self._rows = rows
        self._fields = fields
        self.schema = schema

    def __iter__(self) -> Iterator[_Row]:
        return iter(_Row(row, self._fields) for row in self._rows)


class _Row:
    """A BigQuery result row: readable positionally AND by field name."""

    def __init__(self, values: tuple[object, ...], fields: tuple[str, ...] = ()) -> None:
        self._values = values
        self._fields = fields

    def values(self) -> tuple[object, ...]:
        return self._values

    def __getitem__(self, key: str) -> object:
        return self._values[self._fields.index(key)]


def _seed_event_metrics(session: Session, seeded: _Seeded, event_type_id: uuid.UUID) -> None:
    """Give the BigQuery run a stored event-metric series to compose against.

    The capturing client returns no rows, so the scan generates no events and the
    collection writes no metrics — which would leave ``_collect_event_composition``
    with an empty numerator and make it return early WITHOUT ever asking the warehouse
    for the ``per_distinct_user`` denominator. Seeding the app-side series directly is
    what puts that ``count_distinct`` statement in front of the analyzer.
    """
    reference = pipeline_event_counts()
    for identity, series in reference.items():
        event = Event(
            id=uuid.uuid4(),
            project_id=seeded.project_id,
            event_type_id=event_type_id,
            name=identity,
            description="",
            status="implemented",
        )
        session.add(event)
        for bucket, count in series.items():
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=seeded.scan_config_id,
                    event_id=event.id,
                    event_type_id=None,
                    bucket=bucket,
                    count=count,
                )
            )
    session.commit()


@pytest.fixture(scope="session")
def bq_pipeline_sql(app_db: sessionmaker[Session]) -> list[str]:
    """Every GoogleSQL statement the pipeline generates for a BigQuery data source."""
    capture = _BigQueryCapture()
    warehouse = PipelineWarehouse(
        db_type="bigquery",
        new_adapter=capture.new_adapter,
        events_query=BQ_SOURCE,
        facts_query=BQ_SOURCE,
    )
    window = (PIPELINE_FROM.isoformat(), PIPELINE_TO.isoformat())

    with pytest.MonkeyPatch.context() as patch:
        for module in (scan_task, metrics_tasks, metric_collect):
            patch.setattr(module, "_get_sync_session", app_db)
            patch.setattr(
                module,
                "_build_adapter",
                lambda _ds, _wh=warehouse: _wh.new_adapter(),  # type: ignore[misc]
            )

        with app_db() as session:
            seeded = _seed_catalog(session, warehouse)
            config = session.get(ScanConfig, seeded.scan_config_id)
            assert config is not None and config.event_type_id is not None
            event_type_id = config.event_type_id
            job = ScanJob(
                id=uuid.uuid4(),
                scan_config_id=seeded.scan_config_id,
                status=ScanJobStatus.pending.value,
            )
            session.add(job)
            session.commit()
            job_id = str(job.id)

        scan_task.run_scan.run(str(seeded.scan_config_id), job_id)
        metrics_tasks.collect_metrics.run(str(seeded.scan_config_id), None, *window)

        with app_db() as session:
            _seed_event_metrics(session, seeded, event_type_id)
            events = {
                event.name: event.id
                for event in session.execute(
                    select(Event).where(Event.project_id == seeded.project_id)
                ).scalars()
            }
            definitions = {
                FACT_SUM: _fact_metric(
                    seeded,
                    name="Revenue",
                    aggregation=MetricAggregation.sum,
                    config={"measure_column": "amount"},
                    breakdown_columns=["platform"],
                ),
                FACT_COUNT: _fact_metric(
                    seeded, name="Rows", aggregation=MetricAggregation.count, config={}
                ),
                FACT_DISTINCT: _fact_metric(
                    seeded,
                    name="Users",
                    aggregation=MetricAggregation.count_distinct,
                    config={"distinct_column": "user_id"},
                ),
                FACT_RATIO: _ratio_fact_metric(seeded),
                COMP_PER_USER: _composition_metric(
                    seeded,
                    name="Clicks per user",
                    composition=MetricComposition.per_distinct_user,
                    numerator_event_id=events["event_name=click"],
                ),
            }
            session.add_all(list(definitions.values()))
            session.commit()
            ids = {key: definition.id for key, definition in definitions.items()}

        for bq_definition_id in ids.values():
            metric_collect.collect_metric_definitions.run(str(bq_definition_id), *window)
        metric_collect.collect_fact_metrics_batch.run(
            [str(ids[key]) for key in BATCHED_METRICS], *window
        )

    return capture.sql


def test_the_bigquery_pipeline_generated_the_sql_it_is_meant_to(
    bq_pipeline_sql: list[str],
) -> None:
    """Guard the guard.

    ``test_every_bigquery_pipeline_statement_analyzes`` passes trivially if the capture
    is empty or shallow — and the capturing client returns no rows, so a pipeline that
    bailed out early would still look green. Pin the statements that must be in there:
    the type-directed bucket function (this is where ``TIMESTAMP_BIN`` lived), the
    row-filtered subquery a ratio operand's condition compiles to, and a distinct count.
    """
    joined = "\n\n".join(bq_pipeline_sql)
    assert len(bq_pipeline_sql) >= 10, joined
    assert "TIMESTAMP_" in joined, joined
    assert "_filtered" in joined, joined
    assert "DISTINCT" in joined.upper(), joined


def test_every_bigquery_pipeline_statement_analyzes(
    bq_pipeline_sql: list[str], bq_analyze: Callable[[str], str | None]
) -> None:
    """ANALYSIS ONLY — see the module docstring. No value here is ever asserted."""
    rejected = [(sql, error) for sql in bq_pipeline_sql if (error := bq_analyze(sql)) is not None]
    if rejected:
        detail = "\n\n".join(f"  REJECTED: {error}\n  SQL: {sql}" for sql, error in rejected)
        pytest.fail(
            f"ZetaSQL rejected {len(rejected)} of {len(bq_pipeline_sql)} statement(s) the "
            f"metrics pipeline generates for BigQuery:\n{detail}"
        )


# ── the point of the whole gate ───────────────────────────────────────────────


def test_postgres_and_clickhouse_agree(pg_run: PipelineRun, ch_run: PipelineRun) -> None:
    """Two engines, one pipeline, identical numbers — normalized, not merely "it ran".

    Every series above is already pinned to a ``floor_to_bucket`` reference, so this
    is belt to that braces: it fails loudly if the two warehouses ever agree with each
    other but not with the reference, or agree with the reference on the pieces this
    file asserts while diverging on something it does not.
    """
    assert pg_run.event_series == ch_run.event_series
    assert pg_run.metric_series == ch_run.metric_series
    assert pg_run.batched_series == ch_run.batched_series
    assert pg_run.breakdowns == ch_run.breakdowns
    assert pg_run.event_names == ch_run.event_names
    # Anomalies carry per-run uuids in their scope_ref, so compare the resolved
    # (scope, name, direction, bucket) shape — which is warehouse-independent.
    assert pg_run.anomalies == ch_run.anomalies
