"""Per-organization uniqueness and no default organization (F20 PR5, GH #273).

* ``projects.slug`` and ``data_sources.name`` are unique per organization, not
  instance-wide; the create/rename availability checks agree with the
  constraints (409 in the same organization, fine in another).
* A project-bound data source or API key cannot name another organization than
  its project's (composite foreign keys onto ``projects(id, organization_id)``).
* ``organization_id`` has no ORM or server default on the four owned tables, and
  a create path with no organization bound raises instead of defaulting.
* Reserved project slugs are a 422.
* ``/projects`` and the notification bell list only the request organization's
  projects; the demo cap is counted per organization.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from tripl.main import app
from tripl.middleware.org_context import OrgContextMissing
from tripl.models.api_key import ApiKey
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.invitation import Invitation
from tripl.models.notification import Notification
from tripl.models.organization import DEFAULT_ORG_ID, Organization
from tripl.models.project import Project
from tripl.models.user import User
from tripl.schemas.project import RESERVED_PROJECT_SLUGS, ProjectCreate
from tripl.services import demo_service, project_service
from tripl.tests._members import add_org_member
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"
ACME_ID = uuid.UUID("00000000-0000-0000-0000-0000000a5e01")
ACME_SLUG = "uniq-acme"
ACME = f"/api/v1/orgs/{ACME_SLUG}"
_OWNED = (Project, DataSource, ApiKey, Invitation)


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register(client: AsyncClient, name: str) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": f"{name}@example.com", "password": PASSWORD, "name": name},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _add_acme() -> None:
    async with TestSessionLocal() as session:
        session.add(Organization(id=ACME_ID, slug=ACME_SLUG, name="Uniq Acme"))
        await session.commit()


class Stand:
    """``boss`` owns the default organization and administers acme too."""

    def __init__(self) -> None:
        self.boss = _new_client()
        self.boss_id = uuid.UUID(int=0)


@pytest.fixture
async def stand() -> AsyncIterator[Stand]:
    s = Stand()
    s.boss_id = await _register(s.boss, "uniq-boss")
    await _add_acme()
    async with TestSessionLocal() as session:
        await add_org_member(session, s.boss_id, "admin", org_id=ACME_ID)
    yield s
    await s.boss.aclose()


def _source(name: str) -> dict[str, object]:
    return {
        "name": name,
        "db_type": "clickhouse",
        "host": "clickhouse.internal.example.com",
        "port": 9440,
        "database_name": "analytics",
        "username": "tripl_ro",
        "password": "hunter2",
    }


# --- the schema -------------------------------------------------------------


@pytest.mark.parametrize("model", _OWNED, ids=lambda m: m.__name__)
def test_organization_id_has_no_default(model: type) -> None:
    column = model.__table__.c.organization_id  # type: ignore[attr-defined]
    assert column.default is None
    assert column.server_default is None
    assert column.nullable is False


async def test_an_insert_that_forgets_the_organization_fails() -> None:
    async with TestSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.insert(Project.__table__).values(
                    id=uuid.uuid4(), name="orphan", slug="orphan", description=""
                )
            )
        await session.rollback()


async def test_slug_and_source_name_are_unique_per_organization() -> None:
    await _add_acme()
    async with TestSessionLocal() as session:
        session.add_all(
            [
                Project(name="Web", slug="web", organization_id=DEFAULT_ORG_ID),
                Project(name="Web", slug="web", organization_id=ACME_ID),
                DataSource(
                    name="warehouse",
                    organization_id=DEFAULT_ORG_ID,
                    db_type="clickhouse",
                    host="h",
                    database_name="d",
                ),
                DataSource(
                    name="warehouse",
                    organization_id=ACME_ID,
                    db_type="clickhouse",
                    host="h",
                    database_name="d",
                ),
            ]
        )
        await session.commit()

    async with TestSessionLocal() as session:
        session.add(Project(name="Web again", slug="web", organization_id=ACME_ID))
        with pytest.raises(IntegrityError):
            await session.commit()

    async with TestSessionLocal() as session:
        session.add(
            DataSource(
                name="warehouse",
                organization_id=ACME_ID,
                db_type="clickhouse",
                host="h",
                database_name="d",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_project_bound_row_cannot_name_another_organization() -> None:
    await _add_acme()
    project_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        user = User(email="bound@example.com", name="b", password_hash="x")
        session.add_all([user, Project(id=project_id, name="P", slug="bound-p")])
        await session.commit()
        user_id = user.id

    async with TestSessionLocal() as session:
        session.add(
            DataSource(
                name="foreign",
                project_id=project_id,
                organization_id=ACME_ID,
                db_type="clickhouse",
                host="h",
                database_name="d",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async with TestSessionLocal() as session:
        session.add(
            ApiKey(
                user_id=user_id,
                project_id=project_id,
                organization_id=ACME_ID,
                name="foreign",
                key_prefix="tk_r_x",
                key_hash="f" * 64,
                scope="read",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    # Unbound rows are free to live in any organization.
    async with TestSessionLocal() as session:
        session.add(
            ApiKey(
                user_id=user_id,
                organization_id=ACME_ID,
                name="free",
                key_prefix="tk_r_y",
                key_hash="e" * 64,
                scope="read",
            )
        )
        await session.commit()


def test_only_rows_built_by_test_code_get_the_test_default() -> None:
    """The suite's fallback (``_default_org``) must not cover application code."""
    service_file = str(Path(__file__).resolve().parents[1] / "services" / "not_a_real_module.py")
    built = eval(  # noqa: S307 - a fixed expression, compiled under a service path
        compile("Project(name='x', slug='x')", service_file, "eval"), {"Project": Project}
    )
    assert built.organization_id is None
    assert Project(name="y", slug="y").organization_id == DEFAULT_ORG_ID


