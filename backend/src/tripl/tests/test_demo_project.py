"""The demo project endpoint: the authored plan, its source, its guardrails.

Recipe 5: the plan is authored from the shared roster, everything observed comes
from tripl's own scan and replay over the demo warehouse
(``test_demo_v5_provisioning`` pins the observed half, ``test_demo_contract``
the full story). What is pinned here is what the recipe AUTHORS and how the
endpoint treats it: the plan and its first-seen stagger, the warehouse source
and its catalog, the branch's one pending change, the audit trail, reset and
delete, and the provisioning guardrails (cancel, per-creator cap, sweeps).

A provisioning is the expensive part (tens of seconds even on the suite's
two-day history), so one demo carries every check that needs the real
pipeline, and the guardrail tests swap the seed for a no-op: they are about the
shell's lifecycle, not its contents.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.adapters.registry import build_adapter
from tripl.core.demo_warehouse import (
    DEAD_EVENT,
    DOCUMENTED_PRODUCTS,
    DUPLICATE_PAIRS,
    SCAN_COLUMNS,
    UNDOCUMENTED_PRODUCT,
    roster,
)
from tripl.models.data_source import DataSource, TestStatus
from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.models.variable import Variable
from tripl.services import demo_service, duplicate_service, plan_branch_service
from tripl.services.demo import DemoContext, noise, seed_demo_content
from tripl.services.demo.builders import plan
from tripl.services.demo.builders.branches import CHANGED_EVENT_NAME
from tripl.services.demo.scenario import DEMO_SEED
from tripl.services.project_service import demo_data_source_name
from tripl.tests._audit_feed import org_audit
from tripl.tests.conftest import TestSessionLocal

_OPEN_BRANCH = "feature/checkout-funnel"


def _parsed(value: str) -> datetime:
    # The suite runs on sqlite, which drops the offset of a timezone-aware
    # column, so stamps come back naive here and aware on Postgres.
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


async def _project_id_for_slug(session: AsyncSession, slug: str) -> uuid.UUID:
    return (await session.execute(select(Project.id).where(Project.slug == slug))).scalar_one()


async def _working_branch(session: AsyncSession, project_id: uuid.UUID) -> PlanBranch:
    return (
        await session.execute(
            select(PlanBranch).where(
                PlanBranch.project_id == project_id,
                PlanBranch.kind == BranchKind.working.value,
                PlanBranch.name == _OPEN_BRANCH,
            )
        )
    ).scalar_one()


async def _project_audit(client: AsyncClient, slug: str) -> list[dict]:
    """The project's whole audit trail, every page of it."""
    entries: list[dict] = []
    while True:
        page = await client.get(f"/api/v1/projects/{slug}/audit?limit=200&offset={len(entries)}")
        assert page.status_code == 200, page.text
        items = page.json()["items"]
        entries.extend(items)
        if len(items) < 200:
            return entries


