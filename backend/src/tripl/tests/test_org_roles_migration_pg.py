"""F20 PR4's data migration (``c9e1a3b5d7f9``): backfill again, cap viewers.

``users.role`` stops being read in PR4, so the revision must leave every user
with a default-organization membership (including accounts created after PR1's
snapshot, tripl-t8q4 / tripl-xyzg), every pending invitation with an
``org_role``, and every former instance viewer's project rows at ``viewer`` —
the cap ``effective_role`` used to apply at read time.

The logic is driven on SQLite built from the models as they stood before the
legacy role was dropped (``_legacy_role_schema``; always runs); the whole
revision's ``upgrade`` runs once more on PostgreSQL over asyncpg, the driver
production migrates with (skipped without ``TRIPL_TEST_PG_URL``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from tripl.models.invitation import Invitation
from tripl.models.organization import DEFAULT_ORG_ID, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.tests._legacy_role_schema import (
    create_legacy_role_schema,
    drop_legacy_role_schema,
    set_invitation_roles,
    set_user_role,
)
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.test_alembic_revisions import _load_migration

MIGRATION = "c9e1a3b5d7f9_org_roles_backfill_and_viewer_cap.py"


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load_migration("org_roles_migration", MIGRATION)


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = create_engine("sqlite://")
    enable_sqlite_foreign_keys(engine)
    create_legacy_role_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _user(session: Session, email: str, role: str) -> uuid.UUID:
    user = User(email=email, name=email, password_hash="x")
    session.add(user)
    session.flush()
    set_user_role(session, user.id, role)
    return user.id


def _invitation(session: Session, email: str, role: str, org_role: str | None) -> None:
    invitation = Invitation(
        email=email,
        token_hash=uuid.uuid4().hex * 2,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(invitation)
    session.flush()
    set_invitation_roles(session, invitation.id, role, org_role=org_role)


def _seed(engine: Engine) -> dict[str, uuid.UUID]:
    """An instance as PR1-PR3 leaves it.

    ``old_owner`` went through PR1's backfill; ``late_owner``, ``late_editor``
    and ``late_viewer`` registered (or were promoted) afterwards and have no
    membership and no flag; ``viewer`` holds an ``editor`` row that the old
    read-time cap turned into ``viewer``.
    """
    with Session(engine) as session, session.begin():
        ids = {
            "old_owner": _user(session, "old-owner@example.com", "owner"),
            "late_owner": _user(session, "late-owner@example.com", "owner"),
            "late_editor": _user(session, "late-editor@example.com", "editor"),
            "late_viewer": _user(session, "late-viewer@example.com", "viewer"),
        }
        session.add(
            OrganizationMember(
                organization_id=DEFAULT_ORG_ID, user_id=ids["old_owner"], role="owner"
            )
        )
        for slug in ("alpha", "beta"):
            project = Project(name=slug, slug=slug)
            session.add(project)
            session.flush()
            ids[slug] = project.id
        session.add_all(
            [
                ProjectMember(project_id=ids["alpha"], user_id=ids["late_viewer"], role="editor"),
                ProjectMember(project_id=ids["beta"], user_id=ids["late_viewer"], role="viewer"),
                ProjectMember(project_id=ids["alpha"], user_id=ids["late_editor"], role="editor"),
            ]
        )
        for email, role in (("inv-owner@example.com", "owner"), ("inv-view@example.com", "viewer")):
            _invitation(session, email, role, None)
        _invitation(session, "inv-set@example.com", "editor", "admin")
    return ids


def _upgrade_logic(engine: Engine, migration: ModuleType) -> dict[str, int]:
    with engine.begin() as connection:
        counts: dict[str, int] = migration.backfill_organizations(
            connection, deployment_mode="self_hosted", platform_admin_emails=[]
        )
        counts["viewer_rows_capped"] = migration.cap_viewer_project_roles(connection)
    return counts


def _state(engine: Engine) -> tuple[dict[uuid.UUID, str], set[uuid.UUID], dict, dict]:
    with Session(engine) as session:
        members = {
            user_id: str(role)
            for user_id, role in session.execute(
                select(OrganizationMember.user_id, OrganizationMember.role).where(
                    OrganizationMember.organization_id == DEFAULT_ORG_ID
                )
            ).all()
        }
        admins = set(session.scalars(select(User.id).where(User.is_platform_admin.is_(True))))
        rows = {
            (project_id, user_id): str(role)
            for project_id, user_id, role in session.execute(
                select(ProjectMember.project_id, ProjectMember.user_id, ProjectMember.role)
            ).all()
        }
        invitations = {
            email: str(org_role)
            for email, org_role in session.execute(
                select(Invitation.email, Invitation.org_role)
            ).all()
        }
    return members, admins, rows, invitations


def test_the_revision_follows_the_docs_catalog(migration: ModuleType) -> None:
    assert migration.down_revision == "c3f5a7b9d1e2"


def test_late_accounts_get_memberships_flags_and_viewers_are_capped(
    engine: Engine, migration: ModuleType
) -> None:
    ids = _seed(engine)
    counts = _upgrade_logic(engine, migration)

    members, admins, rows, invitations = _state(engine)
    assert members == {
        ids["old_owner"]: "owner",
        ids["late_owner"]: "owner",
        ids["late_editor"]: "member",
        ids["late_viewer"]: "member",
    }
    # Self-hosted: every instance owner operates the instance, late ones too.
    assert admins == {ids["old_owner"], ids["late_owner"]}
    assert rows == {
        (ids["alpha"], ids["late_viewer"]): "viewer",
        (ids["beta"], ids["late_viewer"]): "viewer",
        # An editor's editor row is theirs to keep.
        (ids["alpha"], ids["late_editor"]): "editor",
    }
    assert invitations == {
        "inv-owner@example.com": "owner",
        "inv-view@example.com": "member",
        # An org_role already chosen is never overwritten.
        "inv-set@example.com": "admin",
    }
    assert counts["members_added"] == 3
    assert counts["platform_admins_granted"] == 2
    assert counts["invitation_roles_filled"] == 2
    assert counts["viewer_rows_capped"] == 1


def test_the_revision_is_idempotent_and_only_adds(engine: Engine, migration: ModuleType) -> None:
    ids = _seed(engine)
    _upgrade_logic(engine, migration)
    # An owner demoted to member after the migration keeps that demotion: a
    # second run adds nothing and rewrites nothing.
    with Session(engine) as session, session.begin():
        session.execute(
            sa.update(OrganizationMember)
            .where(OrganizationMember.user_id == ids["late_owner"])
            .values(role="member")
        )
    before = _state(engine)
    again = _upgrade_logic(engine, migration)
    assert _state(engine) == before
    assert again == {
        "organization_created": 0,
        "members_added": 0,
        "platform_admins_granted": 0,
        "invitation_roles_filled": 0,
        "viewer_rows_capped": 0,
    }


def test_hosted_grants_platform_admin_only_from_the_list(
    engine: Engine, migration: ModuleType
) -> None:
    ids = _seed(engine)
    with engine.begin() as connection:
        migration.backfill_organizations(
            connection,
            deployment_mode="hosted",
            platform_admin_emails=[" Late-Editor@example.com "],
        )
    _members, admins, _rows, _invitations = _state(engine)
    assert admins == {ids["late_editor"]}


def test_an_unknown_deployment_mode_is_refused(engine: Engine, migration: ModuleType) -> None:
    with engine.begin() as connection, pytest.raises(RuntimeError, match="DEPLOYMENT_MODE"):
        migration.backfill_organizations(
            connection, deployment_mode="cloud", platform_admin_emails=[]
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_the_revision_upgrades_on_postgres_over_asyncpg(migration: ModuleType) -> None:
    """The real ``upgrade`` on PostgreSQL: enum-typed binds cast, no repeated binds."""
    from tripl.tests.test_alert_digest_concurrency_pg import _engine_or_skip
    from tripl.tests.test_organizations_migration_pg import _run

    pg_engine = _engine_or_skip()
    drop_legacy_role_schema(pg_engine)
    create_legacy_role_schema(pg_engine)
    try:
        ids = _seed(pg_engine)
        await _run(migration.upgrade)
        members, admins, rows, invitations = _state(pg_engine)
        assert members[ids["late_viewer"]] == "member"
        assert members[ids["late_owner"]] == "owner"
        assert admins == {ids["old_owner"], ids["late_owner"]}
        assert rows[(ids["alpha"], ids["late_viewer"])] == "viewer"
        assert rows[(ids["alpha"], ids["late_editor"])] == "editor"
        assert invitations["inv-view@example.com"] == "member"

        await _run(migration.upgrade)
        assert _state(pg_engine) == (members, admins, rows, invitations)
        await _run(migration.downgrade)
    finally:
        drop_legacy_role_schema(pg_engine)
        pg_engine.dispose()
