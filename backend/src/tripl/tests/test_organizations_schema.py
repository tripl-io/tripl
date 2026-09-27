"""F20 PR1: the organization schema, its backfill, and the settings scope.

The backfill is driven through the migration's own ``backfill_organizations``
on a SQLite schema built from the models (foreign keys enforced), the way
``test_alembic_revisions`` drives other data migrations; the PostgreSQL round
trip of the whole revision lives in ``test_organizations_migration_pg``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tripl.models import Base
from tripl.models.api_key import ApiKey
from tripl.models.app_setting import SERVICE_SETTINGS_KEY, AppSetting
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.invitation import Invitation
from tripl.models.organization import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_SLUG,
    Organization,
    OrganizationMember,
)
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.services import app_settings_service
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_alembic_revisions import _load_migration

MIGRATION = "b8d0f2a4c6e8_organizations_schema_and_default_org.py"


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load_migration("organizations_migration", MIGRATION)


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_engine("sqlite://")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _user(session: Session, email: str, role: str) -> uuid.UUID:
    user = User(email=email, name=email, password_hash="x", role=role)
    session.add(user)
    session.flush()
    return user.id


def _seed_instance(engine: Engine) -> dict[str, uuid.UUID]:
    """Two owners, an editor and a viewer, on two projects."""
    with Session(engine) as session, session.begin():
        ids = {
            "owner1": _user(session, "owner1@example.com", "owner"),
            "owner2": _user(session, "owner2@example.com", "owner"),
            "editor": _user(session, "editor@example.com", "editor"),
            "viewer": _user(session, "viewer@example.com", "viewer"),
        }
        for slug in ("alpha", "beta"):
            project = Project(name=slug, slug=slug)
            session.add(project)
            session.flush()
            ids[slug] = project.id
        session.add_all(
            [
                ProjectMember(project_id=ids["alpha"], user_id=ids["editor"], role="editor"),
                ProjectMember(project_id=ids["beta"], user_id=ids["editor"], role="viewer"),
                # A viewer holding an editor grant: users.role caps it at read
                # time, and the backfill must leave the row as it is.
                ProjectMember(project_id=ids["alpha"], user_id=ids["viewer"], role="editor"),
                ProjectMember(project_id=ids["beta"], user_id=ids["viewer"], role="viewer"),
            ]
        )
    return ids


def _backfill(
    engine: Engine,
    migration: ModuleType,
    *,
    mode: str = "self_hosted",
    emails: tuple[str, ...] = (),
) -> dict[str, int]:
    with engine.begin() as connection:
        result: dict[str, int] = migration.backfill_organizations(
            connection, deployment_mode=mode, platform_admin_emails=list(emails)
        )
    return result


def _memberships(engine: Engine) -> dict[uuid.UUID, str]:
    with Session(engine) as session:
        rows = session.execute(
            select(OrganizationMember.user_id, OrganizationMember.role).where(
                OrganizationMember.organization_id == DEFAULT_ORG_ID
            )
        ).all()
    return {user_id: str(role) for user_id, role in rows}


def _platform_admins(engine: Engine) -> set[uuid.UUID]:
    with Session(engine) as session:
        return set(session.scalars(select(User.id).where(User.is_platform_admin.is_(True))))


# --- the backfill ---------------------------------------------------------------


def test_every_user_joins_the_default_org_with_the_mapped_role(
    engine: Engine, migration: ModuleType
) -> None:
    ids = _seed_instance(engine)

    counts = _backfill(engine, migration)

    assert _memberships(engine) == {
        ids["owner1"]: "owner",
        ids["owner2"]: "owner",
        ids["editor"]: "member",
        ids["viewer"]: "member",
    }
    assert counts["members_added"] == 4
    # The fixture schema already holds the default org; the backfill does not
    # insert a second one.
    assert counts["organization_created"] == 0
    with Session(engine) as session:
        assert session.scalar(select(sa.func.count()).select_from(Organization)) == 1


def test_project_memberships_are_left_unchanged(engine: Engine, migration: ModuleType) -> None:
    ids = _seed_instance(engine)

    counts = _backfill(engine, migration)

    with Session(engine) as session:
        rows = session.execute(
            select(ProjectMember.project_id, ProjectMember.user_id, ProjectMember.role)
        ).all()
    roles = {(project_id, user_id): str(role) for project_id, user_id, role in rows}
    assert roles == {
        (ids["alpha"], ids["editor"]): "editor",
        (ids["beta"], ids["editor"]): "viewer",
        (ids["alpha"], ids["viewer"]): "editor",
        (ids["beta"], ids["viewer"]): "viewer",
    }
    assert "project_memberships_capped" not in counts


def test_self_hosted_makes_every_instance_owner_a_platform_admin(
    engine: Engine, migration: ModuleType
) -> None:
    ids = _seed_instance(engine)

    # The email list is ignored when self-hosted.
    _backfill(engine, migration, mode="self_hosted", emails=("editor@example.com",))

    assert _platform_admins(engine) == {ids["owner1"], ids["owner2"]}


def test_hosted_grants_platform_admin_only_from_the_email_list(
    engine: Engine, migration: ModuleType
) -> None:
    ids = _seed_instance(engine)

    _backfill(
        engine, migration, mode="hosted", emails=(" Editor@Example.COM ", "nobody@example.com")
    )

    # An instance owner is NOT a platform admin in hosted mode unless listed.
    assert _platform_admins(engine) == {ids["editor"]}


def test_hosted_with_no_emails_grants_nobody(engine: Engine, migration: ModuleType) -> None:
    _seed_instance(engine)

    counts = _backfill(engine, migration, mode="hosted")

    assert _platform_admins(engine) == set()
    assert counts["platform_admins_granted"] == 0


def test_an_unknown_deployment_mode_is_refused(engine: Engine, migration: ModuleType) -> None:
    with pytest.raises(RuntimeError, match="DEPLOYMENT_MODE"):
        _backfill(engine, migration, mode="cloud")


def test_the_backfill_is_idempotent(engine: Engine, migration: ModuleType) -> None:
    ids = _seed_instance(engine)
    _backfill(engine, migration)

    again = _backfill(engine, migration)

    assert again == {
        "organization_created": 0,
        "members_added": 0,
        "platform_admins_granted": 0,
    }
    assert len(_memberships(engine)) == 4
    assert _platform_admins(engine) == {ids["owner1"], ids["owner2"]}


def test_the_backfill_creates_the_default_org_when_it_is_missing(
    engine: Engine, migration: ModuleType
) -> None:
    with engine.begin() as connection:
        connection.execute(sa.delete(Organization.__table__))

    counts = _backfill(engine, migration)

    assert counts["organization_created"] == 1
    with Session(engine) as session:
        org = session.get(Organization, DEFAULT_ORG_ID)
        assert org is not None
        assert org.slug == DEFAULT_ORG_SLUG
        assert org.members_can_create_projects is True
        assert org.default_project_role is None


def test_rows_without_an_org_and_project_bound_keys_are_assigned(
    engine: Engine, migration: ModuleType
) -> None:
    other_org = uuid.uuid4()
    with Session(engine) as session, session.begin():
        owner = _user(session, "owner@example.com", "owner")
        session.add(Organization(id=other_org, slug="other", name="Other"))
        session.flush()
        elsewhere = Project(name="elsewhere", slug="elsewhere", organization_id=other_org)
        session.add(elsewhere)
        session.flush()
        # Written as an old container would have: the key never named an org, so
        # it holds the default — the backfill must move it to its project's.
        bound = ApiKey(
            user_id=owner,
            project_id=elsewhere.id,
            name="bound",
            key_prefix="p1",
            key_hash="a" * 64,
            scope="read",
        )
        unbound = ApiKey(
            user_id=owner, name="free", key_prefix="p2", key_hash="b" * 64, scope="read"
        )
        session.add_all([bound, unbound])
        session.add(
            AuditLog(
                action="x", target_type="project", project_slug="elsewhere", organization_id=None
            )
        )
        session.add(
            Invitation(
                email="new@example.com",
                role="viewer",
                token_hash="c" * 64,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        session.flush()
        bound_id, unbound_id = bound.id, unbound.id

    _backfill(engine, migration)

    with Session(engine) as session:
        assert session.get(ApiKey, bound_id).organization_id == other_org  # type: ignore[union-attr]
        assert session.get(ApiKey, unbound_id).organization_id == DEFAULT_ORG_ID  # type: ignore[union-attr]
        assert set(session.scalars(select(AuditLog.organization_id))) == {DEFAULT_ORG_ID}
        invitation = session.scalars(select(Invitation)).one()
        assert invitation.org_role == "member"
        assert invitation.organization_id == DEFAULT_ORG_ID


def test_the_downgrade_guard_names_organization_scoped_settings(
    engine: Engine, migration: ModuleType
) -> None:
    with Session(engine) as session, session.begin():
        session.add(AppSetting(key=SERVICE_SETTINGS_KEY, value={}))
    with engine.connect() as connection:
        assert migration.organization_scoped_setting_keys(connection) == []

    with Session(engine) as session, session.begin():
        session.add(AppSetting(key=SERVICE_SETTINGS_KEY, value={}, organization_id=DEFAULT_ORG_ID))
    with engine.connect() as connection:
        assert migration.organization_scoped_setting_keys(connection) == [SERVICE_SETTINGS_KEY]


# --- the schema -----------------------------------------------------------------


def test_organization_membership_is_unique_per_user(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        user = _user(session, "u@example.com", "editor")
        session.add(OrganizationMember(organization_id=DEFAULT_ORG_ID, user_id=user, role="member"))
    with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
        session.add(OrganizationMember(organization_id=DEFAULT_ORG_ID, user_id=user, role="admin"))


def test_an_organization_that_owns_projects_cannot_be_deleted(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(Project(name="p", slug="p"))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(sa.delete(Organization.__table__))


def test_app_settings_are_unique_per_scope(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(AppSetting(key="k", value={}))
        # Same key at organization scope is a different row, not a duplicate.
        session.add(AppSetting(key="k", value={}, organization_id=DEFAULT_ORG_ID))

    with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
        session.add(AppSetting(key="k", value={}))
    with pytest.raises(IntegrityError), Session(engine) as session, session.begin():
        session.add(AppSetting(key="k", value={}, organization_id=DEFAULT_ORG_ID))


# --- no behaviour change: the suite DB and the existing write paths --------------


@pytest.mark.asyncio
async def test_the_test_database_holds_the_default_org() -> None:
    async with TestSessionLocal() as session:
        org = await session.get(Organization, DEFAULT_ORG_ID)
    assert org is not None
    assert org.slug == DEFAULT_ORG_SLUG


@pytest.mark.asyncio
async def test_rows_written_by_existing_services_land_in_the_default_org(
    client: AsyncClient,
) -> None:
    project = await client.post("/api/v1/projects", json={"name": "Org", "slug": "org-default"})
    assert project.status_code == 201, project.text
    source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Org CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
        },
    )
    assert source.status_code == 201, source.text
    key = await client.post("/api/v1/me/api-keys", json={"name": "agent", "scope": "read"})
    assert key.status_code == 201, key.text

    async with TestSessionLocal() as session:
        assert set(await session.scalars(select(Project.organization_id))) == {DEFAULT_ORG_ID}
        assert set(await session.scalars(select(DataSource.organization_id))) == {DEFAULT_ORG_ID}
        assert set(await session.scalars(select(ApiKey.organization_id))) == {DEFAULT_ORG_ID}
        # The registering first user is not made a platform admin by anything in
        # this PR: only the migration sets the flag.
        assert set(await session.scalars(select(User.is_platform_admin))) == {False}


async def _put_setting(organization_id: uuid.UUID | None, value: dict[str, Any]) -> None:
    async with TestSessionLocal() as session:
        session.add(
            AppSetting(key=SERVICE_SETTINGS_KEY, value=value, organization_id=organization_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_settings_reads_ignore_organization_rows() -> None:
    # Only an organization's row exists: the operator scope is still empty.
    await _put_setting(DEFAULT_ORG_ID, {"scan_row_limit_default": 7})
    async with TestSessionLocal() as session:
        assert await app_settings_service.get_service_overrides(session) == {}

    await _put_setting(None, {"scan_row_limit_default": 123})
    async with TestSessionLocal() as session:
        assert await app_settings_service.get_service_overrides(session) == {
            "scan_row_limit_default": 123
        }
        limits = await app_settings_service.get_row_limit_defaults(session)
        assert limits.scan_row_limit_default == 123
        sync_view = await session.run_sync(app_settings_service.get_service_overrides_sync)
        assert sync_view == {"scan_row_limit_default": 123}


@pytest.mark.asyncio
async def test_settings_writes_touch_only_the_operator_row() -> None:
    await _put_setting(DEFAULT_ORG_ID, {"scan_row_limit_default": 7})

    async with TestSessionLocal() as session:
        await app_settings_service.update_service_overrides(
            session, {"scan_row_limit_default": 321}
        )

    async with TestSessionLocal() as session:
        rows = {
            row.organization_id: dict(row.value)
            for row in await session.scalars(select(AppSetting))
        }
    assert rows == {
        None: {"scan_row_limit_default": 321},
        DEFAULT_ORG_ID: {"scan_row_limit_default": 7},
    }