@pytest.fixture
def _no_op_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Promote demo shells without authoring or observing anything.

    ``_seed_demo_content`` returning no context is the published seam for this
    (the observed stages are skipped): these tests are about the shell.
    """

    async def _nothing(_session: AsyncSession, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(demo_service, "_seed_demo_content", _nothing)


# ── One provisioned demo: the authored plan, its source, reset, delete ──────


async def _assert_the_plan_is_the_roster(client: AsyncClient, slug: str) -> None:
    events_resp = await client.get(f"/api/v1/projects/{slug}/events?limit=500")
    assert events_resp.status_code == 200
    items = events_resp.json()["items"]
    by_name = {ev["name"]: ev for ev in items}
    # The list hides archived events unless asked for them.
    authored = {event.name for event in roster.events() if event.status != "archived"}
    assert authored <= set(by_name)
    assert any(ev.get("field_values") for ev in items)

    # "First seen" (created_at) is the plan's own stagger, never the seed
    # instant: core events anchor the history's start, planned ones were
    # written in the last fortnight, nothing authored reads as brand new.
    # (A name the scan adopted on its own is first seen when it was adopted.)
    now = datetime.now(tz=UTC)
    ages = {
        name: (now - _parsed(by_name[name]["created_at"])).total_seconds() / 86400
        for name in authored
    }
    for name, age_days in ages.items():
        assert age_days >= 1, f"{name} first seen only {age_days:.1f}d ago — reads as brand new"
    assert ages["Home Screen View"] >= noise.history_days() - 1, ages["Home Screen View"]
    # Part of the catalog falls inside the Overview KPI's 14-day lookback, so
    # its "active events" sparkline is not a flat zero.
    assert any(age_days < 13 for age_days in ages.values())


async def _assert_the_catalog_and_source(client: AsyncClient, slug: str) -> None:
    metrics_total = await client.get(f"/api/v1/projects/{slug}/metrics/total")
    assert metrics_total.status_code == 200
    assert metrics_total.json()["data"], "the replay stored no event volume"

    metrics = (await client.get(f"/api/v1/projects/{slug}/metrics")).json()["items"]
    assert len(metrics) >= 4
    assert {"sql", "event_composition", "fact"} <= {metric["kind"] for metric in metrics}
    assert any(metric["spark"] for metric in metrics)
    # The conversion ratio is a fraction (purchases / paywall views), not a count.
    conversion = next(metric for metric in metrics if metric["name"] == "purchase_conversion")
    assert conversion["kind"] == "event_composition"
    assert conversion["latest_value"] is not None
    assert 0.0 <= conversion["latest_value"] < 1.0, conversion["latest_value"]

    fact_tables = (await client.get(f"/api/v1/projects/{slug}/fact-tables")).json()["items"]
    assert fact_tables
    detail = (
        await client.get(f"/api/v1/projects/{slug}/fact-tables/{fact_tables[0]['id']}")
    ).json()
    assert "completed" in {row_filter["name"] for row_filter in detail["row_filters"]}

    # The fact-table preview reads the warehouse's ``orders`` through the normal
    # path (introspection -> build_adapter -> the demo engine). It answers the
    # query's SHAPE and never its rows.
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
    columns = {column["name"]: column["type"] for column in preview["columns"]}
    assert {
        "created_at",
        "order_id",
        "amount",
        "currency",
        "user_id",
        "country",
        "status",
        "payment_method",
    } <= set(columns)
    assert columns["created_at"] == "timestamp"
    assert columns["amount"] == "number"
    assert columns["status"] == "string"
    assert "user_id" in preview["identifier_candidates"]
    assert "sample_rows" not in preview

    async with TestSessionLocal() as session:
        project_id = await _project_id_for_slug(session, slug)
        source = (
            await session.execute(select(DataSource).where(DataSource.project_id == project_id))
        ).scalar_one()
        anomaly_settings = (
            await session.execute(
                select(ProjectAnomalySettings).where(
                    ProjectAnomalySettings.project_id == project_id
                )
            )
        ).scalar_one()

    # The demo warehouse, tested healthy (the scan stage stamps it) and owned by
    # the project so the Overview rail can filter it per project.
    assert source.db_type == "demo"
    assert source.last_test_status == TestStatus.success
    assert source.last_test_at is not None
    assert source.project_id == project_id
    # detect.py only runs when the project-level settings row is enabled too.
    assert anomaly_settings.anomaly_detection_enabled is True
    assert anomaly_settings.detect_events is True
    assert anomaly_settings.detect_project_total is True
    # Platform stories are told by attribution and release regressions.
    assert anomaly_settings.detect_platform_breakdowns is False

    # The dead event is authored past the 30-day cutoff on both of its clocks
    # (first seen and last seen), so the dead-events query can flag it.
    dead = await client.get(f"/api/v1/projects/{slug}/reconciliation/dead-events?days=30")
    assert dead.status_code == 200
    assert DEAD_EVENT in {item["name"] for item in dead.json()["items"]}

    # The seeder inserts through the ORM and never meets the write path's
    # checks; a no-op PATCH re-validates every stored scan config against them.
    configs = (await client.get(f"/api/v1/projects/{slug}/scans")).json()
    assert configs
    for config in configs:
        resp = await client.patch(
            f"/api/v1/projects/{slug}/scans/{config['id']}", json={"name": config["name"]}
        )
        assert resp.status_code == 200, (config["name"], resp.json())


async def _assert_the_branch_carries_one_change(client: AsyncClient, slug: str) -> None:
    branches = (await client.get(f"/api/v1/projects/{slug}/branches")).json()["items"]
    feature = next(branch for branch in branches if branch["name"] == _OPEN_BRANCH)
    diff_resp = await client.get(f"/api/v1/projects/{slug}/branches/{feature['id']}/diff")
    assert diff_resp.status_code == 200
    diff = diff_resp.json()
    assert diff["summary"] == {"added": 0, "removed": 0, "changed": 1, "housekeeping": 0}
    [entry] = diff["entries"]
    assert (entry["entity_type"], entry["kind"], entry["name"]) == (
        "event",
        "changed",
        CHANGED_EVENT_NAME,
    )
    assert {change["field"] for change in entry["field_changes"]} == {"description"}
    # Whether main moved since the branch was cut: see
    # test_a_fresh_demos_open_branch_is_not_behind_main.

    # A full plan copy behind it, not just the one edited row.
    async with TestSessionLocal() as session:
        project_id = await _project_id_for_slug(session, slug)
        branch = await _working_branch(session, project_id)
        main_branch_id = await plan_branch_service.ensure_main_branch_id(session, project_id)
        main_count = await session.scalar(
            select(func.count(Event.id)).where(Event.branch_id == main_branch_id)
        )
        branch_count = await session.scalar(
            select(func.count(Event.id)).where(Event.branch_id == branch.id)
        )
    assert branch_count == len(roster.events())
    assert main_count is not None and main_count >= branch_count


async def _assert_the_audit_trail(client: AsyncClient, slug: str) -> list[dict]:
    entries = await _project_audit(client, slug)
    assert entries, "a fresh demo lands on 'No audit entries yet'"
    actions = {entry["action"] for entry in entries}
    # Every authored action is one the Audit tab's filter offers.
    assert {"event_type.create", "variable.create", "alert_rule.create"} <= actions
    assert all(entry["user_email"] for entry in entries)

    # The rows are derived, not invented: one creation per authored event, every
    # edit after the creation it edits, nothing dated ahead of the seed.
    at = {entry["id"]: _parsed(entry["created_at"]) for entry in entries}
    created = [entry for entry in entries if entry["action"] == "event.create"]
    # Main edits only: the branch edit targets the branch's COPY of the event.
    updated = [
        entry for entry in entries if entry["action"] == "event.update" and not entry["branch_name"]
    ]
    assert created and updated
    creation_at = {entry["target_id"]: at[entry["id"]] for entry in created}
    assert len(creation_at) == len(created)
    # On the NAME too: the branch copies carry fresh ids but the same names.
    assert len({entry["target_name"] for entry in created}) == len(created)
    for entry in updated:
        assert entry["target_id"] in creation_at, entry
        assert creation_at[entry["target_id"]] < at[entry["id"]], entry
    assert max(at.values()) <= datetime.now(UTC)

    # A data source is a workspace resource: its row is filed with no project,
    # exactly as api/v1/data_sources.py writes it; the scan config is a project
    # resource and is filed as one.
    assert "data_source.create" not in actions
    assert "scan_config.create" in actions
    warehouses = [
        entry
        for entry in await org_audit("data_source.create")
        if entry["target_name"] == demo_data_source_name(slug)
    ]
    assert len(warehouses) == 1, warehouses
    assert warehouses[0]["project_id"] is None
    assert warehouses[0]["project_slug"] == ""
    return entries


@pytest.mark.asyncio
async def test_a_demo_is_its_authored_plan_and_survives_reset_and_delete(
    client: AsyncClient,
) -> None:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202, resp.text
    demo = resp.json()
    assert demo["generation_status"] == "ready", demo
    slug = demo["slug"]
    assert slug.startswith("demo-")
    assert len(slug) == len("demo-") + 6

    await _assert_the_plan_is_the_roster(client, slug)
    await _assert_the_catalog_and_source(client, slug)
    await _assert_the_branch_carries_one_change(client, slug)
    before = await _assert_the_audit_trail(client, slug)

    async with TestSessionLocal() as session:
        old_project_id = await _project_id_for_slug(session, slug)
        old_branch_id = (await _working_branch(session, old_project_id)).id

    # Reset drops the old demo in full — branches, their plan copies, its audit
    # trail — and re-seeds the same recipe under a fresh id, the same slug and
    # the same name.
    reset = await client.post(f"/api/v1/projects/demo/{slug}/reset")
    assert reset.status_code == 200, reset.text
    assert reset.json()["name"] == demo["name"]
    assert reset.json()["generation_status"] == "ready", reset.json()

    async with TestSessionLocal() as session:
        new_project_id = await _project_id_for_slug(session, slug)
        assert new_project_id != old_project_id
        for count_query in (
            select(func.count(PlanBranch.id)).where(PlanBranch.project_id == old_project_id),
            select(func.count(Event.id)).where(Event.branch_id == old_branch_id),
            select(func.count(Event.id)).where(Event.project_id == old_project_id),
            select(func.count(DataSource.id)).where(DataSource.project_id == old_project_id),
        ):
            assert (await session.execute(count_query)).scalar_one() == 0
        new_branch = await _working_branch(session, new_project_id)
        assert await session.scalar(
            select(func.count(Event.id)).where(Event.branch_id == new_branch.id)
        )

    # The trail does not stack: the audit list filters on the slug, so the
    # replaced demo's rows would reattach to the new one. Counted on the
    # UNFILTERED feed, where the project filter cannot hide them.
    def creations(items: list[dict]) -> int:
        return sum(1 for entry in items if entry["action"] == "event.create")

    after = await _project_audit(client, slug)
    assert creations(after) == creations(before)
    everything = await org_audit()
    assert creations(everything) == creations(before)
    assert all(entry["project_id"] != str(old_project_id) for entry in everything)
    warehouses = [
        entry
        for entry in await org_audit("data_source.create")
        if entry["target_name"] == demo_data_source_name(slug)
    ]
    assert len(warehouses) == 1, warehouses

    # A data-rich demo deletes cleanly.
    deleted = await client.delete(f"/api/v1/projects/{slug}")
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/projects/{slug}")).status_code == 404


@pytest.mark.asyncio
async def test_a_fresh_demos_open_branch_is_not_behind_main(client: AsyncClient) -> None:
    """The catalog scan writes to main (event identities, the warehouse's columns as
    fields, the variables it finds); a branch snapshotted before it read behind main
    with ~220 changes nobody made. The branches are built after the observed stages."""
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]
    branches = (await client.get(f"/api/v1/projects/{slug}/branches")).json()["items"]
    feature = next(branch for branch in branches if branch["name"] == _OPEN_BRANCH)
    diff = (await client.get(f"/api/v1/projects/{slug}/branches/{feature['id']}/diff")).json()
    assert diff["behind_base"] is False


# ── The authored half alone, at a fixed clock ────────────────────────────────

_FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


async def _seed_fixture(session: AsyncSession, slug: str) -> uuid.UUID:
    """A demo shell with the recipe's authored builders run at a fixed clock."""
    project = Project(
        name="Demo Project",
        slug=slug,
        description="authored-half fixture",
        is_demo=True,
        generation_status=ProjectGenerationStatus.seeding.value,
        generation_stage="init",
    )
    session.add(project)
    await session.flush()
    branch_id = await plan_branch_service.ensure_main_branch_id(session, project.id)
    await session.commit()
    ctx = DemoContext(
        project_id=project.id, branch_id=branch_id, slug=slug, now=_FIXED_NOW, seed=DEMO_SEED
    )
    await seed_demo_content(session, ctx)
    await session.commit()
    return project.id


