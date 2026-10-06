"""Tests for the demo project generator endpoint."""

import uuid
from datetime import UTC, datetime, timedelta
from statistics import median

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.adapters.registry import build_adapter
from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT
from tripl.core.analyzers.distribution_drift import PSI_BAND_MINOR, PSI_BAND_SIGNIFICANT
from tripl.core.bucketing import floor_to_bucket
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.data_source import DataSource, TestStatus
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import MetricScopeType, ProjectGenerationStatus
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.planned_event import PlannedEvent
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.schema_drift import SCHEMA_DRIFT_STATUS_OPEN
from tripl.models.variable import Variable
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.services import demo_service, plan_branch_service
from tripl.services.demo import DemoContext, noise, seed_demo_content
from tripl.services.demo.builders import plan
from tripl.services.demo.builders.alerts import DEMO_PLANNED_EVENT_LABEL
from tripl.services.demo.builders.variables import DRIFT_OBSERVED_VALUES
from tripl.services.demo.builders.warehouse import (
    WEEKLY_PROMO_EVENT_NAME,
    WEEKLY_PROMO_MULTIPLIER,
    weekly_promo_buckets,
)
from tripl.services.demo.scenario import DEMO_SEED
from tripl.services.project_service import demo_data_source_name
from tripl.tests._audit_feed import org_audit
from tripl.tests.conftest import TestSessionLocal


async def _project_id_for_slug(session: AsyncSession, slug: str) -> uuid.UUID:
    return (await session.execute(select(Project.id).where(Project.slug == slug))).scalar_one()


async def _scan_config_id_for_project(
    session: AsyncSession, project_id: uuid.UUID
) -> uuid.UUID | None:
    return (
        (await session.execute(select(ScanConfig.id).where(ScanConfig.project_id == project_id)))
        .scalars()
        .first()
    )


