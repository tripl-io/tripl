"""The organization revision (``b8d0f2a4c6e8``) on PostgreSQL, through asyncpg.

The SQLite tests in ``test_organizations_schema`` prove the backfill's logic;
they cannot prove the DDL or the driver. This runs the revision's real
``downgrade`` and ``upgrade`` on a real PostgreSQL over the driver production
migrates with: from the model schema down to the pre-organization shape, rows
seeded as the previous release would hold them, up again with the backfill,
the refused downgrade, a clean downgrade, and a final upgrade.

Gated like the other ``*_pg`` tests: skipped without ``TRIPL_TEST_PG_URL``, a
failure when ``TRIPL_TEST_PG_REQUIRED=1`` says CI must not skip it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine
from sqlalchemy.ext.asyncio import create_async_engine

from tripl.config import settings
from tripl.models import Base
from tripl.tests._legacy_role_schema import drop_legacy_role_schema
from tripl.tests.test_alembic_revisions import _load_migration
from tripl.tests.test_alert_digest_concurrency_pg import _PG_URL, _engine_or_skip

pytestmark = pytest.mark.postgres

MIGRATION = "b8d0f2a4c6e8_organizations_schema_and_default_org.py"
# Revisions after ``MIGRATION`` whose schema the model metadata already carries,
# newest first. The fixture builds the HEAD schema with ``create_all``, so these
# must be unwound before ``MIGRATION.downgrade`` runs: ``doc_files`` (F22) keeps
# an ``organization_id`` foreign key that would block dropping ``organizations``.
# A new revision that depends on the organization schema belongs here.
LATER_MIGRATIONS: tuple[str, ...] = (
    "a2c4e6f8b0d3_drop_legacy_instance_role.py",
    "f8a0c2e4b6d9_audit_webhooks.py",
    "f5b7d9e1a3c4_org_scim.py",
    "e3a5c7d9f1b2_org_oidc_sso.py",
    "d2f4a6c8e0b1_default_project_role.py",
    "c1e3a5b7d9f2_platform_console.py",
    "b8d0f2a4c6e9_email_verification.py",
    "a7c9e1f3b5d8_organization_groups.py",
    "f6c8a0b2d4e7_per_org_photo_storage.py",
    "d4e8f1a2b3c5_incident_summary_hash_without_hrefs.py",
    "e5b7d9f1a3c6_organization_status.py",
    "d4a6c8e0f2b4_per_org_uniqueness_and_no_org_default.py",
    "c9e1a3b5d7f9_org_roles_backfill_and_viewer_cap.py",
    "c3f5a7b9d1e2_docs_catalog.py",
)
DEFAULT_ORG = "00000000-0000-0000-0000-00000000d0f1"
_PSYCOPG_PREFIX = "postgresql+psycopg://"
_ORG_COLUMNS = {
    "projects": "NO",
    "data_sources": "NO",
    "api_keys": "NO",
    "invitations": "NO",
    "audit_log": "YES",
    "app_settings": "YES",
}


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    engine = _engine_or_skip()
    drop_legacy_role_schema(engine)
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        # The downgrades put ``user_role`` back; the model metadata no longer knows it.
        drop_legacy_role_schema(engine)
        engine.dispose()


def _asyncpg_url() -> str:
    assert _PG_URL is not None
    if not _PG_URL.startswith(_PSYCOPG_PREFIX):
        pytest.skip(
            "TRIPL_TEST_PG_URL is not a postgresql+psycopg URL; cannot derive the asyncpg one"
        )
    return "postgresql+asyncpg://" + _PG_URL.removeprefix(_PSYCOPG_PREFIX)


async def _run(step: Callable[[], None]) -> None:
    """One direction of the revision, with ``op`` bound, over asyncpg."""

    def _in_context(connection: Connection) -> None:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            step()

    engine = create_async_engine(_asyncpg_url())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_in_context)
    finally:
        await engine.dispose()


def _seed_previous_release(engine: Engine) -> dict[str, uuid.UUID]:
    """Rows as the release before organizations holds them."""
    ids = {name: uuid.uuid4() for name in ("owner", "editor", "viewer", "alpha", "beta", "key")}
    with engine.begin() as connection:
        for name, role in (("owner", "owner"), ("editor", "editor"), ("viewer", "viewer")):
            connection.execute(
                sa.text(
                    "INSERT INTO users (id, email, name, password_hash, role) "
                    "VALUES (:id, :email, :name, 'x', CAST(:role AS user_role))"
                ),
                {"id": ids[name], "email": f"{name}@example.com", "name": name, "role": role},
            )
        for slug in ("alpha", "beta"):
            connection.execute(
                sa.text(
                    "INSERT INTO projects (id, name, slug, description) "
                    "VALUES (:id, :name, :slug, '')"
                ),
                {"id": ids[slug], "name": slug, "slug": slug},
            )
        for project, user, role in (
            ("alpha", "viewer", "editor"),
            ("beta", "viewer", "viewer"),
            ("alpha", "editor", "editor"),
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO project_members (id, project_id, user_id, role) "
                    "VALUES (:id, :project_id, :user_id, CAST(:role AS project_member_role))"
                ),
                {
                    "id": uuid.uuid4(),
                    "project_id": ids[project],
                    "user_id": ids[user],
                    "role": role,
                },
            )
        connection.execute(
            sa.text(
                "INSERT INTO api_keys (id, user_id, project_id, name, key_prefix, key_hash, scope) "
                "VALUES (:id, :user_id, :project_id, 'k', 'tk_', :key_hash, 'read')"
            ),
            {
                "id": ids["key"],
                "user_id": ids["owner"],
                "project_id": ids["alpha"],
                "key_hash": "a" * 64,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO audit_log (id, action, target_type, payload) "
                "VALUES (:id, 'settings.update', 'settings', '{}')"
            ),
            {"id": uuid.uuid4()},
        )
        connection.execute(
            sa.text("INSERT INTO app_settings (id, key, value) VALUES (:id, 'service', '{}')"),
            {"id": uuid.uuid4()},
        )
    return ids


def _org_column_nullability(engine: Engine) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT table_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'organization_id'"
            )
        ).all()
    return {table: nullable for table, nullable in rows if table in _ORG_COLUMNS}


def _enum_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as connection:
        return (
            connection.execute(
                sa.text("SELECT 1 FROM pg_type WHERE typname = :name"), {"name": name}
            ).first()
            is not None
        )


def _revision_of(filename: str) -> str:
    return filename.split("_", 1)[0]


def test_later_migrations_lists_every_revision_after_this_one() -> None:
    """``LATER_MIGRATIONS`` is the chain from head down to ``MIGRATION``, newest first.

    Plain (no Postgres needed), so a revision added on top without an entry here
    fails in every job rather than only in the Postgres one.
    """
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    script = ScriptDirectory.from_config(config)
    # Exclusive of the lower bound: the revisions strictly above MIGRATION.
    chain = [
        revision.revision for revision in script.iterate_revisions("heads", _revision_of(MIGRATION))
    ]
    assert chain == [_revision_of(filename) for filename in LATER_MIGRATIONS]


@pytest.mark.asyncio
async def test_organizations_revision_round_trips_on_postgres(
    pg_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration: ModuleType = _load_migration("organizations_migration_pg", MIGRATION)
    monkeypatch.setattr(settings, "deployment_mode", "self_hosted")
    monkeypatch.setattr(settings, "platform_admin_emails", [])

    # Down from the model schema to the shape the previous release runs on:
    # first the later revisions, so the schema is exactly this revision's head.
    for index, filename in enumerate(LATER_MIGRATIONS):
        later: ModuleType = _load_migration(f"organizations_migration_pg_later_{index}", filename)
        await _run(later.downgrade)
    await _run(migration.downgrade)
    assert _org_column_nullability(pg_engine) == {}
    assert not _enum_exists(pg_engine, "organization_member_role")
    ids = _seed_previous_release(pg_engine)

    await _run(migration.upgrade)

    assert _org_column_nullability(pg_engine) == _ORG_COLUMNS
    with pg_engine.connect() as connection:
        members = dict(
            connection.execute(
                sa.text(
                    "SELECT user_id, role::text FROM organization_members "
                    "WHERE organization_id = CAST(:org AS uuid)"
                ),
                {"org": DEFAULT_ORG},
            ).all()
        )
        assert members == {ids["owner"]: "owner", ids["editor"]: "member", ids["viewer"]: "member"}
        project_roles = dict(
            connection.execute(
                sa.text("SELECT project_id, role::text FROM project_members WHERE user_id = :u"),
                {"u": ids["viewer"]},
            ).all()
        )
        # Left as the previous release wrote them: this revision does not cap
        # them; c9e1a3b5d7f9 does (test_org_roles_migration_pg).
        assert project_roles == {ids["alpha"]: "editor", ids["beta"]: "viewer"}
        editor_role = connection.execute(
            sa.text("SELECT role::text FROM project_members WHERE user_id = :u"),
            {"u": ids["editor"]},
        ).scalar_one()
        assert editor_role == "editor"
        admins = set(
            connection.execute(sa.text("SELECT id FROM users WHERE is_platform_admin")).scalars()
        )
        assert admins == {ids["owner"]}
        for table in ("projects", "api_keys", "audit_log"):
            orgs = set(
                connection.execute(sa.text(f"SELECT organization_id::text FROM {table}")).scalars()
            )
            assert orgs == {DEFAULT_ORG}, table

    # A container still on the previous release inserts without the column.
    with pg_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO projects (id, name, slug, description) VALUES (:id, 'old', 'old', '')"
            ),
            {"id": uuid.uuid4()},
        )
        org = connection.execute(
            sa.text("SELECT organization_id::text FROM projects WHERE slug = 'old'")
        ).scalar_one()
        assert org == DEFAULT_ORG
        # Per-scope uniqueness: the same key at organization scope is allowed.
        connection.execute(
            sa.text(
                "INSERT INTO app_settings (id, key, value, organization_id) "
                "VALUES (:id, 'service', '{}', CAST(:org AS uuid))"
            ),
            {"id": uuid.uuid4(), "org": DEFAULT_ORG},
        )

    # An organization-scoped setting blocks the downgrade, leaving the schema intact.
    with pytest.raises(RuntimeError, match="organization-scoped"):
        await _run(migration.downgrade)
    assert _org_column_nullability(pg_engine) == _ORG_COLUMNS

    with pg_engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM app_settings WHERE organization_id IS NOT NULL"))
    await _run(migration.downgrade)
    assert _org_column_nullability(pg_engine) == {}
    assert not _enum_exists(pg_engine, "organization_member_role")
    with pg_engine.connect() as connection:
        unique_key = connection.execute(
            sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_app_settings_key'")
        ).scalar_one()
        assert "UNIQUE" in unique_key

    # And up again: the downgrade left nothing a second upgrade trips over.
    await _run(migration.upgrade)
    assert _org_column_nullability(pg_engine) == _ORG_COLUMNS