async def _branch_diff_shape(session: AsyncSession, slug: str, project_id: uuid.UUID) -> tuple:
    branch = await _working_branch(session, project_id)
    diff = await plan_branch_service.diff_branch(session, slug, branch.id)
    entries = sorted(
        (entry.entity_type, entry.kind, entry.parent, entry.name, tuple(entry.changes))
        for entry in diff.entries
    )
    return entries, diff.summary, diff.behind_base


async def _created_ats(session: AsyncSession, project_id: uuid.UUID) -> dict[str, datetime]:
    main = await plan_branch_service.ensure_main_branch_id(session, project_id)
    rows = (
        await session.execute(
            select(Event.name, Event.created_at).where(
                Event.project_id == project_id, Event.branch_id == main
            )
        )
    ).all()
    return {name: created_at for name, created_at in rows}


@pytest.mark.asyncio
async def test_the_authored_half_is_deterministic() -> None:
    # Same clock + seed => the same branch diff and the same first-seen stamps:
    # the authored half derives from the recipe, never from per-run randomness.
    async with TestSessionLocal() as session:
        first_id = await _seed_fixture(session, "demo-detA")
        second_id = await _seed_fixture(session, "demo-detB")

        first_diff = await _branch_diff_shape(session, "demo-detA", first_id)
        assert first_diff == await _branch_diff_shape(session, "demo-detB", second_id)
        entries, summary, behind_base = first_diff
        assert summary == {"added": 0, "removed": 0, "changed": 1, "housekeeping": 0}
        assert (entries[0][1], entries[0][3]) == ("changed", CHANGED_EVENT_NAME)
        assert behind_base is False

        first_created = await _created_ats(session, first_id)
        assert first_created == await _created_ats(session, second_id)
        assert set(first_created) == {event.name for event in roster.events()}