@pytest.mark.asyncio
async def test_create_demo_project_returns_202(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    data = resp.json()
    assert data["slug"].startswith("demo-")
    assert len(data["slug"]) == len("demo-") + 6


@pytest.mark.asyncio
async def test_demo_project_has_events(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    events_resp = await client.get(f"/api/v1/projects/{slug}/events")
    assert events_resp.status_code == 200
    data = events_resp.json()
    items = data["items"] if isinstance(data, dict) else data
    assert len(items) > 0

    # At least one event should have field values
    has_field_values = any(len(ev.get("field_values", [])) > 0 for ev in items)
    assert has_field_values, "Expected at least one event with field values"


@pytest.mark.asyncio
async def test_demo_events_first_seen_matches_history_window(client: AsyncClient) -> None:
    # "First seen" (created_at) is STAGGERED across the seeded ~23-day metric
    # history (PR #51 follow-up): core events anchor the window
    # start, the rest ramp in, and nothing is younger than ~2 days. A uniform
    # history_start stamp left the Overview 14-day "active events" sparkline a
    # flat zero; the provisioning instant made everything first seen "just now".
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    events_resp = await client.get(f"/api/v1/projects/{slug}/events")
    assert events_resp.status_code == 200
    data = events_resp.json()
    items = data["items"] if isinstance(data, dict) else data
    assert items

    now = datetime.now(tz=UTC)
    ages_by_name: dict[str, float] = {}
    for ev in items:
        created = datetime.fromisoformat(ev["created_at"])
        # The API may serialize created_at without an offset; treat naive as UTC so
        # the subtraction below doesn't mix naive/aware datetimes.
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        ages_by_name[ev["name"]] = (now - created).total_seconds() / 86400

    # (a) Nothing reads as first seen "just now" — the stagger floor is ~2 days;
    # assert >= 1 day to leave deterministic-jitter slack.
    for name, age_days in ages_by_name.items():
        assert age_days >= 1, f"{name} first seen only {age_days:.1f}d ago — reads as brand new"

    # (b) The oldest (core) events still anchor the full ~23-day history window.
    assert max(ages_by_name.values()) >= noise.DEMO_HISTORY_DAYS - 1, ages_by_name
    assert ages_by_name["Home Screen View"] >= noise.DEMO_HISTORY_DAYS - 1, ages_by_name[
        "Home Screen View"
    ]

    # (c) Part of the catalog falls INSIDE the Overview KPI's 14-day lookback so
    # the "active events" sparkline is not a flat zero. (< 13d is always >= the
    # route's midnight-aligned time_from for days=14.)
    inside_14d = [name for name, age_days in ages_by_name.items() if age_days < 13]
    assert inside_14d, "no event first seen inside the last 14 days — sparkline is a flat zero"


def test_demo_event_first_seen_stagger_is_deterministic() -> None:
    # (d) Same recipe (clock + seed) -> identical first-seen timestamps: the
    # stagger derives from noise.derive_seed(seed, event name), never from
    # random state or the wall clock at call time.
    fixed_now = datetime(2026, 7, 1, 10, 30, tzinfo=UTC)
    specs = plan.event_specs(fixed_now)
    first = plan.staggered_created_ats(specs, now=fixed_now, seed=DEMO_SEED)
    second = plan.staggered_created_ats(specs, now=fixed_now, seed=DEMO_SEED)
    assert first == second
    assert len(first) == len(specs)


@pytest.mark.asyncio
async def test_demo_project_has_metrics(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    metrics_resp = await client.get(f"/api/v1/projects/{slug}/metrics/total")
    assert metrics_resp.status_code == 200
    data = metrics_resp.json()
    assert len(data["data"]) > 0, "Expected metric data points for demo project"


@pytest.mark.asyncio
async def test_demo_project_has_metrics_catalog(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    # Metrics catalog: at least the four seeded definitions, covering all kinds.
    metrics_resp = await client.get(f"/api/v1/projects/{slug}/metrics")
    assert metrics_resp.status_code == 200
    metrics = metrics_resp.json()["items"]
    assert len(metrics) >= 4, "Expected at least four seeded metric definitions"
    kinds = {metric["kind"] for metric in metrics}
    assert {"sql", "event_composition", "fact"} <= kinds, kinds

    # MetricValue rows render through the enriched list (latest value + spark).
    with_values = [metric for metric in metrics if metric["spark"]]
    assert with_values, "Expected at least one metric definition with collected values"

    # The conversion ratio is a fraction (purchases / screen views), not a count.
    conversion = next(metric for metric in metrics if metric["name"] == "purchase_conversion")
    assert conversion["kind"] == "event_composition"
    assert conversion["latest_value"] is not None
    assert 0.0 < conversion["latest_value"] < 1.0, conversion["latest_value"]


@pytest.mark.asyncio
async def test_demo_project_has_fact_table_with_named_filter(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    list_resp = await client.get(f"/api/v1/projects/{slug}/fact-tables")
    assert list_resp.status_code == 200
    fact_tables = list_resp.json()["items"]
    assert len(fact_tables) >= 1, "Expected at least one seeded fact table"

    fact_table_id = fact_tables[0]["id"]
    detail_resp = await client.get(f"/api/v1/projects/{slug}/fact-tables/{fact_table_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    filter_names = {row_filter["name"] for row_filter in detail["row_filters"]}
    assert "completed" in filter_names, filter_names


@pytest.mark.asyncio
async def test_demo_data_source_is_tested_healthy(client: AsyncClient) -> None:
    """The synthetic warehouse is stamped tested-healthy at seed time (issue .14).

    The in-memory adapter always answers, so a never-checked source would read as
    "untested" and drop out of the HEALTHY count / Overview badge for no real
    reason. Provisioning sets last_test_status=success (with last_test_at), and
    the source stays project-scoped so the Overview rail can filter it per project.
    """
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    async with TestSessionLocal() as session:
        project_id = await _project_id_for_slug(session, slug)
        source = (
            await session.execute(select(DataSource).where(DataSource.project_id == project_id))
        ).scalar_one()

    assert source.db_type == "synthetic"
    assert source.last_test_status == TestStatus.success
    assert source.last_test_at is not None
    assert source.project_id == project_id


@pytest.mark.asyncio
async def test_demo_fact_table_preview_serves_synthetic_orders(client: AsyncClient) -> None:
    """The fact-table preview endpoint reads real synthetic ``orders`` rows.

    The demo fact table is backed by a ``db_type='synthetic'`` DataSource, so the
    normal preview path (``POST /fact-tables/preview`` -> introspection service ->
    ``build_adapter`` -> ``SyntheticAdapter``) returns the in-memory orders rows
    and their bucketed column types with no network/filesystem access.
    """
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    fact_tables = (await client.get(f"/api/v1/projects/{slug}/fact-tables")).json()["items"]
    fact_table_id = fact_tables[0]["id"]
    detail = (await client.get(f"/api/v1/projects/{slug}/fact-tables/{fact_table_id}")).json()

    # Preview (used by the create/edit "Preview columns" surface AND schema
    # refresh) over the fact table's own SQL / data source.
    preview_resp = await client.post(
        f"/api/v1/projects/{slug}/fact-tables/preview",
        json={
            "data_source_id": detail["data_source_id"],
            "sql": detail["sql"],
            "timestamp_column": detail["timestamp_column"],
        },
    )
    assert preview_resp.status_code == 200, preview_resp.text
    preview = preview_resp.json()

    # The synthetic ``orders`` schema, bucketed by the introspection service.
    columns = {column["name"]: column["type"] for column in preview["columns"]}
    assert {"created_at", "amount", "currency", "user_id", "country", "status"} <= set(columns)
    assert columns["created_at"] == "timestamp"
    assert columns["amount"] == "number"
    assert columns["status"] == "string"
    # ``user_id`` carries an identifier signal and is offered as a candidate.
    assert "user_id" in preview["identifier_candidates"]

    # No rows come back. The preview answers the query's SHAPE and never its
    # contents: sample rows were returned to callers and
    # displayed by nothing, so the second warehouse query behind them is gone.
    # The column assertions above are what prove the synthetic adapter really
    # served this — they are the demo ``orders`` schema, not a fabrication.
    assert "sample_rows" not in preview


@pytest.mark.asyncio
async def test_create_demo_project_twice_unique_slugs(client: AsyncClient) -> None:
    resp1 = await client.post("/api/v1/projects/demo")
    resp2 = await client.post("/api/v1/projects/demo")
    assert resp1.status_code == 202
    assert resp2.status_code == 202
    assert resp1.json()["slug"] != resp2.json()["slug"]


@pytest.mark.asyncio
async def test_demo_project_viewer_cannot_create(anon_client: AsyncClient) -> None:
    # Register a new user (default role is viewer in a fresh instance with
    # an existing owner, but in tests the first registered user becomes owner
    # and subsequent ones are viewers).  Here we use anon_client which has no
    # session at all — the endpoint should return 401.
    resp = await anon_client.post("/api/v1/projects/demo")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_demo_project_cascades(client: AsyncClient) -> None:
    # A demo project is data-rich (event types, events, fields, metrics,
    # signals, drifts, scan configs). Deleting it must succeed and remove it.
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    del_resp = await client.delete(f"/api/v1/projects/{slug}")
    assert del_resp.status_code == 204
    assert (await client.get(f"/api/v1/projects/{slug}")).status_code == 404


@pytest.mark.asyncio
async def test_demo_project_seeds_enabled_anomaly_settings(client: AsyncClient) -> None:
    # The scan advertises anomaly_detection_enabled=True; detect.py only runs when
    # a matching project-level ProjectAnomalySettings row is ALSO enabled, so the
    # seeder must persist one or detection would silently never run.
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    async with TestSessionLocal() as session:
        project_id = await _project_id_for_slug(session, slug)
        settings = (
            await session.execute(
                select(ProjectAnomalySettings).where(
                    ProjectAnomalySettings.project_id == project_id
                )
            )
        ).scalar_one()

    assert settings.anomaly_detection_enabled is True
    # Gate scopes are on so the seeded event/type/project anomalies are what the
    # worker would produce.
    assert settings.detect_events is True
    assert settings.detect_project_total is True


# The seed clock decides which hour of the week every series starts on, and with
# it where the weekly promo's sends land. At Monday 22:00 UTC the promo hour is
# the trough of Paywall View's week, so the oldest send's usual volume (319) sits
# below every stored count (the send itself is stored at 3x, and the later
# same-hour buckets carry more upward drift). CI hit that hour once
# (tripl-lcg5); pinning the clock keeps the test off the wall clock and keeps
# that case covered. The second clock is an ordinary midweek afternoon.
_TROUGH_PROMO_CLOCK = datetime(2026, 10, 5, 22, tzinfo=UTC)
_MIDWEEK_CLOCK = datetime(2026, 10, 7, 14, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("clock", [_TROUGH_PROMO_CLOCK, _MIDWEEK_CLOCK], ids=["trough", "midweek"])
async def test_demo_project_anomalies_match_seeded_series(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, clock: datetime
) -> None:
    # Every seeded MetricAnomaly must be reproducible from the visible EventMetric
    # series. Two kinds are seeded at event scope: what the real detector found
    # over exactly the stored counts, and the weekly promo's past sends that
    # someone marked expected (hand-written, scored against the volume the hour
    # would have had without the promo). Each kind is checked against its own
    # derivation; the promo's ``expected`` is NOT bounded by the series, since
    # the series only ever stores the promo-lifted count for that hour.
    monkeypatch.setattr(demo_service, "_demo_clock", lambda: clock)
    promo_buckets = set(weekly_promo_buckets(clock - timedelta(hours=1)))
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    async with TestSessionLocal() as session:
        project_id = await _project_id_for_slug(session, slug)
        scan_config_id = await _scan_config_id_for_project(session, project_id)
        assert scan_config_id is not None

        all_anomalies = (
            (
                await session.execute(
                    select(MetricAnomaly).where(MetricAnomaly.scan_config_id == scan_config_id)
                )
            )
            .scalars()
            .all()
        )

        # Post-Wave-1 tuning, the seeded series is shaped to yield a SMALL number of
        # genuine anomalies (one visible spike per scope), not hundreds.
        assert 0 < len(all_anomalies) <= 12, len(all_anomalies)

        # The plan has a main and a feature branch, each with its own row.
        promo_event_ids = set(
            (
                await session.execute(
                    select(Event.id).where(
                        Event.project_id == project_id, Event.name == WEEKLY_PROMO_EVENT_NAME
                    )
                )
            )
            .scalars()
            .all()
        )
        event_anomalies = [a for a in all_anomalies if a.scope_type == SCOPE_EVENT]
        promo_verdicts = [
            a
            for a in event_anomalies
            if a.event_id in promo_event_ids and a.bucket.replace(tzinfo=UTC) in promo_buckets
        ]
        detected = [a for a in event_anomalies if a not in promo_verdicts]
        assert detected, "expected at least one detector event-scope anomaly"
        assert len(promo_verdicts) == len(promo_buckets)

        for anomaly in promo_verdicts:
            stored = (
                await session.execute(
                    select(EventMetric.count).where(
                        EventMetric.scan_config_id == scan_config_id,
                        EventMetric.event_id == anomaly.event_id,
                        EventMetric.bucket == anomaly.bucket,
                    )
                )
            ).scalar_one()
            # The stored count is the promo-lifted one; expected is its usual.
            assert anomaly.actual_count == float(stored)
            assert anomaly.expected_count * WEEKLY_PROMO_MULTIPLIER == float(stored)
            assert anomaly.direction == "spike"

        for anomaly in detected:
            assert anomaly.event_id is not None
            # The anomaly bucket exists in the event's stored series.
            metric = (
                await session.execute(
                    select(EventMetric).where(
                        EventMetric.scan_config_id == scan_config_id,
                        EventMetric.event_id == anomaly.event_id,
                        EventMetric.bucket == anomaly.bucket,
                    )
                )
            ).scalar_one()
            # actual_count is drawn straight from the stored count.
            assert anomaly.actual_count == float(metric.count)

            # expected_count is drawn from the same series (a phase median), so it
            # sits within the series' observed range, and a spike overshoots it.
            series_counts = (
                (
                    await session.execute(
                        select(EventMetric.count).where(
                            EventMetric.scan_config_id == scan_config_id,
                            EventMetric.event_id == anomaly.event_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert min(series_counts) <= anomaly.expected_count <= max(series_counts)
            assert anomaly.direction == "spike"
            assert anomaly.actual_count > anomaly.expected_count


@pytest.mark.asyncio
async def test_demo_project_distribution_drift_is_real_psi(client: AsyncClient) -> None:
    # Drift rows are computed by the real compute_psi over a genuinely shifting
    # platform mix, so PSI varies across buckets and each band matches the score.
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    async with TestSessionLocal() as session:
        project_id = await _project_id_for_slug(session, slug)
        scan_config_id = await _scan_config_id_for_project(session, project_id)
        drifts = (
            (
                await session.execute(
                    select(DistributionDrift)
                    .where(DistributionDrift.scan_config_id == scan_config_id)
                    .order_by(DistributionDrift.bucket)
                )
            )
            .scalars()
            .all()
        )

    assert len(drifts) >= 3, "expected a distribution-drift ladder"

    # A genuinely drifting mix produces varied PSI, not a hand-written constant.
    psi_values = [round(drift.psi, 4) for drift in drifts]
    assert len(set(psi_values)) > 1, psi_values
    # PSI climbs toward the most recent (newest) bucket as the mix drifts further.
    assert drifts[-1].psi > drifts[0].psi

    for drift in drifts:
        # Band is exactly what compute_psi's thresholds imply from the score.
        if drift.psi < PSI_BAND_MINOR:
            assert drift.band == "stable", (drift.psi, drift.band)
        elif drift.psi < PSI_BAND_SIGNIFICANT:
            assert drift.band == "minor", (drift.psi, drift.band)
        else:
            assert drift.band == "significant", (drift.psi, drift.band)
        # Top movers are real contributions from the seeded values.
        assert drift.top_movers
        mover_values = {mover["value"] for mover in drift.top_movers}
        assert {"web", "ios"} <= mover_values


# ---------------------------------------------------------------------------
# Recipe 4: real pending branch change + variable-value drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_recipe_version_is_pinned_to_4(client: AsyncClient) -> None:
    # The literal is deliberate: bumping the recipe must be a conscious edit here
    # too, so a stale demo (recipe < 4) is never mistaken for the current shape.
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    assert resp.json()["demo_recipe_version"] == "4"


async def _working_branch(session: AsyncSession, project_id: uuid.UUID) -> PlanBranch:
    return (
        await session.execute(
            select(PlanBranch).where(
                PlanBranch.project_id == project_id,
                PlanBranch.kind == BranchKind.working.value,
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_demo_branch_diff_is_exactly_one_modified_event(client: AsyncClient) -> None:
    # The seeded feature branch carries a REAL pending change: the plan is
    # deep-copied onto it and exactly one event description is edited, so the
    # diff (the merge preview's content) is non-empty and precisely scoped.
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    branches = (await client.get(f"/api/v1/projects/{slug}/branches")).json()["items"]
    feature = next(b for b in branches if b["name"] == "feature/checkout-funnel")

    diff_resp = await client.get(f"/api/v1/projects/{slug}/branches/{feature['id']}/diff")
    assert diff_resp.status_code == 200
    diff = diff_resp.json()
    assert diff["summary"] == {"added": 0, "removed": 0, "changed": 1, "housekeeping": 0}
    assert len(diff["entries"]) == 1, diff["entries"]

    entry = diff["entries"][0]
    assert entry["entity_type"] == "event"
    assert entry["kind"] == "changed"
    assert entry["name"] == "Buy Button Click"
    assert {fc["field"] for fc in entry["field_changes"]} == {"description"}
    # Main has not moved since the branch was cut.
    assert diff["behind_base"] is False

    # The branch holds a full plan copy (not just the one edited row), so a
    # merge preview has real content behind it: every main event has a branch
    # counterpart.
    async with TestSessionLocal() as session:
        project_id = await _project_id_for_slug(session, slug)
        branch = await _working_branch(session, project_id)
        main_count = (
            await session.execute(
                select(func.count(Event.id)).where(
                    Event.project_id == project_id,
                    Event.branch_id != branch.id,
                )
            )
        ).scalar_one()
        branch_count = (
            await session.execute(select(func.count(Event.id)).where(Event.branch_id == branch.id))
        ).scalar_one()
    assert branch_count == main_count
    assert branch_count > 0


@pytest.mark.asyncio
async def test_demo_seeds_one_open_variable_value_drift(client: AsyncClient) -> None:
    # prod_weekly is observed OUTSIDE the documented override list for
    # product_id on Trial Started ([prod_monthly, prod_annual]), so the
    # variables drift UI (list_value_drifts / open drift counts / event drift
    # badge) has a real open row behind it. It never feeds the firing rule's
    # replay, but only because its scan_config_id is NULL and both the live
    # loader and the replay twin require one to be set — the replay HAS read
    # variable-value drifts since the replay learned to load them.
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    async with TestSessionLocal() as session:
        project_id = await _project_id_for_slug(session, slug)
        drifts = (
            (
                await session.execute(
                    select(VariableValueDrift).where(VariableValueDrift.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(drifts) == 1, drifts
        drift = drifts[0]
        assert drift.status == SCHEMA_DRIFT_STATUS_OPEN
        assert drift.observed_values == ["prod_weekly"]
        assert drift.variable_name == "product_id"
        assert drift.event_name == "Trial Started"
        # The observed value sits outside the documented override list.
        assert not set(drift.observed_values) & {"prod_monthly", "prod_annual"}
        assert drift.resolved_at is None
        assert drift.detected_at is not None


# A fixed clock for the determinism fixtures (mirrors test_demo_scenario).
_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


async def _seed_fixture(session: AsyncSession, slug: str) -> uuid.UUID:
    """Provision a demo shell + seed the full scenario at a fixed clock/seed."""
    project = Project(
        name="Demo Project",
        slug=slug,
        description="determinism fixture",
        is_demo=True,
        generation_status=ProjectGenerationStatus.seeding.value,
        generation_stage="init",
    )
    session.add(project)
    await session.flush()
    branch_id = await plan_branch_service.ensure_main_branch_id(session, project.id)
    await session.commit()
    ctx = DemoContext(
        project_id=project.id,
        branch_id=branch_id,
        slug=slug,
        now=_FIXED_NOW,
        seed=DEMO_SEED,
    )
    await seed_demo_content(session, ctx)
    await session.commit()
    return project.id


async def _drift_shape(session: AsyncSession, project_id: uuid.UUID) -> list[tuple]:
    rows = (
        (
            await session.execute(
                select(VariableValueDrift).where(VariableValueDrift.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    return sorted(
        (
            drift.variable_name,
            drift.event_name,
            tuple(drift.observed_values),
            drift.status,
            drift.detected_at,
        )
        for drift in rows
    )


async def _branch_diff_shape(session: AsyncSession, slug: str, project_id: uuid.UUID) -> tuple:
    branch = await _working_branch(session, project_id)
    diff = await plan_branch_service.diff_branch(session, slug, branch.id)
    entries = sorted(
        (entry.entity_type, entry.kind, entry.parent, entry.name, tuple(entry.changes))
        for entry in diff.entries
    )
    return entries, diff.summary, diff.behind_base


@pytest.mark.asyncio
async def test_demo_branch_change_and_drift_are_deterministic() -> None:
    # Same clock + seed => identical drift rows and an identical branch diff:
    # the pending change and the drift derive from the recipe, never from
    # per-run randomness.
    async with TestSessionLocal() as session:
        first_id = await _seed_fixture(session, "demo-branchA")
        second_id = await _seed_fixture(session, "demo-branchB")

        first_drift = await _drift_shape(session, first_id)
        second_drift = await _drift_shape(session, second_id)
        assert first_drift == second_drift
        assert len(first_drift) == 1

        first_diff = await _branch_diff_shape(session, "demo-branchA", first_id)
        second_diff = await _branch_diff_shape(session, "demo-branchB", second_id)
        assert first_diff == second_diff
        entries, summary, behind_base = first_diff
        assert summary == {"added": 0, "removed": 0, "changed": 1, "housekeeping": 0}
        assert entries[0][1] == "changed"
        assert entries[0][3] == "Buy Button Click"
        assert behind_base is False


@pytest.mark.asyncio
async def test_reset_purges_branch_plan_entities_and_drift(client: AsyncClient) -> None:
    # Reset drops the old demo in full — including the branch-side plan copy and
    # the variable-value drift — and re-seeds the same shape under a fresh id.
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    slug = resp.json()["slug"]

    async with TestSessionLocal() as session:
        old_project_id = await _project_id_for_slug(session, slug)
        old_branch = await _working_branch(session, old_project_id)
        old_branch_id = old_branch.id
        old_branch_events = (
            await session.execute(
                select(func.count(Event.id)).where(Event.branch_id == old_branch_id)
            )
        ).scalar_one()
        assert old_branch_events > 0

    reset_resp = await client.post(f"/api/v1/projects/demo/{slug}/reset")
    assert reset_resp.status_code == 200

    async with TestSessionLocal() as session:
        new_project_id = await _project_id_for_slug(session, slug)
        assert new_project_id != old_project_id

        # Old rows are gone: branches, their plan copies, and the drift.
        for count_query in (
            select(func.count(PlanBranch.id)).where(PlanBranch.project_id == old_project_id),
            select(func.count(Event.id)).where(Event.branch_id == old_branch_id),
            select(func.count(VariableValueDrift.id)).where(
                VariableValueDrift.project_id == old_project_id
            ),
        ):
            assert (await session.execute(count_query)).scalar_one() == 0

        # And the fresh seed carries the same recipe-4 shape again.
        new_branch = await _working_branch(session, new_project_id)
        new_branch_events = (
            await session.execute(
                select(func.count(Event.id)).where(Event.branch_id == new_branch.id)
            )
        ).scalar_one()
        assert new_branch_events > 0
        new_drifts = (
            await session.execute(
                select(func.count(VariableValueDrift.id)).where(
                    VariableValueDrift.project_id == new_project_id
                )
            )
        ).scalar_one()
        assert new_drifts == 1


async def _seeded_metric_anomaly(
    session: AsyncSession, project_id: uuid.UUID
) -> tuple[MetricAnomaly, MetricDefinition]:
    """The project's one seeded ``metric``-scope anomaly and its definition."""
    metric_ids = {
        str(metric_id)
        for metric_id in (
            await session.execute(
                select(MetricDefinition.id).where(MetricDefinition.project_id == project_id)
            )
        )
        .scalars()
        .all()
    }
    rows = (
        (
            await session.execute(
                select(MetricAnomaly).where(
                    MetricAnomaly.scope_type == MetricScopeType.metric.value,
                    MetricAnomaly.scan_config_id.is_(None),
                    MetricAnomaly.scope_ref.in_(metric_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, f"expected exactly one seeded metric anomaly, got {len(rows)}"
    metric = await session.get(MetricDefinition, uuid.UUID(rows[0].scope_ref))
    assert metric is not None
    return rows[0], metric


def _same_instant(left: datetime, right: datetime) -> bool:
    """Compare two datetimes that may differ only in tz-awareness (SQLite)."""
    return left.replace(tzinfo=None) == right.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_seeded_scan_history_runs_are_internally_consistent() -> None:
    """Each backfilled run reports its OWN hour, rows and wall time.

    They used to be constants: three consecutive hourly runs all claimed the
    same window (anchored to the seed instant, so the older two "scanned" an
    hour that had not happened yet), byte-identical millions of rows and an
    identical 42.0s — next to a real Run now reporting ~30K rows in ~3s.
    """
    async with TestSessionLocal() as session:
        project_id = await _seed_fixture(session, "demo-scanhistory")
        scan_config_id = await _scan_config_id_for_project(session, project_id)
        jobs = (
            (
                await session.execute(
                    select(ScanJob)
                    .where(
                        ScanJob.scan_config_id == scan_config_id,
                        ScanJob.status == ScanJobStatus.completed.value,
                    )
                    .order_by(ScanJob.started_at)
                )
            )
            .scalars()
            .all()
        )
        totals = (
            (
                await session.execute(
                    select(EventMetric.bucket, func.sum(EventMetric.count))
                    .where(
                        EventMetric.scan_config_id == scan_config_id,
                        EventMetric.event_type_id.is_not(None),
                    )
                    .group_by(EventMetric.bucket)
                )
            )
            .tuples()
            .all()
        )

    assert len(jobs) >= 3
    seeded_hour_totals = {bucket: total for bucket, total in totals}
    windows: set[tuple[str, str]] = set()
    rows_seen: set[int] = set()
    for job in jobs:
        summary = job.result_summary or {}
        window_from = datetime.fromisoformat(str(summary["scan_window_from"]))
        window_to = datetime.fromisoformat(str(summary["scan_window_to"]))
        windows.add((str(summary["scan_window_from"]), str(summary["scan_window_to"])))
        rows_seen.add(int(summary["scan_rows_processed"]))

        # A run can only have scanned a window that had already closed. (SQLite
        # hands datetimes back naive; the recipe stores UTC.)
        started_at = job.started_at.replace(tzinfo=window_to.tzinfo)
        assert window_to <= started_at, (window_to, started_at)
        assert window_to - window_from == timedelta(hours=1)
        # And it reports the volume the seeded warehouse holds for that hour.
        bucket = next(key for key in seeded_hour_totals if _same_instant(key, window_from))
        assert int(summary["scan_rows_processed"]) == seeded_hour_totals[bucket]

    assert len(windows) == len(jobs), "each run must report its own window"
    assert len(rows_seen) == len(jobs), "each run must report its own row count"
    durations = [job.completed_at - job.started_at for job in jobs]
    # The reported defect was three consecutive runs stamped with a byte-identical
    # constant; two real runs may legitimately land on the same tenth of a second.
    assert len(set(durations)) > 1, (
        f"backfilled runs must not all report the same wall time, got {durations}"
    )
    assert all(timedelta(seconds=1) < duration < timedelta(seconds=30) for duration in durations), (
        f"backfilled runs must land in the same band as a real Run now, got {durations}"
    )


@pytest.mark.asyncio
async def test_seeded_metric_anomaly_sits_on_the_metrics_own_bucket_grid() -> None:
    """The seeded catalog-metric spike is on-grid and scored against a baseline.

    It used to be written at ``now - 1 day`` with the hour preserved, which put a
    half-day-offset point inside a 1-day series, and its ``expected`` was the
    newest stored value — the current PARTIAL period, i.e. the lowest point on
    the chart.
    """
    async with TestSessionLocal() as session:
        project_id = await _seed_fixture(session, "demo-metricanom")
        anomaly, metric = await _seeded_metric_anomaly(session, project_id)
        values = (
            (
                await session.execute(
                    select(MetricValue.bucket, MetricValue.value)
                    .where(MetricValue.metric_definition_id == metric.id)
                    .order_by(MetricValue.bucket)
                )
            )
            .tuples()
            .all()
        )
        # Deterministic pick: the first SCOREABLE catalog metric by display
        # order, not a Postgres tie-break over four identical created_at values.
        second_project_id = await _seed_fixture(session, "demo-metricanom2")
        _second_anomaly, second_metric = await _seeded_metric_anomaly(session, second_project_id)
    assert second_metric.name == metric.name

    by_bucket = {bucket: value for bucket, value in values}
    # ON an existing stored bucket, so the chart gains no extra point...
    assert anomaly.bucket in by_bucket
    # ...and on the metric's own interval grid. (An event_composition metric
    # inherits the scan config's interval and stores ``interval`` as NULL.)
    if metric.interval is not None:
        assert anomaly.bucket == floor_to_bucket(anomaly.bucket, str(metric.interval))
    # Reports the stored value, scored against the series baseline (not the
    # newest, still-filling bucket).
    assert anomaly.actual_count == pytest.approx(by_bucket[anomaly.bucket])
    assert anomaly.expected_count < anomaly.actual_count
    assert anomaly.bucket != max(by_bucket)
    assert anomaly.expected_count == pytest.approx(
        median(by_bucket[b] for b in sorted(by_bucket)[:-1])
    )


@pytest.mark.asyncio
async def test_a_planned_promo_covers_the_seeded_metric_spike() -> None:
    """F18's done-when: a planned window suppresses a seeded spike in the demo.

    The catalog-metric spike is stored and tagged with the promo that expected
    it, so the chart draws it inside the window while no simulated firing,
    open rule state or delivery item names it.
    """
    async with TestSessionLocal() as session:
        project_id = await _seed_fixture(session, "demo-planned")
        anomaly, _metric = await _seeded_metric_anomaly(session, project_id)
        planned = (
            await session.execute(select(PlannedEvent).where(PlannedEvent.project_id == project_id))
        ).scalar_one()
        assert planned.label == DEMO_PLANNED_EVENT_LABEL
        assert anomaly.planned_event_id == planned.id
        assert planned.starts_at <= anomaly.bucket.replace(tzinfo=planned.starts_at.tzinfo)
        assert anomaly.bucket.replace(tzinfo=planned.ends_at.tzinfo) < planned.ends_at
        assert (planned.scope_type, planned.scope_ref) == ("metric", anomaly.scope_ref)

        metric_states = (
            await session.execute(
                select(func.count())
                .select_from(AlertRuleState)
                .where(AlertRuleState.scope_ref == anomaly.scope_ref)
            )
        ).scalar_one()
        assert metric_states == 0


@pytest.mark.asyncio
async def test_demo_variables_document_their_allowed_values() -> None:
    """``product_id`` ships a documented value list.

    The coached "Variables & value drift" chapter tells the user to "compare
    observed values against the documented list", but every demo variable had an
    empty ``allowed_values``, so the column read "—".
    """
    async with TestSessionLocal() as session:
        project_id = await _seed_fixture(session, "demo-allowedvalues")
        variables = (
            (
                await session.execute(
                    select(Variable)
                    .where(Variable.project_id == project_id)
                    .order_by(Variable.name)
                )
            )
            .scalars()
            .all()
        )
    by_name = {variable.name: variable for variable in variables}
    assert by_name["product_id"].allowed_values == [
        "prod_monthly",
        "prod_annual",
        "prod_lifetime",
    ]
    # The seeded drift value must sit OUTSIDE the documented list, or the
    # chapter's "a scan saw prod_weekly outside the documented values" is false.
    assert not set(DRIFT_OBSERVED_VALUES) & set(by_name["product_id"].allowed_values)
    # Unbounded identifiers stay undocumented — a list there would be a lie.
    assert by_name["user_id"].allowed_values == []
    assert by_name["session_id"].allowed_values == []


@pytest.mark.asyncio
async def test_demo_event_field_values_are_authored() -> None:
    """The recipe's field values are hand-authored, so a scan must not rewrite them.

    Seeded unauthored, the demo's own guided first scan replaced the documented
    ``${product_id}`` / ``${platform}`` templates with whatever literal the
    synthetic warehouse emitted, and the seeded variable value contexts — which
    describe exactly those templates — were dropped with them.
    """
    async with TestSessionLocal() as session:
        project_id = await _seed_fixture(session, "demo-authoredvalues")
        field_values = (
            (
                await session.execute(
                    select(EventFieldValue)
                    .join(Event, Event.id == EventFieldValue.event_id)
                    .where(Event.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )

    assert field_values
    assert all(field_value.is_authored for field_value in field_values)
    # The variable templates the coached chapter points at are among them.
    assert {"${product_id}", "${platform}"} <= {field_value.value for field_value in field_values}


@pytest.mark.asyncio
async def test_demo_scan_config_does_not_expose_non_catalog_columns() -> None:
    """The demo scan projects only the columns the curated plan models.

    A ``SELECT *`` scan handed ``user_id``/``session_id`` (the active-sessions
    metric's columns) to the hourly catalog sync, which auto-created USER_ID and
    SESSION_ID FieldDefinitions on every event type and filled the curated events
    table with raw sample values like ``s29_5``.
    """
    async with TestSessionLocal() as session:
        project_id = await _seed_fixture(session, "demo-scancolumns")
        config = (
            await session.execute(select(ScanConfig).where(ScanConfig.project_id == project_id))
        ).scalar_one()
        data_source = await session.get(DataSource, config.data_source_id)
    assert data_source is not None

    adapter = build_adapter(data_source)
    try:
        exposed = {column.name for column in adapter.get_columns(config.base_query)}
    finally:
        adapter.close()

    assert "user_id" not in exposed
    assert "session_id" not in exposed
    # Everything the plan models (plus the reserved metric dimensions and the
    # identity column the group rules key on) is still there.
    assert {
        "event_time",
        "event_type",
        "event_name",
        "screen_name",
        "platform",
        "button_id",
        "product_id",
        "amount",
        "currency",
        "app_version",
    } <= exposed


# ── Provisioning guardrails: cancel, per-creator cap, failed-shell sweep ──────


@pytest.mark.asyncio
async def test_cancel_request_reports_nothing_to_cancel_when_idle(client: AsyncClient) -> None:
    """No in-flight provision means the caller must be told plainly, not lied to."""
    resp = await client.post("/api/v1/projects/demo/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": False, "slug": None, "state": "none"}


@pytest.mark.asyncio
async def test_cancel_request_flags_a_seeding_shell(client: AsyncClient) -> None:
    """A shell still `seeding` is flagged so phase 2 abandons itself."""
    me = (await client.get("/api/v1/auth/me")).json()
    async with TestSessionLocal() as session:
        shell = Project(
            name="Demo Project",
            slug="demo-inflight",
            is_demo=True,
            generation_status=ProjectGenerationStatus.seeding.value,
            generation_stage="init",
            created_by_user_id=uuid.UUID(me["id"]),
        )
        session.add(shell)
        await session.commit()

    resp = await client.post("/api/v1/projects/demo/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": True, "slug": "demo-inflight", "state": "stopped"}

    async with TestSessionLocal() as session:
        stage = await session.scalar(
            select(Project.generation_stage).where(Project.slug == "demo-inflight")
        )
    assert stage == demo_service.DEMO_CANCEL_REQUESTED_STAGE


@pytest.mark.asyncio
async def test_cancelled_provision_deletes_its_shell_instead_of_promoting(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression for a cancelled demo: a cancel must leave NO project behind.

    Before the fix the create ran to completion regardless and a fully-seeded
    demo appeared seconds after the user abandoned it. The cancel arrives from a
    second request mid-seed; here the monkeypatched seeder stands in for that
    concurrent write so the assertion is on what phase 2 does with the flag.
    """
    real_seed = demo_service._seed_demo_content

    async def cancelling_seed(session: AsyncSession, **kwargs: object) -> None:
        await real_seed(session, **kwargs)  # type: ignore[arg-type]
        await session.execute(
            update(Project)
            .where(Project.id == kwargs["project_id"])
            .values(generation_stage=demo_service.DEMO_CANCEL_REQUESTED_STAGE)
        )

    monkeypatch.setattr(demo_service, "_seed_demo_content", cancelling_seed)

    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 409

    async with TestSessionLocal() as session:
        remaining = (
            (await session.execute(select(Project).where(Project.is_demo.is_(True))))
            .scalars()
            .all()
        )
    assert remaining == []


@pytest.mark.asyncio
async def test_demo_creation_is_capped_per_creator(client: AsyncClient) -> None:
    """The (N+1)-th demo is refused with a message that points at reset/delete."""
    for _ in range(demo_service.MAX_DEMOS_PER_CREATOR):
        assert (await client.post("/api/v1/projects/demo")).status_code == 202

    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 409
    assert "Reset or delete" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_repeated_demos_get_distinguishable_names(client: AsyncClient) -> None:
    """Two demos must not be two identically-titled cards."""
    first = (await client.post("/api/v1/projects/demo")).json()
    second = (await client.post("/api/v1/projects/demo")).json()
    assert first["name"] == "Demo Project"
    assert second["name"] == "Demo Project 2"


@pytest.mark.asyncio
async def test_reset_keeps_the_demos_name(client: AsyncClient) -> None:
    """A reset refreshes content, so the numbered name must survive it."""
    await client.post("/api/v1/projects/demo")
    second = (await client.post("/api/v1/projects/demo")).json()

    resp = await client.post(f"/api/v1/projects/demo/{second['slug']}/reset")
    assert resp.status_code == 200
    assert resp.json()["name"] == second["name"]


@pytest.mark.asyncio
async def test_stale_failed_shells_are_swept_on_the_next_create(client: AsyncClient) -> None:
    """Failed shells stop accumulating forever."""
    async with TestSessionLocal() as session:
        old = Project(
            name="Demo Project",
            slug="demo-oldfail",
            is_demo=True,
            generation_status=ProjectGenerationStatus.failed.value,
            generation_error="Demo provisioning failed during seeding (IntegrityError).",
            created_at=datetime.now(UTC)
            - timedelta(days=demo_service.FAILED_SHELL_RETENTION_DAYS + 1),
        )
        recent = Project(
            name="Demo Project",
            slug="demo-newfail",
            is_demo=True,
            generation_status=ProjectGenerationStatus.failed.value,
            generation_error="Demo provisioning failed during seeding (IntegrityError).",
        )
        session.add_all([old, recent])
        await session.commit()

    assert (await client.post("/api/v1/projects/demo")).status_code == 202

    async with TestSessionLocal() as session:
        slugs = set(
            (
                await session.scalars(
                    select(Project.slug).where(
                        Project.generation_status == ProjectGenerationStatus.failed.value
                    )
                )
            ).all()
        )
    assert "demo-oldfail" not in slugs
    assert "demo-newfail" in slugs


# ── Explorability of the surfaces the docs promise ───────────────────────────


@pytest.mark.asyncio
async def test_demo_surfaces_its_planted_dead_event(client: AsyncClient) -> None:
    """Coverage claimed zero gaps while the catalog showed a stale event.

    The recipe deliberately ages one event's warehouse volume out, but the
    dead-events query also requires ``created_at < cutoff`` (a grace period), and
    every demo event was staggered INSIDE the 30-day window — so the planted
    example was permanently unflaggable.
    """
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]

    resp = await client.get(f"/api/v1/projects/{slug}/reconciliation/dead-events?days=30")
    assert resp.status_code == 200
    names = [item["name"] for item in resp.json()["items"]]
    assert "Subscription Cancelled" in names


@pytest.mark.asyncio
async def test_demo_seeds_a_retryable_failed_delivery(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Audit table only offers Retry on a failed row."""
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]

    resp = await client.get(f"/api/v1/projects/{slug}/alert-deliveries")
    assert resp.status_code == 200
    deliveries = resp.json()["items"]
    failed = [row for row in deliveries if row["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["error_message"]

    # ...and it is a row Retry can actually re-send. ``send_alert_delivery``
    # re-renders the message from ``delivery.items`` while the header it writes
    # counts ``matched_count``, so a failed row seeded with no items of its own
    # came back from the one Retry this demo exists to demonstrate reading
    # "sent", announcing N signals, with nothing underneath.
    # Asserted on the DETAIL endpoint because that is the payload the Audit
    # row expands into, and the number the reader compares against the header.
    detail = await client.get(f"/api/v1/projects/{slug}/alert-deliveries/{failed[0]['id']}")
    assert detail.status_code == 200
    assert len(detail.json()["items"]) == failed[0]["matched_count"] > 0

    # Retry enqueues the real dispatch task. Stub ``.delay`` so the suite keeps
    # its no-broker contract (CONTRIBUTING: pytest needs no RabbitMQ) — the same
    # pattern test_alerting.py uses — while still asserting the endpoint resolves
    # and hands this delivery to the worker.
    #
    # The celery app is imported FIRST for the same reason the service does it
    # (services/_alerting_deliveries.py): the task graph is cyclic, so entering
    # at ``tasks.alerts`` in a process that has not loaded the app yet lands
    # mid-cycle and raises ImportError.
    import tripl.worker.celery_app  # noqa: F401
    from tripl.worker.tasks.alerts import send_alert_delivery

    enqueued: list[str] = []
    monkeypatch.setattr(send_alert_delivery, "delay", lambda did: enqueued.append(did))

    # …and the Retry the docs promise actually resolves for it.
    retry = await client.post(f"/api/v1/projects/{slug}/alert-deliveries/{failed[0]['id']}/retry")
    assert retry.status_code == 200
    assert enqueued == [failed[0]["id"]]


@pytest.mark.asyncio
async def test_demo_audit_log_is_not_empty_out_of_the_box(client: AsyncClient) -> None:
    """A fresh demo used to land on "No audit entries yet"."""
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]

    resp = await client.get(f"/api/v1/projects/{slug}/audit?limit=200")
    assert resp.status_code == 200
    entries = resp.json()["items"]
    assert entries

    actions = {entry["action"] for entry in entries}
    # Every seeded action is one the Audit tab's filter offers, so the trail is
    # filterable rather than only visible.
    assert {"event_type.create", "variable.create", "alert_rule.create"} <= actions
    # Attributed to the demo's creator, not to nobody.
    assert all(entry["user_email"] for entry in entries)


@pytest.mark.asyncio
async def test_demo_audit_trail_covers_the_events_it_authored(client: AsyncClient) -> None:
    """Events lead the Audit tab's filter, and on a demo holding eighteen of them
    that group matched nothing — the log implied nobody had ever created an event
    on the project.

    The rows are derived, not invented: a creation is dated from the event's own
    ``created_at``, an edit from the ``EventChange`` the activity builder seeded,
    a dismissal from the candidate's ``resolved_at``. So what is worth pinning is
    not a row count but the coherence that derivation buys — one creation per
    event, every edit after the creation it edits, and nothing dated ahead of the
    seed instant.
    """
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]

    listed = await client.get(f"/api/v1/projects/{slug}/audit?limit=200")
    assert listed.status_code == 200
    entries = listed.json()["items"]

    def parsed(value: str) -> datetime:
        # The suite runs on sqlite, which drops the offset on a timezone-aware
        # column, so these come back naive here and aware on Postgres. The
        # builders normalise the same way (core.bucketing.to_utc).
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)

    at = {entry["id"]: parsed(entry["created_at"]) for entry in entries}

    created = [entry for entry in entries if entry["action"] == "event.create"]
    # MAIN edits only. The recipe also authors one edit ON the feature branch
    # (branches.CHANGED_EVENT_NAME), and that row targets the branch's COPY of
    # the event — a row the deep copy created, not an authored ``event.create``
    # — so the creation-precedes-edit check below cannot reach it.
    updated = [
        entry for entry in entries if entry["action"] == "event.update" and not entry["branch_name"]
    ]
    assert created, "the Events filter group would match nothing"
    assert updated, "no edit was recorded for the events the recipe edited"
    assert "shadow_event.dismiss" in {entry["action"] for entry in entries}

    # ONE creation per event, and the guard has to be on the NAME. The branches
    # builder deep-copies the whole plan onto a feature branch before the audit
    # builder runs, and the copies carry fresh uuids but the SAME names — so a
    # project-wide select would file a second row per event that a unique-id
    # check would wave straight through.
    creation_at = {entry["target_id"]: at[entry["id"]] for entry in created}
    assert len(creation_at) == len(created)
    assert len({entry["target_name"] for entry in created}) == len(created)
    assert all(entry["target_name"] for entry in created)

    # An event cannot be edited before it exists. This is what breaks if the
    # creation rows are ever dated from the seed instant instead of the event:
    # the stagger puts events up to ~3 weeks back, the edits days back.
    for entry in updated:
        assert entry["target_id"] in creation_at, entry
        assert creation_at[entry["target_id"]] < at[entry["id"]], entry

    # Nothing in the future, on any row: a seeded row that forgets its explicit
    # created_at silently lands at transaction_timestamp, i.e. after everything.
    assert max(at.values()) <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_demo_does_not_scope_the_data_source_entry_to_the_project(
    client: AsyncClient,
) -> None:
    """A data source is a workspace resource, and api/v1/data_sources.py records
    its actions with no project at all — which is exactly why the Audit tab's
    filter excludes ``data_source.*``.

    The recipe used to seed that one row WITH a project, so the demo was the only
    place in the product where that shape existed: a row sitting in the feed that
    no filter option could ever isolate. The demo's warehouse is
    project-OWNED so it is cleaned up with the project, but that is a cascade
    detail, not an audit scope.
    """
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]

    listed = await client.get(f"/api/v1/projects/{slug}/audit?limit=200")
    actions = {entry["action"] for entry in listed.json()["items"]}

    assert "data_source.create" not in actions
    # The scan config IS a project resource and its real route records it as one,
    # so it must still be there — the fix is about matching each route, not about
    # hiding the recipe's setup work.
    assert "scan_config.create" in actions

    # Absence alone would also pass if the row had been dropped altogether, which
    # is a different change with a different meaning. It still exists, unscoped —
    # exactly what api/v1/data_sources.py writes.
    unscoped = await org_audit("data_source.create")
    # The name the warehouse builder really gave the row, read from the same
    # helper it used: the audit entry restated a bare "Demo warehouse" literal
    # until it was fixed, naming a source that exists under no such name.
    rows = [entry for entry in unscoped if entry["target_name"] == demo_data_source_name(slug)]
    assert len(rows) == 1, rows
    assert rows[0]["project_id"] is None
    assert rows[0]["project_slug"] == ""


@pytest.mark.asyncio
async def test_resetting_a_demo_does_not_stack_the_previous_trail(client: AsyncClient) -> None:
    """A reset destroys the project and seeds a replacement under the SAME slug,
    and the audit list filters on the slug — so the old project's rows used to
    reattach to the new one.

    Two resets and the tab claimed every event had been created three times, by a
    project that no longer exists. The rows were not being preserved, they were
    being misattributed.
    """
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]
    replaced_id = (await client.get(f"/api/v1/projects/{slug}")).json()["id"]

    def creations(items: list[dict]) -> int:
        return sum(1 for entry in items if entry["action"] == "event.create")

    before = creations(
        (await client.get(f"/api/v1/projects/{slug}/audit?limit=200")).json()["items"]
    )
    assert before

    reset = await client.post(f"/api/v1/projects/demo/{slug}/reset")
    assert reset.status_code == 200, reset.text

    after = (await client.get(f"/api/v1/projects/{slug}/audit?limit=200")).json()["items"]
    assert creations(after) == before

    # Asserted on the UNFILTERED feed, deliberately, and by COUNT. Since
    # a later change the project filter resolves the slug to whichever project
    # owns it now, so it hides the replaced demo's rows whether or not they were
    # deleted — the assertion above would pass with the purge removed. And the
    # rows cannot be recognised by the id they used to carry either: deleting the
    # project nulls it (ON DELETE SET NULL, which the suite enforces). What
    # survives the purge is a second full generation of the trail, so counting is
    # what catches it.
    everything = await org_audit()
    assert creations(everything) == before
    assert all(entry["project_id"] != replaced_id for entry in everything)

    # The demo's own data-source row carries no project, so the id-scoped delete
    # cannot see it — it is found by the warehouse it names instead. Left behind
    # it would outlive the DataSource the same reset destroys, and every reset
    # would add another creation of a warehouse that no longer exists.
    unscoped = await org_audit("data_source.create")
    warehouses = [
        entry for entry in unscoped if entry["target_name"] == demo_data_source_name(slug)
    ]
    assert len(warehouses) == 1, warehouses


@pytest.mark.asyncio
async def test_a_shell_abandoned_mid_seed_never_locks_out_the_creator(
    client: AsyncClient,
) -> None:
    """A crash between the phase-1 commit and the outcome must not eat a cap slot.

    The shell is hidden from every list, so a user could neither see nor delete
    it — a stalled one has to stop counting and get reclaimed.
    """
    me = (await client.get("/api/v1/auth/me")).json()
    stalled_age = timedelta(hours=demo_service.STALLED_SEEDING_HOURS + 1)
    async with TestSessionLocal() as session:
        session.add_all(
            [
                Project(
                    name="Demo Project",
                    slug=f"demo-stalled{index}",
                    is_demo=True,
                    generation_status=ProjectGenerationStatus.seeding.value,
                    created_by_user_id=uuid.UUID(me["id"]),
                    created_at=datetime.now(UTC) - stalled_age,
                )
                for index in range(demo_service.MAX_DEMOS_PER_CREATOR)
            ]
        )
        await session.commit()

    assert (await client.post("/api/v1/projects/demo")).status_code == 202

    async with TestSessionLocal() as session:
        left = (
            await session.scalars(select(Project.slug).where(Project.slug.like("demo-stalled%")))
        ).all()
    assert list(left) == []


@pytest.mark.asyncio
async def test_every_seeded_scan_config_survives_a_patch_of_itself(client: AsyncClient) -> None:
    """The seeder must not write what the write path refuses to accept.

    It inserts through the ORM, so it never meets ``check_scalar_columns_unreserved``.
    It used to put ``platform`` in both ``metric_breakdown_columns`` and
    ``distribution_drift_fields`` while ``platform_column`` designated the same
    column, which that check forbids — so the demo shipped a scan config the API
    would not save, and renaming the demo scan returned a 422 naming fields the
    user had never touched.

    A no-op PATCH is the cheapest general guard: it re-validates the stored row
    against the rules a user's own edit meets, so any future drift between the
    seeder and the write path fails here rather than in front of an evaluator.
    """
    demo = await client.post("/api/v1/projects/demo")
    assert demo.status_code == 202
    slug = demo.json()["slug"]

    scans = await client.get(f"/api/v1/projects/{slug}/scans")
    assert scans.status_code == 200
    configs = scans.json()
    assert configs, "the demo is expected to seed at least one scan config"

    for config in configs:
        # Send the name it already has: nothing changes, but everything is checked.
        resp = await client.patch(
            f"/api/v1/projects/{slug}/scans/{config['id']}",
            json={"name": config["name"]},
        )
        assert resp.status_code == 200, (
            f"the seeded scan config {config['name']!r} cannot be saved by the API "
            f"that owns it: {resp.json()}"
        )