@pytest.mark.no_default_org
async def test_creating_a_project_with_no_organization_bound_raises() -> None:
    async with TestSessionLocal() as session:
        with pytest.raises(OrgContextMissing):
            await project_service.create_project(
                session, ProjectCreate(name="Nowhere", slug="nowhere")
            )
    async with TestSessionLocal() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Project)) == 0


# --- slugs over HTTP -------------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(RESERVED_PROJECT_SLUGS))
def test_reserved_slugs_are_refused_by_the_schema(slug: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        ProjectCreate(name="x", slug=slug)


async def test_reserved_slugs_are_a_422(stand: Stand) -> None:
    created = await stand.boss.post(
        "/api/v1/projects", json={"name": "Demo", "slug": "demo", "description": ""}
    )
    assert created.status_code == 422, created.text

    ok = await stand.boss.post(
        "/api/v1/projects", json={"name": "Demo-ish", "slug": "demo-ish", "description": ""}
    )
    assert ok.status_code == 201, ok.text
    renamed = await stand.boss.patch("/api/v1/projects/demo-ish", json={"slug": "orgs"})
    assert renamed.status_code == 422, renamed.text


async def test_a_project_holding_a_now_reserved_slug_stays_editable(stand: Stand) -> None:
    """Only a CHANGE to a reserved slug is refused, not the slug it already has.

    The settings form resends the unchanged slug with every save, so a project
    created before the slug was reserved must still take a name edit.
    """
    async with TestSessionLocal() as session:
        session.add(Project(name="Legacy API", slug="api"))
        await session.commit()

    saved = await stand.boss.patch(
        "/api/v1/projects/api",
        json={"name": "Legacy API v2", "slug": "api", "description": "", "timezone": "UTC"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["name"] == "Legacy API v2"
    assert saved.json()["slug"] == "api"

    moved = await stand.boss.patch("/api/v1/projects/api", json={"slug": "admin"})
    assert moved.status_code == 422, moved.text
    left = await stand.boss.patch("/api/v1/projects/api", json={"slug": "public-api"})
    assert left.status_code == 200, left.text


async def test_slug_availability_is_per_organization(stand: Stand) -> None:
    body = {"name": "Web", "slug": "web", "description": ""}
    home = await stand.boss.post("/api/v1/projects", json=body)
    assert home.status_code == 201, home.text
    away = await stand.boss.post(f"{ACME}/projects", json=body)
    assert away.status_code == 201, away.text
    assert away.json()["id"] != home.json()["id"]

    again = await stand.boss.post(f"{ACME}/projects", json=body)
    assert again.status_code == 409, again.text

    # A rename is checked in the project's own organization only.
    shop = await stand.boss.post(
        f"{ACME}/projects", json={"name": "Shop", "slug": "shop", "description": ""}
    )
    assert shop.status_code == 201, shop.text
    renamed = await stand.boss.patch("/api/v1/projects/web", json={"slug": "shop"})
    assert renamed.status_code == 200, renamed.text
    clash = await stand.boss.patch(f"{ACME}/projects/web", json={"slug": "shop"})
    assert clash.status_code == 409, clash.text


async def test_source_name_availability_is_per_organization(stand: Stand) -> None:
    home = await stand.boss.post("/api/v1/data-sources", json=_source("warehouse"))
    assert home.status_code == 201, home.text
    away = await stand.boss.post(f"{ACME}/data-sources", json=_source("warehouse"))
    assert away.status_code == 201, away.text
    again = await stand.boss.post(f"{ACME}/data-sources", json=_source("warehouse"))
    assert again.status_code == 409, again.text

    other = await stand.boss.post(f"{ACME}/data-sources", json=_source("lake"))
    assert other.status_code == 201, other.text
    # Renaming the default org's source to a name only acme holds is fine.
    renamed = await stand.boss.patch(
        f"/api/v1/data-sources/{home.json()['id']}", json={"name": "lake"}
    )
    assert renamed.status_code == 200, renamed.text
    clash = await stand.boss.patch(
        f"{ACME}/data-sources/{away.json()['id']}", json={"name": "lake"}
    )
    assert clash.status_code == 409, clash.text


# --- lists ------------------------------------------------------------------------


async def test_the_project_list_is_the_request_organizations(stand: Stand) -> None:
    home = await stand.boss.post(
        "/api/v1/projects", json={"name": "Home", "slug": "home", "description": ""}
    )
    away = await stand.boss.post(
        f"{ACME}/projects", json={"name": "Away", "slug": "away", "description": ""}
    )
    assert home.status_code == 201 and away.status_code == 201

    listed_home = await stand.boss.get("/api/v1/projects")
    assert listed_home.status_code == 200, listed_home.text
    assert {row["id"] for row in listed_home.json()} == {home.json()["id"]}

    listed_away = await stand.boss.get(f"{ACME}/projects")
    assert listed_away.status_code == 200, listed_away.text
    assert {row["id"] for row in listed_away.json()} == {away.json()["id"]}


async def test_the_bell_is_filtered_by_the_request_organization(stand: Stand) -> None:
    home = await stand.boss.post(
        "/api/v1/projects", json={"name": "Home", "slug": "home", "description": ""}
    )
    away = await stand.boss.post(
        f"{ACME}/projects", json={"name": "Away", "slug": "away", "description": ""}
    )
    assert home.status_code == 201 and away.status_code == 201
    ids: dict[str, uuid.UUID] = {}
    async with TestSessionLocal() as session:
        for label, project_id in (("home", home.json()["id"]), ("away", away.json()["id"])):
            note = Notification(
                user_id=stand.boss_id,
                project_id=uuid.UUID(project_id),
                kind="comment",
                entity_type="event",
                entity_id=uuid.uuid4(),
                title=f"note {label}",
                url=f"/p/{label}",
                created_at=datetime.now(UTC),
            )
            session.add(note)
            await session.flush()
            ids[label] = note.id
        await session.commit()

    home_bell = await stand.boss.get("/api/v1/me/notifications")
    assert home_bell.status_code == 200, home_bell.text
    assert [item["id"] for item in home_bell.json()["items"]] == [str(ids["home"])]
    away_bell = await stand.boss.get(f"{ACME}/me/notifications")
    assert [item["id"] for item in away_bell.json()["items"]] == [str(ids["away"])]

    count = await stand.boss.get(f"{ACME}/me/notifications/unread-count")
    assert count.status_code == 200, count.text
    assert count.json()["unread"] == 1

    # Marking everything read under acme leaves the default org's note unread.
    marked = await stand.boss.post(f"{ACME}/me/notifications/read", json={"all": True})
    assert marked.status_code == 200, marked.text
    assert marked.json()["updated"] == 1
    still = await stand.boss.get("/api/v1/me/notifications/unread-count")
    assert still.json()["unread"] == 1


async def test_the_demo_cap_is_counted_per_organization() -> None:
    await _add_acme()
    async with TestSessionLocal() as session:
        user = User(email="demo-cap@example.com", name="d", password_hash="x")
        session.add(user)
        await session.flush()
        for index in range(demo_service.MAX_DEMOS_PER_CREATOR):
            session.add(
                Project(
                    name=f"Demo Project {index + 1}",
                    slug=f"demo-cap-{index}",
                    is_demo=True,
                    generation_status=ProjectGenerationStatus.ready.value,
                    created_by_user_id=user.id,
                    organization_id=DEFAULT_ORG_ID,
                )
            )
        await session.commit()

        home = await demo_service._live_demo_names(session, user.id, DEFAULT_ORG_ID)
        away = await demo_service._live_demo_names(session, user.id, ACME_ID)
    assert len(home) == demo_service.MAX_DEMOS_PER_CREATOR
    assert away == []
