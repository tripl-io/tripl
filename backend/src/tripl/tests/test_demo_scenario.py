"""Scenario-builder tests.

Covers the new plan/catalog examples added by the declarative demo scenario
(meta values, event-type relation + owner, authored variable override, event
change history, figma spec, feature-branch journey), the determinism guarantee
(same clock + seed => identical metric/anomaly shape), and an independently
testable single builder.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services import plan_branch_service
from tripl.services.demo import (
    DEMO_RECIPE_VERSION,
    DEMO_SEED,
    DemoContext,
    noise,
    seed_demo_content,
)
from tripl.services.demo.builders import plan as plan_builder
from tripl.tests.conftest import TestSessionLocal

# A fixed clock for the deterministic / builder-level tests.
_FIXED_NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# API visibility of the new examples
# ---------------------------------------------------------------------------


def _events_by_name(items: list[dict]) -> dict[str, dict]:
    return {item["name"]: item for item in items}


@pytest.mark.asyncio
async def test_new_examples_are_api_visible(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    # --- events + meta VALUES ------------------------------------------------
    events_resp = await client.get(f"/api/v1/projects/{slug}/events")
    assert events_resp.status_code == 200
    items = events_resp.json()["items"]
    by_name = _events_by_name(items)

    # At least one event carries meta values; Purchase Completed has jira + team.
    assert any(item["meta_values"] for item in items), "expected meta values on some event"
    purchase_meta = {mv["value"] for mv in by_name["Purchase Completed"]["meta_values"]}
    assert {"PAY-1234", "Payments"} <= purchase_meta, purchase_meta
    assert by_name["Home Screen View"]["meta_values"], "expected meta value on Home Screen View"

    home_id = by_name["Home Screen View"]["id"]
    paywall_id = by_name["Paywall View"]["id"]

    # --- event change history ------------------------------------------------
    history_resp = await client.get(f"/api/v1/projects/{slug}/events/{home_id}/history")
    assert history_resp.status_code == 200
    history = history_resp.json()
    assert len(history) >= 3, history
    fields_changed = {row["field"] for row in history}
    assert "status" in fields_changed, fields_changed
    # The demo creator is a real user, so their email surfaces on the edits.
    assert any(row["user_email"] == "test@example.com" for row in history)

    # --- figma spec (external_url, no stored bytes) --------------------------
    photos_resp = await client.get(f"/api/v1/projects/{slug}/events/{paywall_id}/photos")
    assert photos_resp.status_code == 200
    photos = photos_resp.json()
    figma = [p for p in photos if p["kind"] == "figma"]
    assert len(figma) == 1, photos
    assert figma[0]["external_url"] and "figma.com" in figma[0]["external_url"]
    assert figma[0]["storage_backend"] is None  # no blob / no secret storage

    # --- event-type relation -------------------------------------------------
    relations_resp = await client.get(f"/api/v1/projects/{slug}/relations")
    assert relations_resp.status_code == 200
    relations = relations_resp.json()
    assert len(relations) >= 1, relations
    assert relations[0]["relation_type"] == "belongs_to"

    # --- event-type owner (real creator only) --------------------------------
    types_resp = await client.get(f"/api/v1/projects/{slug}/event-types")
    assert types_resp.status_code == 200
    types_by_name = {t["name"]: t for t in types_resp.json()}
    screen_view_id = types_by_name["screen_view"]["id"]
    owners_resp = await client.get(f"/api/v1/projects/{slug}/event-types/{screen_view_id}/owners")
    assert owners_resp.status_code == 200
    owners = owners_resp.json()
    assert len(owners) == 1, owners
    assert owners[0]["user_email"] == "test@example.com"

    # --- authored per-event override -----------------------------------------
    variables_resp = await client.get(f"/api/v1/projects/{slug}/variables")
    assert variables_resp.status_code == 200
    variables_by_name = {v["name"]: v for v in variables_resp.json()["items"]}
    product_id = variables_by_name["product_id"]["id"]
    overrides_resp = await client.get(
        f"/api/v1/projects/{slug}/variables/{product_id}/event-overrides"
    )
    assert overrides_resp.status_code == 200
    overrides = overrides_resp.json()
    assert len(overrides) == 1, overrides
    assert overrides[0]["event_name"] == "Trial Started"
    assert overrides[0]["values"] == ["prod_monthly", "prod_annual"]

    # --- feature branch journey (branch + comment + revision) ----------------
    branches_resp = await client.get(f"/api/v1/projects/{slug}/branches")
    assert branches_resp.status_code == 200
    branch_items = branches_resp.json()["items"]
    feature = next(b for b in branch_items if b["name"] == "feature/checkout-funnel")
    assert feature["kind"] == "working"

    comments_resp = await client.get(f"/api/v1/projects/{slug}/branches/{feature['id']}/comments")
    assert comments_resp.status_code == 200
    comments = comments_resp.json()
    assert len(comments) >= 1, comments
    assert "checkout funnel" in comments[0]["body"]

    revisions_resp = await client.get(f"/api/v1/projects/{slug}/revisions")
    assert revisions_resp.status_code == 200
    revisions = revisions_resp.json()["items"]
    assert len(revisions) >= 1, revisions
    assert any("feature/checkout-funnel" in rev["summary"] for rev in revisions)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


async def _provision_shell(session: AsyncSession, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a hidden demo shell + main branch, mirroring create_demo_project phase 1."""
    project = Project(
        name="Demo Project",
        slug=slug,
        description="determinism fixture",
        is_demo=True,
        demo_recipe_version=DEMO_RECIPE_VERSION,
        generation_status=ProjectGenerationStatus.seeding.value,
        generation_stage="init",
    )
    session.add(project)
    await session.flush()
    branch_id = await plan_branch_service.ensure_main_branch_id(session, project.id)
    await session.commit()
    return project.id, branch_id


