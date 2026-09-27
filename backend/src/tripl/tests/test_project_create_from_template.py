"""Creating a project from a built-in template (F21, GH #274).

The template's plan lands on ONE draft working branch; main stays empty until
that branch is reviewed and merged through the ordinary flow. No metric, fact
table, data source, scan config, alert destination or alert rule is created.
"""

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.main import app
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.fact_table import FactTable
from tripl.models.metric_definition import MetricDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.scan_config import ScanConfig
from tripl.models.subscription import Subscription
from tripl.models.variable import Variable
from tripl.services import project_template_service
from tripl.services.project_templates import TEMPLATES, get_template
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"


async def _create(client: AsyncClient, slug: str, template_id: str | None = "ecommerce") -> Any:
    body: dict[str, Any] = {"name": slug, "slug": slug}
    if template_id is not None:
        body["template_id"] = template_id
    return await client.post("/api/v1/projects", json=body)


async def _project_id(session: AsyncSession, slug: str) -> uuid.UUID | None:
    project_id: uuid.UUID | None = await session.scalar(
        select(Project.id).where(Project.slug == slug)
    )
    return project_id


async def _main_branch_id(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID:
    main_id: uuid.UUID | None = await session.scalar(
        select(PlanBranch.id).where(
            PlanBranch.project_id == project_id, PlanBranch.kind == BranchKind.main.value
        )
    )
    assert main_id is not None
    return main_id


async def _count(session: AsyncSession, column: Any, *where: Any) -> int:
    total: int | None = await session.scalar(select(func.count(column)).where(*where))
    return total or 0


async def _plan_counts(
    session: AsyncSession, project_id: uuid.UUID, branch_id: uuid.UUID
) -> tuple[int, int, int]:
    types = await _count(
        session, EventType.id, EventType.project_id == project_id, EventType.branch_id == branch_id
    )
    events = await _count(
        session, Event.id, Event.project_id == project_id, Event.branch_id == branch_id
    )
    variables = await _count(
        session, Variable.id, Variable.project_id == project_id, Variable.branch_id == branch_id
    )
    return types, events, variables


async def _second_user(email: str, *, owner: AsyncClient, role: str | None) -> AsyncClient:
    other = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    resp = await other.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD, "name": email}
    )
    assert resp.status_code == 201, resp.text
    if role is not None:
        patched = await owner.patch(f"/api/v1/users/{resp.json()['id']}", json={"role": role})
        assert patched.status_code == 200, patched.text
        relogin = await other.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert relogin.status_code == 200, relogin.text
    return other


# ── list endpoint ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_templates(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/project-templates")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [t["id"] for t in body] == ["ecommerce", "subscriptions", "mobile_games", "b2b_saas"]
    for item, template in zip(body, TEMPLATES, strict=True):
        counts = template.counts()
        assert item["version"] == template.version
        assert item["branch_name"] == template.branch_name
        assert item["counts"] == {
            "event_types": counts.event_types,
            "fields": counts.fields,
            "events": counts.events,
            "variables": counts.variables,
            "metric_suggestions": counts.metric_suggestions,
            "alert_suggestions": counts.alert_suggestions,
        }
        assert item["event_type_names"] == [et.name for et in template.event_types]
        assert {m["needs"] for m in item["metric_suggestions"]} <= {"scan", "data_source"}
        assert {a["needs"] for a in item["alert_suggestions"]} == {"alert_destination"}


@pytest.mark.asyncio
async def test_list_templates_requires_authentication(anon_client: AsyncClient) -> None:
    resp = await anon_client.get("/api/v1/project-templates")
    assert resp.status_code == 401