def test_first_seen_follows_the_plan_and_never_the_wall_clock() -> None:
    catalog = roster.events()
    non_core = [event for event in catalog if "core" not in event.tags]
    last_rank = len(non_core) - 1
    rank_of = {event.name: rank for rank, event in enumerate(non_core)}
    history_start = noise.history_start(_FIXED_NOW)

    def created(event: roster.EventDef) -> datetime:
        return plan.created_at_for(
            event, rank_of.get(event.name, 0), last_rank, now=_FIXED_NOW, seed=DEMO_SEED
        )

    for event in catalog:
        assert created(event) == created(event), event.name
        age = _FIXED_NOW - created(event)
        if event.name == DEAD_EVENT:
            # Past the dead-events cutoff on its first-seen clock too.
            assert age > plan.DEAD_EVENT_LAST_SEEN + timedelta(days=29)
        elif "core" in event.tags:
            assert history_start <= created(event) < history_start + timedelta(hours=12)
        elif event.status in {"draft", "in_review", "ready_for_dev"}:
            assert timedelta(days=1) <= age < timedelta(days=15), event.name
        else:
            assert timedelta(days=2) <= age <= timedelta(days=22), (event.name, age)


@pytest.mark.asyncio
async def test_demo_variables_document_their_allowed_values() -> None:
    """The coached chapter compares observed values against the documented list."""
    async with TestSessionLocal() as session:
        project_id = await _seed_fixture(session, "demo-allowedvalues")
        variables = (
            (await session.execute(select(Variable).where(Variable.project_id == project_id)))
            .scalars()
            .all()
        )
    by_name = {variable.name: variable for variable in variables}
    assert by_name["product_id"].allowed_values == list(DOCUMENTED_PRODUCTS)
    # The value the warehouse also sends sits OUTSIDE the documented list, or
    # "a scan saw prod_weekly outside the documented values" would be false.
    assert UNDOCUMENTED_PRODUCT not in by_name["product_id"].allowed_values
    # Unbounded identifiers stay undocumented — a list there would be a lie.
    assert by_name["user_id"].allowed_values == []
    assert by_name["session_id"].allowed_values == []


