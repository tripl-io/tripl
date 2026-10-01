"""Organization schema constraints and settings scope."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
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
from tripl.models.data_source import DataSource
from tripl.models.organization import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_SLUG,
    Organization,
    OrganizationMember,
)
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services import app_settings_service
from tripl.tests._default_org import seed_default_organization
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_engine("sqlite://")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        seed_default_organization(connection)
    try:
        yield engine
    finally:
        engine.dispose()


def _user(session: Session, email: str) -> uuid.UUID:
    user = User(email=email, name=email, password_hash="x")
    session.add(user)
    session.flush()
    return user.id


# --- the schema -----------------------------------------------------------------


def test_organization_membership_is_unique_per_user(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        user = _user(session, "u@example.com")
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
        # Since PR4 the first user of a self-hosted instance is its platform admin.
        assert set(await session.scalars(select(User.is_platform_admin))) == {True}


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
