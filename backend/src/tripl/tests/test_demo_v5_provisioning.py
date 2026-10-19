"""Recipe 5 provisioning: the plan is authored, observations are scanned.

One demo is provisioned end to end on the in-memory warehouse engine (the
conftest seeds it inline, as the worker would) and checked against the
recipe's invariants: the roster's names are the plan, the observed surfaces
come out of tripl's own scan and replay, the human reactions the recipe adds
sit on what the replay detected, and the frontend's contract holds right after
creation. Invariants, not counts copied from a run: the warehouse model and the
detector may evolve, the story may not.

A second, two-day demo is what the rest of the suite provisions
(``DEMO_HISTORY_DAYS``): shorter, seasonality-blind, still complete.

The calendar properties at the end are pure: whatever hour a demo is created,
the replay's walk-forward split lands on an incident the dispatcher can alert
on, and the build's one detection pass is the job that ends there, so a fresh
demo always opens with something in its alert inbox. Last, the in-memory
engine's hourly counting is held to the synthetic adapter's row-by-row
implementation it replaces on the collection path.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from tripl.config import settings
from tripl.core.adapters.demo_memory import DemoMemoryAdapter
from tripl.core.adapters.synthetic import SyntheticAdapter
from tripl.core.demo_warehouse import (
    DEAD_EVENT,
    EVENTS_SCAN_SQL,
    KEPT_EVENT_NAMES,
    KEPT_EVENT_TYPES,
    SCHEMA_DRIFT_EVENT,
    VARIABLE_DRIFT_EVENT,
    incidents_between,
    roster,
)
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_destination import AlertDestination
from tripl.models.data_source import DataSource
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.metric_value import MetricValue
from tripl.models.planned_event import PlannedEvent
from tripl.models.project import Project
from tripl.models.release_regression import ReleaseRegression
from tripl.models.scan_job import ScanJob
from tripl.models.schema_drift import SchemaDrift
from tripl.models.signal_triage import SignalTriage
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.services import demo_service
from tripl.services.demo import DEMO_RECIPE_VERSION, noise, pipeline
from tripl.services.demo.builders import alerts as alert_builder
from tripl.services.demo.builders import warehouse as warehouse_builder
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.utils.query_windows import resolve_lookback_window

#: Generous next to the budgets (four minutes for the full history, twenty
#: seconds for two days, on a small ARM host): runners differ, and a
#: provisioning far past them is a regression worth failing on all the same.
PROVISIONING_SECONDS_LIMIT = 600
SHORT_PROVISIONING_SECONDS_LIMIT = 120


@pytest.fixture
def _memory_warehouse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "demo_warehouse_mode", "memory")


@pytest.fixture
def queued_deliveries(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The deliveries the seed would hand to the broker (the suite has none)."""
    queued: list[str] = []

    async def _capture(delivery_ids: list[str]) -> None:
        queued.extend(delivery_ids)

    monkeypatch.setattr(demo_service, "enqueue_alert_deliveries", _capture)
    return queued