async def _seed_fixture(session: AsyncSession, slug: str, *, seed: int) -> uuid.UUID:
    project_id, branch_id = await _provision_shell(session, slug)
    ctx = DemoContext(
        project_id=project_id,
        branch_id=branch_id,
        slug=slug,
        now=_FIXED_NOW,
        seed=seed,
    )
    await seed_demo_content(session, ctx)
    await session.commit()
    return project_id


async def _scan_config_id(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID:
    return (
        await session.execute(select(ScanConfig.id).where(ScanConfig.project_id == project_id))
    ).scalar_one()


async def _event_metric_shape(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[tuple[str, datetime], int]:
    """Per-event metric counts keyed by (event name, bucket) — ids are project-local."""
    event_rows = await session.execute(
        select(Event.id, Event.name).where(Event.project_id == project_id)
    )
    name_by_id = dict(event_rows.all())
    scan_config_id = await _scan_config_id(session, project_id)
    rows = (
        await session.execute(
            select(EventMetric.event_id, EventMetric.bucket, EventMetric.count).where(
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.event_id.is_not(None),
            )
        )
    ).all()
    return {(name_by_id[event_id], bucket): count for event_id, bucket, count in rows}


async def _anomaly_shape(session: AsyncSession, project_id: uuid.UUID) -> list[tuple]:
    """Anomaly shape without project-local ids (scope_ref/event ids excluded)."""
    scan_config_id = await _scan_config_id(session, project_id)
    rows = (
        (
            await session.execute(
                select(MetricAnomaly).where(MetricAnomaly.scan_config_id == scan_config_id)
            )
        )
        .scalars()
        .all()
    )
    shape = [
        (
            a.scope_type,
            a.bucket,
            a.actual_count,
            round(a.expected_count, 6),
            round(a.z_score, 6),
            a.direction,
        )
        for a in rows
    ]
    return sorted(shape)


@pytest.mark.asyncio
async def test_scenario_is_deterministic_for_fixed_clock_and_seed() -> None:
    async with TestSessionLocal() as session:
        first_id = await _seed_fixture(session, "demo-detA", seed=DEMO_SEED)
        second_id = await _seed_fixture(session, "demo-detB", seed=DEMO_SEED)

        first_metrics = await _event_metric_shape(session, first_id)
        second_metrics = await _event_metric_shape(session, second_id)
        assert first_metrics == second_metrics
        assert first_metrics, "expected a non-empty metric series"

        first_anomalies = await _anomaly_shape(session, first_id)
        second_anomalies = await _anomaly_shape(session, second_id)
        assert first_anomalies == second_anomalies
        assert first_anomalies, "expected at least one seeded anomaly"


@pytest.mark.asyncio
async def test_different_seeds_change_the_metric_shape() -> None:
    async with TestSessionLocal() as session:
        base_id = await _seed_fixture(session, "demo-seedX", seed=DEMO_SEED)
        other_id = await _seed_fixture(session, "demo-seedY", seed=DEMO_SEED + 1)

        base_metrics = await _event_metric_shape(session, base_id)
        other_metrics = await _event_metric_shape(session, other_id)
        # Same keys (same clock => same events/buckets), but the deterministic
        # noise differs, so the counts are not identical everywhere.
        assert set(base_metrics) == set(other_metrics)
        assert base_metrics != other_metrics


def test_derive_seed_is_stable_and_key_sensitive() -> None:
    # Reproducible for the same (seed, key); PYTHONHASHSEED-independent.
    assert noise.derive_seed(DEMO_SEED, "Home Screen View") == noise.derive_seed(
        DEMO_SEED, "Home Screen View"
    )
    assert noise.derive_seed(DEMO_SEED, "Home Screen View") != noise.derive_seed(
        DEMO_SEED, "Paywall View"
    )
    # hourly_volume is a pure function of its inputs.
    bucket = _FIXED_NOW - timedelta(hours=5)
    assert noise.hourly_volume(1800, bucket, 3, 42, 500) == noise.hourly_volume(
        1800, bucket, 3, 42, 500
    )


# ---------------------------------------------------------------------------
# Independently testable single builder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_builder_seeds_schema_in_isolation() -> None:
    async with TestSessionLocal() as session:
        project_id, branch_id = await _provision_shell(session, "demo-planonly")
        ctx = DemoContext(
            project_id=project_id,
            branch_id=branch_id,
            slug="demo-planonly",
            now=_FIXED_NOW,
            seed=DEMO_SEED,
            created_by=None,  # no real user -> owner example is skipped
        )
        await plan_builder.build_plan(session, ctx)
        await session.commit()

        event_types = (
            (await session.execute(select(EventType).where(EventType.project_id == project_id)))
            .scalars()
            .all()
        )
        assert len(event_types) == 3

        events = (
            (await session.execute(select(Event).where(Event.project_id == project_id)))
            .scalars()
            .all()
        )
        assert len(events) == len(plan_builder.event_specs(_FIXED_NOW))

        relations = (
            (
                await session.execute(
                    select(EventTypeRelation).where(EventTypeRelation.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(relations) == 1

        # created_by is None => the owner example is intentionally skipped.
        owners = (
            (
                await session.execute(
                    select(EventTypeOwner).where(
                        EventTypeOwner.event_type_id.in_([et.id for et in event_types])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert owners == []

        # ctx is populated with stable semantic references for downstream builders.
        assert set(ctx.event_type_ids) == {"screen_view", "click", "purchase"}
        assert "Home Screen View" in ctx.event_ids