# ── create from template ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_from_template_seeds_a_draft_branch_and_leaves_main_empty(
    client: AsyncClient,
) -> None:
    template = get_template("ecommerce")
    assert template is not None
    counts = template.counts()

    resp = await _create(client, "shop")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    branch_id = body["template_branch_id"]
    assert branch_id is not None
    # Main-scoped counters stay 0: the plan lives on the branch.
    assert body["summary"]["event_count"] == 0
    assert body["summary"]["event_type_count"] == 0

    async with TestSessionLocal() as session:
        project_id = await _project_id(session, "shop")
        assert project_id is not None
        main_id = await _main_branch_id(session, project_id)
        assert await _plan_counts(session, project_id, main_id) == (0, 0, 0)
        assert await _plan_counts(session, project_id, uuid.UUID(branch_id)) == (
            counts.event_types,
            counts.events,
            counts.variables,
        )
        statuses = (
            await session.scalars(
                select(Event.status).where(Event.branch_id == uuid.UUID(branch_id))
            )
        ).all()
        assert set(statuses) == {"draft"}

    branches = await client.get("/api/v1/projects/shop/branches")
    assert branches.status_code == 200, branches.text
    working = [b for b in branches.json()["items"] if b["kind"] == "working"]
    assert len(working) == 1
    assert working[0]["id"] == branch_id
    assert working[0]["name"] == template.branch_name
    assert working[0]["status"] == "draft"

    detail = await client.get(f"/api/v1/projects/shop/branches/{branch_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["description"] == project_template_service.render_branch_description(
        template
    )

    diff = await client.get(f"/api/v1/projects/shop/branches/{branch_id}/diff")
    assert diff.status_code == 200, diff.text
    entries = diff.json()["entries"]
    assert {e["kind"] for e in entries} == {"added"}
    by_type: dict[str, int] = {}
    for entry in entries:
        by_type[entry["entity_type"]] = by_type.get(entry["entity_type"], 0) + 1
    assert by_type == {
        "event_type": counts.event_types,
        "field_definition": counts.fields,
        "event": counts.events,
        "variable": counts.variables,
    }
    assert diff.json()["summary"] == {
        "added": counts.event_types + counts.fields + counts.events + counts.variables,
        "removed": 0,
        "changed": 0,
        # A fresh template branch carries no machine-made rows to set aside.
        "housekeeping": 0,
    }


@pytest.mark.parametrize("template", TEMPLATES, ids=[t.id for t in TEMPLATES])
@pytest.mark.asyncio
async def test_every_template_seeds(client: AsyncClient, template: Any) -> None:
    slug = f"tpl-{template.id.replace('_', '-')}"
    resp = await _create(client, slug, template.id)
    assert resp.status_code == 201, resp.text
    branch_id = uuid.UUID(resp.json()["template_branch_id"])
    counts = template.counts()
    async with TestSessionLocal() as session:
        project_id = await _project_id(session, slug)
        assert project_id is not None
        assert await _plan_counts(session, project_id, branch_id) == (
            counts.event_types,
            counts.events,
            counts.variables,
        )


@pytest.mark.asyncio
async def test_creator_is_member_author_and_subscribed(client: AsyncClient) -> None:
    editor = await _second_user("tpl-editor@example.com", owner=client, role=None)
    try:
        resp = await _create(editor, "editor-shop")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["my_role"] == "editor"
        branch_id = uuid.UUID(body["template_branch_id"])
        user_id = uuid.UUID((await editor.get("/api/v1/auth/me")).json()["id"])

        async with TestSessionLocal() as session:
            project_id = await _project_id(session, "editor-shop")
            member = await session.scalar(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
                )
            )
            assert member is not None
            assert str(member.role) == "editor"
            branch = await session.get(PlanBranch, branch_id)
            assert branch is not None
            assert branch.created_by == user_id
            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.user_id == user_id, Subscription.entity_id == branch_id
                )
            )
            assert subscription is not None
            assert "author" in subscription.reasons

        # The creator can open the branch it was sent to.
        seen = await editor.get(f"/api/v1/projects/editor-shop/branches/{branch_id}")
        assert seen.status_code == 200, seen.text
    finally:
        await editor.aclose()


@pytest.mark.asyncio
async def test_no_metrics_alerts_or_sources_are_created(client: AsyncClient) -> None:
    resp = await _create(client, "no-extras")
    assert resp.status_code == 201, resp.text
    async with TestSessionLocal() as session:
        project_id = await _project_id(session, "no-extras")
        assert project_id is not None
        assert await _count(session, DataSource.id) == 0
        assert await _count(session, ScanConfig.id, ScanConfig.project_id == project_id) == 0
        assert (
            await _count(session, MetricDefinition.id, MetricDefinition.project_id == project_id)
            == 0
        )
        assert await _count(session, FactTable.id, FactTable.project_id == project_id) == 0
        assert (
            await _count(session, AlertDestination.id, AlertDestination.project_id == project_id)
            == 0
        )
        assert await _count(session, AlertRule.id) == 0