@pytest.mark.usefixtures("_memory_warehouse")
async def test_a_v5_demo_is_the_roster_observed_by_the_real_pipeline(
    client: AsyncClient, queued_deliveries: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The full story: three weekly promo sends, the release train, the
    # whole-history scoring pass. The conftest runs the history fill inline after the
    # first build, so the request returns the filled demo.
    monkeypatch.setattr(settings, "demo_history_days", noise.DEMO_HISTORY_DAYS)
    started = time.perf_counter()
    resp = await client.post("/api/v1/projects/demo")
    elapsed = time.perf_counter() - started
    assert resp.status_code == 202, resp.text
    demo = resp.json()
    assert demo["generation_status"] == "ready", demo
    assert demo["generation_stage"] is None
    assert demo["generation_progress"] is None
    assert demo["demo_history_status"] == "complete"
    assert demo["demo_history_progress"] is None
    assert demo["demo_recipe_version"] == DEMO_RECIPE_VERSION == "5"
    slug = demo["slug"]
    project_id = uuid.UUID(demo["id"])

    async with TestSessionLocal() as session:
        # The plan is the roster: every name, nothing invented by the scan
        # among the planned events, the dead event's one authored observation.
        events = {
            name: (status, last_seen)
            for name, status, last_seen in (
                await session.execute(
                    select(Event.name, Event.status, Event.last_seen_at).where(
                        Event.project_id == project_id
                    )
                )
            ).all()
        }
        roster_names = {event.name for event in roster.events()}
        assert roster_names <= set(events)
        assert set(KEPT_EVENT_NAMES) <= set(events)
        assert events[DEAD_EVENT][1] is not None
        source = (
            await session.execute(select(DataSource).where(DataSource.project_id == project_id))
        ).scalar_one()
        assert source.db_type == "demo"
        assert source.last_test_message and "memory" in source.last_test_message

        # Observed by the pipeline, never seeded: volumes, breakdowns, the drifts
        # the warehouse carries, release regressions, catalog metric values.
        async def count(model: type[Base]) -> int:
            return int(await session.scalar(select(func.count()).select_from(model)) or 0)

        assert await count(EventMetric) > 0
        assert await count(EventMetricBreakdown) > 0
        assert await count(MetricAnomaly) > 0
        assert await count(DistributionDrift) > 0
        assert await count(ReleaseRegression) > 0
        assert await count(MetricValue) > 0
        schema_drift_fields = set(
            (await session.execute(select(SchemaDrift.field_name, SchemaDrift.sample_value))).all()
        )
        # The live collection's contract check, which a replay skips.
        assert ("currency", "GBP") in schema_drift_fields
        jobs = (await session.execute(select(ScanJob.status, ScanJob.result_summary))).all()
        assert jobs and {status for status, _ in jobs} == {"completed"}
        assert SCHEMA_DRIFT_EVENT in events
        # One detection pass for the whole build, and no platform series scored.
        # At most one detection pass per phase: the first build's over its two
        # days, the history fill's over the whole history.
        scoring = [
            summary
            for _, summary in jobs
            if summary and int(str(summary.get("anomalies_detected") or 0)) > 0
        ]
        assert 1 <= len(scoring) <= 2
        assert await count(MetricBreakdownAnomaly) == 0

        # Reactions to what the replay detected.
        promo = (
            (
                await session.execute(
                    select(SignalTriage).where(
                        SignalTriage.note == pipeline.DEMO_WEEKLY_PROMO_NOTE,
                        SignalTriage.action == "expected",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(promo) >= 3
        planned = (
            await session.execute(
                select(PlannedEvent).where(PlannedEvent.label == pipeline.DEMO_PLANNED_EVENT_LABEL)
            )
        ).scalar_one()
        assert planned.scope_type == "metric"

        # Alerting: the dispatcher filed deliveries; the flaky sink is the column.
        flaky = (
            await session.execute(
                select(AlertDestination).where(
                    AlertDestination.name == alert_builder.FLAKY_SINK_NAME
                )
            )
        ).scalar_one()
        assert flaky.simulate_first_attempt_failure is True
        deliveries = (await session.execute(select(AlertDelivery.id))).scalars().all()
        assert deliveries
        assert {str(delivery_id) for delivery_id in deliveries} == set(queued_deliveries)

    # The frontend's contract right after creation.
    inbox = await client.get(f"/api/v1/projects/{slug}/alert-inbox")
    assert inbox.status_code == 200, inbox.text
    assert inbox.json()["total"] > 0
    types = await client.get(f"/api/v1/projects/{slug}/event-types")
    assert types.status_code == 200
    assert set(KEPT_EVENT_TYPES) <= {row["name"] for row in types.json()}

    # Last, so a slow runner still reports which of the invariants above hold.
    assert elapsed < PROVISIONING_SECONDS_LIMIT, f"provisioning took {elapsed:.0f}s"


@pytest.mark.usefixtures("_memory_warehouse")
async def test_a_cancel_between_stages_discards_the_demo(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _cancel_during_scan(session, ctx) -> None:  # type: ignore[no-untyped-def]
        project = await session.get(Project, ctx.project_id)
        project.generation_stage = demo_service.DEMO_CANCEL_REQUESTED_STAGE
        await session.commit()

    monkeypatch.setattr(pipeline, "run_scan_stage", _cancel_during_scan)
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 409, resp.text
    async with TestSessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Project)) == 0
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


@pytest.mark.usefixtures("_memory_warehouse")
async def test_a_failed_stage_leaves_only_the_failure_marker(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(session, ctx) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("warehouse unreachable")

    monkeypatch.setattr(pipeline, "run_scan_stage", _boom)
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["generation_status"] == "failed"
    assert "RuntimeError" in body["generation_error"]
    async with TestSessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 0
        shell = await session.get(Project, uuid.UUID(body["id"]))
        assert shell is not None and shell.slug == body["slug"]


@pytest.mark.usefixtures("_memory_warehouse")
async def test_a_short_history_demo_is_still_complete(
    client: AsyncClient, queued_deliveries: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "demo_history_days", 2)
    started = time.perf_counter()
    resp = await client.post("/api/v1/projects/demo")
    elapsed = time.perf_counter() - started
    assert resp.status_code == 202, resp.text
    assert resp.json()["generation_status"] == "ready"
    async with TestSessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(EventMetric))
        assert await session.scalar(select(func.count()).select_from(MetricAnomaly))
        drifts = (await session.execute(select(SchemaDrift.sample_value))).scalars().all()
        assert "GBP" in drifts
    assert queued_deliveries
    inbox = await client.get(f"/api/v1/projects/{resp.json()['slug']}/alert-inbox")
    assert inbox.json()["total"] > 0
    assert elapsed < SHORT_PROVISIONING_SECONDS_LIMIT, f"provisioning took {elapsed:.0f}s"


@pytest.mark.usefixtures("_memory_warehouse")
async def test_the_undocumented_product_is_an_open_value_drift(
    client: AsyncClient, queued_deliveries: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "demo_history_days", 2)
    resp = await client.post("/api/v1/projects/demo")
    assert resp.json()["generation_status"] == "ready"
    async with TestSessionLocal() as session:
        observed = (
            (
                await session.execute(
                    select(VariableValueDrift.observed_values)
                    .join(Event, Event.id == VariableValueDrift.event_id)
                    .where(Event.name == VARIABLE_DRIFT_EVENT)
                )
            )
            .scalars()
            .all()
        )
    assert any("prod_weekly" in (values or []) for values in observed)


@pytest.mark.parametrize("hour", range(24))
def test_every_build_hour_scans_the_undocumented_product_on_trials(hour: int) -> None:
    """The test above's value drift, held at every build hour of a day, wall clock aside.

    The drift detector reads ``product_id`` from the live collection's catalog
    scan only: :data:`~.warehouse.SCAN_LOOKBACK_HOURS` ending at the build hour,
    the ``resolve_lookback_window`` the collector applies. Asked here of the
    engine directly, as the grouped scan asks it. 2026-10-19 is the day whose
    builds at 14:00 and 15:00 UTC scanned no ``prod_weekly`` trial.
    """
    now = datetime(2026, 10, 19, hour, 20, tzinfo=UTC)
    window = resolve_lookback_window(
        time_column="event_time",
        lookback_hours=warehouse_builder.SCAN_LOOKBACK_HOURS,
        end=now.replace(minute=0),
    )
    assert window is not None
    engine = DemoMemoryAdapter(now=now)
    reg_names, _, _, breakdown = engine.get_full_breakdown(
        EVENTS_SCAN_SQL,
        ["event_name", "product_id"],
        [],
        time_column="event_time",
        time_from=window[0],
        time_to=window[1],
    )
    name_at, product_at = reg_names.index("event_name"), reg_names.index("product_id")
    trial_products = {row[product_at] for row in breakdown if row[name_at] == VARIABLE_DRIFT_EVENT}
    assert roster.UNDOCUMENTED_PRODUCT in trial_products


def test_the_history_progress_reads_the_replay_jobs() -> None:
    now = datetime(2026, 10, 14, 9, tzinfo=UTC)
    windows = pipeline.history_windows(now)
    assert windows[0][0] == noise.history_start(now)
    assert windows[-1][1] == now
    half = [
        {
            "time_from": start.isoformat(),
            "time_to": end.isoformat(),
            "replay_chunks_total": 4,
            "replay_chunks_completed": 4 if index < len(windows) // 2 else 0,
        }
        for index, (start, end) in enumerate(windows)
    ]
    progress = pipeline.history_progress(half)
    assert 0 < progress < 99
    done = [{**row, "replay_chunks_completed": 4} for row in half]
    assert pipeline.history_progress(done) == 99
    assert pipeline.history_progress([]) == 0


@pytest.mark.parametrize("hour", range(0, 24 * 7, 1))
def test_every_creation_hour_has_an_incident_to_alert_on(
    hour: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property over a week of creation hours: the split sits on an incident.

    On the shipped history (the suite's two days would clamp the scoring span).
    The first build's windows are the ones checked: they hold the split, and
    the history fill's re-scoring ends at the same instant.
    """
    monkeypatch.setattr(settings, "demo_history_days", noise.DEMO_HISTORY_DAYS)
    now = datetime(2026, 10, 12, tzinfo=UTC) + timedelta(hours=hour)
    windows = pipeline.history_windows(now, pipeline.first_build_start(now))
    # Contiguous, oldest first, the first build's two days, no window too wide.
    assert windows[0][0] == pipeline.first_build_start(now)
    assert windows[-1][1] == now
    # The last window is the live collection, which resumes where a replay
    # stopped: a replay always comes first, an incident still running at the
    # build hour included.
    assert len(windows) >= 2
    for (_, end), (start, _) in zip(windows, windows[1:], strict=False):
        assert end == start
    assert all(end - start <= pipeline.REPLAY_JOB_SPAN for start, end in windows)
    # One window ends right after an incident hour that is complete and recent.
    incident_hours = {
        bucket
        for incident in incidents_between(now - timedelta(days=2), now)
        for bucket in pipeline._hours(incident.start, incident.end)
    }
    heads = {end - timedelta(hours=1) for _, end in windows}
    alertable = {
        bucket
        for bucket in heads & incident_hours
        if bucket >= now - timedelta(hours=23) and bucket + timedelta(hours=1) <= now
    }
    assert alertable
    # The one detection pass is the job that ends there, and it scores the
    # whole history (the weekly promo's first send included).
    scored_from, scored_to = pipeline.scoring_window(now)
    assert scored_to - timedelta(hours=1) in alertable
    assert scored_from == noise.history_start(now) == noise.history_start(now).replace(hour=0)
    # The first build scores its two days up to the same instant.
    first_from, first_to = pipeline.scoring_window(now, pipeline.first_build_start(now))
    assert (first_from, first_to) == (pipeline.first_build_start(now), scored_to)


_ROW_COLUMNS = [
    "event_type",
    "event_name",
    "screen_name",
    "platform",
    "app_version",
    "country",
    "button_id",
    "product_id",
    "amount",
    "currency",
]


@pytest.mark.parametrize("interval", ["1h", "15m", "1d"])
@pytest.mark.parametrize(
    "breakdowns",
    [None, ["platform"], [*warehouse_builder.DISTRIBUTION_DRIFT_FIELDS, "properties.source"]],
)
def test_the_engine_counts_an_hour_at_a_time_as_the_rows_would(
    interval: str, breakdowns: list[str] | None
) -> None:
    """Same rows, keys and order as ``SyntheticAdapter``, on a window that cuts hours."""
    now = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
    time_to = datetime(2026, 10, 14, 8, 40, tzinfo=UTC)
    time_from = time_to - timedelta(hours=3, minutes=25)
    paths = {"properties": ["source", "payment_method", "error_code", "results_count"]}
    if breakdowns is None:
        args: tuple[object, ...] = (EVENTS_SCAN_SQL, "event_time", interval, _ROW_COLUMNS)
        name = "get_time_bucketed_counts"
    else:
        args = (EVENTS_SCAN_SQL, "event_time", interval, breakdowns, _ROW_COLUMNS)
        name = "get_time_bucketed_breakdown_counts_multi"
    args = (*args, ["properties"], paths, time_from, time_to)
    fast = getattr(DemoMemoryAdapter(now=now), name)(*args)
    by_row = getattr(SyntheticAdapter, name)(DemoMemoryAdapter(now=now), *args)
    assert fast[-1]
    assert fast == by_row