@pytest.mark.asyncio
async def test_demo_event_field_values_are_authored() -> None:
    """Hand-authored values, so the demo's own scans never rewrite the templates."""
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
    assert {"${product_id}", "${platform}"} <= {field_value.value for field_value in field_values}


@pytest.mark.asyncio
async def test_demo_scan_projects_only_low_cardinality_columns() -> None:
    """The catalog scan never groups by an identifier.

    Handed ``user_id``/``session_id``, the hourly catalog sync would create
    USER_ID / SESSION_ID fields on every event type and fill the curated plan
    with raw identifiers; ``order_id`` is the same mistake on ``orders``.
    """
    async with TestSessionLocal() as session:
        project_id = await _seed_fixture(session, "demo-scancolumns")
        config = (
            await session.execute(select(ScanConfig).where(ScanConfig.project_id == project_id))
        ).scalar_one()
        data_source = await session.get(DataSource, config.data_source_id)
    assert data_source is not None
    assert data_source.db_type == "demo"

    adapter = build_adapter(data_source)
    try:
        exposed = {column.name for column in adapter.get_columns(config.base_query)}
    finally:
        adapter.close()
    assert not {"user_id", "session_id", "order_id"} & exposed
    assert set(SCAN_COLUMNS) <= exposed


@pytest.mark.asyncio
async def test_the_duplicates_view_flags_the_rosters_near_duplicate() -> None:
    async with TestSessionLocal() as session:
        project_id = await _seed_fixture(session, "demo-duplicates")
        main = await plan_branch_service.ensure_main_branch_id(session, project_id)
        page = await duplicate_service.list_clusters(session, project_id, main)
    clusters = [{event.name for event in cluster.events} for cluster in page.items]
    for left, right in DUPLICATE_PAIRS:
        assert any({left, right} <= names for names in clusters), clusters