@pytest.mark.asyncio
async def test_audit_rows_for_project_and_branch(client: AsyncClient) -> None:
    resp = await _create(client, "audited")
    assert resp.status_code == 201, resp.text
    branch_id = uuid.UUID(resp.json()["template_branch_id"])
    async with TestSessionLocal() as session:
        rows = (
            await session.scalars(select(AuditLog).where(AuditLog.project_slug == "audited"))
        ).all()
    by_action = {row.action: row for row in rows}
    assert by_action["project.create"].payload["template_id"] == "ecommerce"
    branch_row = by_action["plan_branch.create"]
    assert branch_row.target_id == branch_id
    assert branch_row.payload["template_id"] == "ecommerce"


# ── errors, RBAC and atomicity ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_template_is_422_and_creates_nothing(client: AsyncClient) -> None:
    resp = await _create(client, "ghost", "no-such-template")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "Unknown project template"
    async with TestSessionLocal() as session:
        assert await _project_id(session, "ghost") is None
        assert await _count(session, PlanBranch.id) == 0


@pytest.mark.asyncio
async def test_empty_template_id_is_rejected(client: AsyncClient) -> None:
    resp = await _create(client, "empty-tpl", "")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_without_template_behaviour_is_unchanged(client: AsyncClient) -> None:
    resp = await _create(client, "blank", None)
    assert resp.status_code == 201, resp.text
    assert resp.json()["template_branch_id"] is None
    branches = await client.get("/api/v1/projects/blank/branches")
    assert [b["kind"] for b in branches.json()["items"]] == ["main"]


@pytest.mark.asyncio
async def test_viewer_cannot_create_from_template(client: AsyncClient) -> None:
    viewer = await _second_user("tpl-viewer@example.com", owner=client, role="viewer")
    try:
        resp = await _create(viewer, "viewer-shop")
        assert resp.status_code == 403, resp.text
    finally:
        await viewer.aclose()
    async with TestSessionLocal() as session:
        assert await _project_id(session, "viewer-shop") is None


@pytest.mark.asyncio
async def test_duplicate_slug_is_409_and_seeds_nothing_twice(client: AsyncClient) -> None:
    first = await _create(client, "twice")
    assert first.status_code == 201, first.text
    second = await _create(client, "twice")
    assert second.status_code == 409
    async with TestSessionLocal() as session:
        project_id = await _project_id(session, "twice")
        assert project_id is not None
        working = await _count(
            session,
            PlanBranch.id,
            PlanBranch.project_id == project_id,
            PlanBranch.kind == BranchKind.working.value,
        )
        assert working == 1


@pytest.mark.asyncio
async def test_seeding_failure_leaves_no_half_project(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("seed failed")

    monkeypatch.setattr(project_template_service, "_add_events", _boom)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", cookies=client.cookies
    ) as boom_client:
        resp = await _create(boom_client, "half")
    assert resp.status_code == 500

    async with TestSessionLocal() as session:
        assert await _project_id(session, "half") is None
        assert await _count(session, PlanBranch.id) == 0
        assert await _count(session, EventType.id) == 0
        assert await _count(session, Variable.id) == 0
        assert await _count(session, ProjectMember.id) == 0

    # The slug is still free: the same request succeeds once seeding works.
    monkeypatch.undo()
    retry = await _create(client, "half")
    assert retry.status_code == 201, retry.text


# ── review and merge through the existing flow ──────────────────────────────


@pytest.mark.asyncio
async def test_merging_the_template_branch_puts_the_plan_on_main(client: AsyncClient) -> None:
    template = get_template("ecommerce")
    assert template is not None
    resp = await _create(client, "merge-shop")
    assert resp.status_code == 201, resp.text
    branch_id = resp.json()["template_branch_id"]

    for action in ("submit", "approve"):
        moved = await client.post(
            f"/api/v1/projects/merge-shop/branches/{branch_id}/transition",
            json={"action": action},
        )
        assert moved.status_code == 200, moved.text
    merged = await client.post(f"/api/v1/projects/merge-shop/branches/{branch_id}/merge")
    assert merged.status_code == 200, merged.text

    counts = template.counts()
    async with TestSessionLocal() as session:
        project_id = await _project_id(session, "merge-shop")
        assert project_id is not None
        main_id = await _main_branch_id(session, project_id)
        assert await _plan_counts(session, project_id, main_id) == (
            counts.event_types,
            counts.events,
            counts.variables,
        )
        names = set(
            (
                await session.scalars(
                    select(Event.name).where(
                        Event.project_id == project_id, Event.branch_id == main_id
                    )
                )
            ).all()
        )
    assert names == {ev.name for ev in template.events}