# ── Provisioning guardrails: cancel, per-creator cap, sweeps ─────────────────


@pytest.mark.asyncio
async def test_demo_project_viewer_cannot_create(anon_client: AsyncClient) -> None:
    resp = await anon_client.post("/api/v1/projects/demo")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_cancel_request_reports_nothing_to_cancel_when_idle(client: AsyncClient) -> None:
    """No in-flight provision means the caller must be told plainly, not lied to."""
    resp = await client.post("/api/v1/projects/demo/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": False, "slug": None, "state": "none"}


@pytest.mark.asyncio
async def test_cancel_request_flags_a_seeding_shell(client: AsyncClient) -> None:
    """A shell still `seeding` is flagged so the seed abandons itself between stages."""
    me = (await client.get("/api/v1/auth/me")).json()
    async with TestSessionLocal() as session:
        session.add(
            Project(
                name="Demo Project",
                slug="demo-inflight",
                is_demo=True,
                generation_status=ProjectGenerationStatus.seeding.value,
                generation_stage="init",
                created_by_user_id=uuid.UUID(me["id"]),
            )
        )
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
@pytest.mark.usefixtures("_no_op_seed")
async def test_demos_are_numbered_unique_and_capped_per_creator(client: AsyncClient) -> None:
    """Distinguishable cards, distinct slugs, and a refusal that points at reset/delete."""
    demos = []
    for _ in range(demo_service.MAX_DEMOS_PER_CREATOR):
        resp = await client.post("/api/v1/projects/demo")
        assert resp.status_code == 202, resp.text
        demos.append(resp.json())
    assert len({demo["slug"] for demo in demos}) == len(demos)
    assert [demo["name"] for demo in demos] == ["Demo Project"] + [
        f"Demo Project {number}" for number in range(2, len(demos) + 1)
    ]

    refused = await client.post("/api/v1/projects/demo")
    assert refused.status_code == 409
    assert "Reset or delete" in refused.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_op_seed")
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


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_op_seed")
async def test_a_shell_abandoned_mid_seed_never_locks_out_the_creator(
    client: AsyncClient,
) -> None:
    """A crash between the shell's commit and the outcome must not eat a cap slot.

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
                    # Nothing has written to an abandoned shell since its
                    # creation; the stall is judged by that last write.
                    created_at=datetime.now(UTC) - stalled_age,
                    updated_at=datetime.now(UTC) - stalled_age,
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
