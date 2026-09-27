"""organizations: schema and the default-organization backfill (F20 PR1, #273)

Adds the tenant layer above projects WITHOUT changing behaviour. Nothing reads
any of this for a permission decision yet: ``users.role`` stays the source of
truth until the gates switch over in a later PR.

Schema:

* ``organizations`` and ``organization_members`` (role: the new native enum
  ``organization_member_role`` = owner | admin | member).
* ``users.is_platform_admin`` (false).
* ``organization_id`` on ``projects``, ``data_sources``, ``api_keys`` and
  ``invitations``: NOT NULL, ON DELETE RESTRICT, with a server default naming
  the default organization. The server default is what keeps the previous
  release's containers — still serving while this runs, and knowing nothing
  about organizations — able to insert.
* ``audit_log.organization_id``: NULLABLE (platform-level actions will have no
  organization), ON DELETE SET NULL, indexed with ``(created_at, id)``.
* ``invitations.org_role``: nullable, reserved.
* ``app_settings.organization_id``: nullable (NULL = operator scope). The
  ``UNIQUE(key)`` index becomes two partial unique indexes, one per scope.

Data (:func:`backfill_organizations`, dialect-neutral Core so the tests drive it
on SQLite and on PostgreSQL through asyncpg):

* the default organization, fixed id ``00000000-0000-0000-0000-00000000d0f1``;
* every user becomes a member: instance owner -> ``owner`` (there can be
  several), editor -> ``member``, viewer -> ``member``;
* platform admins: with ``DEPLOYMENT_MODE=self_hosted`` (the default) every
  instance owner; with ``hosted`` only the users whose email is listed in
  ``PLATFORM_ADMIN_EMAILS``;
* every existing project, data source, invitation and audit row goes to the
  default organization; a project-bound API key takes its project's
  organization, any other key the default one;
* pending invitations get an ``org_role`` by the same mapping as users.

``project_members`` is NOT touched. A viewer can hold an ``editor`` row today;
``effective_role`` caps it through ``users.role``, and promoting that user to
editor restores the edit grant. Capping those rows belongs with the PR that
stops consulting ``users.role``, next to the write-path rule that keeps the cap
true.

The backfill is a snapshot. Nothing in the application writes
``organization_members``, ``users.is_platform_admin`` or ``invitations.org_role``
yet, so users and invitations created after this revision (including by the
previous release during the deploy) have none of them, and an owner promoted
later is not flagged. ``DEPLOYMENT_MODE`` and ``PLATFORM_ADMIN_EMAILS`` are read
once, here; changing them afterwards grants and revokes nothing. The PR that
starts READING these must first re-run :func:`backfill_organizations` (it is
idempotent and only adds) in its own migration, and add the write paths.

Downgrade removes all of it. It refuses to run while any organization-scoped
``app_settings`` row exists: dropping the column would silently turn that row
into an operator setting, or leave two rows under one key where ``UNIQUE(key)``
cannot be restored.

Revision ID: b8d0f2a4c6e8
Revises: d7e9f1a3b5c7
Create Date: 2026-09-27 12:00:00.000000

"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection

revision: str = "b8d0f2a4c6e8"
down_revision: str | None = "d7e9f1a3b5c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies, not imports: this revision must keep meaning what it meant when
# it ran, whatever the application's constants become.
DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000d0f1")
DEFAULT_ORG_SLUG = "default"
DEFAULT_ORG_NAME = "Default organization"
SELF_HOSTED = "self_hosted"
HOSTED = "hosted"

_ORG_ROLE_ENUM = "organization_member_role"
_ORG_ROLE_VALUES = ("owner", "admin", "member")
_USER_ROLE_VALUES = ("owner", "editor", "viewer")
_PROJECT_ROLE_VALUES = ("editor", "viewer")

# Tables whose organization_id is NOT NULL + RESTRICT. audit_log is handled on
# its own: nullable, SET NULL, and a composite index instead of a plain one.
_OWNED_TABLES = ("projects", "data_sources", "api_keys", "invitations")

# Typed enums for the data statements: on PostgreSQL the binds are cast to the
# native type (comparing a native enum to a varchar is an error there); on SQLite
# they are plain strings. ``create_constraint=False`` — nothing here emits DDL.
_user_role = sa.Enum(*_USER_ROLE_VALUES, name="user_role", create_constraint=False)
_org_role = sa.Enum(*_ORG_ROLE_VALUES, name=_ORG_ROLE_ENUM, create_constraint=False)

_organizations = sa.table(
    "organizations",
    sa.column("id", sa.Uuid()),
    sa.column("slug", sa.String()),
    sa.column("name", sa.String()),
    sa.column("members_can_create_projects", sa.Boolean()),
)
_organization_members = sa.table(
    "organization_members",
    sa.column("id", sa.Uuid()),
    sa.column("organization_id", sa.Uuid()),
    sa.column("user_id", sa.Uuid()),
    sa.column("role", _org_role),
)
_users = sa.table(
    "users",
    sa.column("id", sa.Uuid()),
    sa.column("email", sa.String()),
    sa.column("role", _user_role),
    sa.column("is_platform_admin", sa.Boolean()),
)
_projects = sa.table(
    "projects", sa.column("id", sa.Uuid()), sa.column("organization_id", sa.Uuid())
)
_api_keys = sa.table(
    "api_keys",
    sa.column("project_id", sa.Uuid()),
    sa.column("organization_id", sa.Uuid()),
)
_invitations = sa.table(
    "invitations",
    sa.column("role", _user_role),
    sa.column("org_role", _org_role),
    sa.column("organization_id", sa.Uuid()),
)
_app_settings = sa.table(
    "app_settings",
    sa.column("key", sa.String()),
    sa.column("organization_id", sa.Uuid()),
)


def _org_role_for(user_role: str) -> str:
    return "owner" if user_role == "owner" else "member"


def backfill_organizations(
    connection: Connection,
    *,
    deployment_mode: str,
    platform_admin_emails: Sequence[str],
) -> dict[str, int]:
    """Every data step of this revision. Idempotent; returns counts for the log.

    Only ADDS: a membership that already exists, a platform-admin flag that is
    already set, an organization already assigned, are all left as they are.
    """
    if deployment_mode not in (SELF_HOSTED, HOSTED):
        raise RuntimeError(
            f"DEPLOYMENT_MODE must be {SELF_HOSTED!r} or {HOSTED!r}, not {deployment_mode!r}"
        )
    counts: dict[str, int] = {}

    exists = connection.execute(
        sa.select(_organizations.c.id).where(_organizations.c.id == DEFAULT_ORG_ID)
    ).first()
    if exists is None:
        connection.execute(
            sa.insert(_organizations).values(
                id=DEFAULT_ORG_ID,
                slug=DEFAULT_ORG_SLUG,
                name=DEFAULT_ORG_NAME,
                members_can_create_projects=True,
            )
        )
    counts["organization_created"] = int(exists is None)

    already = set(
        connection.execute(
            sa.select(_organization_members.c.user_id).where(
                _organization_members.c.organization_id == DEFAULT_ORG_ID
            )
        ).scalars()
    )
    users = connection.execute(sa.select(_users.c.id, _users.c.role).order_by(_users.c.id)).all()
    new_members = [
        {
            "id": uuid.uuid4(),
            "organization_id": DEFAULT_ORG_ID,
            "user_id": user_id,
            "role": _org_role_for(str(role)),
        }
        for user_id, role in users
        if user_id not in already
    ]
    if new_members:
        connection.execute(sa.insert(_organization_members), new_members)
    counts["members_added"] = len(new_members)

    if deployment_mode == SELF_HOSTED:
        admins = _users.c.role == "owner"
    else:
        emails = sorted({email.strip().lower() for email in platform_admin_emails if email.strip()})
        admins = _users.c.email.in_(emails) if emails else sa.false()
    granted = connection.execute(
        sa.update(_users)
        .where(admins, _users.c.is_platform_admin.is_not(True))
        .values(is_platform_admin=True)
    )
    counts["platform_admins_granted"] = granted.rowcount

    for table_name in (*_OWNED_TABLES, "audit_log"):
        table = sa.table(table_name, sa.column("organization_id", sa.Uuid()))
        connection.execute(
            sa.update(table)
            .where(table.c.organization_id.is_(None))
            .values(organization_id=DEFAULT_ORG_ID)
        )

    # After the default fill above, so a key bound to a project always ends up
    # with that project's organization whatever the fill wrote.
    project_org = (
        sa.select(_projects.c.organization_id)
        .where(_projects.c.id == _api_keys.c.project_id)
        .scalar_subquery()
    )
    connection.execute(
        sa.update(_api_keys)
        .where(_api_keys.c.project_id.is_not(None))
        .values(organization_id=project_org)
    )

    for user_role, org_role in (("owner", "owner"), ("editor", "member"), ("viewer", "member")):
        connection.execute(
            sa.update(_invitations)
            .where(_invitations.c.org_role.is_(None), _invitations.c.role == user_role)
            .values(org_role=org_role)
        )
    return counts


def organization_scoped_setting_keys(connection: Connection) -> list[str]:
    """Keys held at organization scope — what makes this revision irreversible."""
    return sorted(
        set(
            connection.execute(
                sa.select(_app_settings.c.key).where(_app_settings.c.organization_id.is_not(None))
            ).scalars()
        )
    )


def _default_org_literal() -> sa.TextClause:
    return sa.text(f"'{DEFAULT_ORG_ID}'::uuid")


def upgrade() -> None:
    from tripl.config import settings

    bind = op.get_bind()
    postgresql.ENUM(*_ORG_ROLE_VALUES, name=_ORG_ROLE_ENUM).create(bind, checkfirst=True)
    org_role_type = postgresql.ENUM(*_ORG_ROLE_VALUES, name=_ORG_ROLE_ENUM, create_type=False)
    project_role_type = postgresql.ENUM(
        *_PROJECT_ROLE_VALUES, name="project_member_role", create_type=False
    )

    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("default_project_role", project_role_type, nullable=True),
        sa.Column(
            "members_can_create_projects",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)

    op.create_table(
        "organization_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", org_role_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_organization_member"),
    )
    op.create_index(
        "ix_organization_members_user_id", "organization_members", ["user_id"], unique=False
    )

    op.add_column(
        "users",
        sa.Column("is_platform_admin", sa.Boolean(), server_default=sa.false(), nullable=True),
    )

    # Nullable first, WITH the server default: PostgreSQL stores a constant
    # default in the catalog instead of rewriting the table, and existing rows
    # read it back. The backfill then covers anything the default did not, and
    # only after it do the columns become NOT NULL and grow their foreign keys.
    for table in _OWNED_TABLES:
        op.add_column(
            table,
            sa.Column(
                "organization_id", sa.Uuid(), server_default=_default_org_literal(), nullable=True
            ),
        )
    op.add_column(
        "audit_log",
        sa.Column(
            "organization_id", sa.Uuid(), server_default=_default_org_literal(), nullable=True
        ),
    )
    op.add_column("invitations", sa.Column("org_role", org_role_type, nullable=True))
    op.add_column("app_settings", sa.Column("organization_id", sa.Uuid(), nullable=True))

    counts = backfill_organizations(
        bind,
        deployment_mode=settings.deployment_mode,
        platform_admin_emails=settings.platform_admin_emails,
    )
    log = logging.getLogger("alembic.runtime.migration")
    log.info("organizations backfill (%s): %s", settings.deployment_mode, counts)
    if settings.deployment_mode == HOSTED and not any(
        email.strip() for email in settings.platform_admin_emails
    ):
        log.warning(
            "DEPLOYMENT_MODE=hosted with no PLATFORM_ADMIN_EMAILS: nobody was made a "
            "platform admin. The value is applied only by this upgrade."
        )

    op.alter_column("users", "is_platform_admin", nullable=False)
    for table in _OWNED_TABLES:
        op.alter_column(table, "organization_id", nullable=False)
        op.create_foreign_key(
            f"{table}_organization_id_fkey",
            table,
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(f"ix_{table}_organization_id", table, ["organization_id"], unique=False)

    op.create_foreign_key(
        "audit_log_organization_id_fkey",
        "audit_log",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_audit_log_organization_created",
        "audit_log",
        ["organization_id", "created_at", "id"],
        unique=False,
    )

    op.create_foreign_key(
        "app_settings_organization_id_fkey",
        "app_settings",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_index("ix_app_settings_key", table_name="app_settings")
    op.create_index(
        "uq_app_settings_operator_key",
        "app_settings",
        ["key"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NULL"),
    )
    op.create_index(
        "uq_app_settings_organization_key",
        "app_settings",
        ["organization_id", "key"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    scoped = organization_scoped_setting_keys(bind)
    if scoped:
        raise RuntimeError(
            "Cannot downgrade past b8d0f2a4c6e8: app_settings holds organization-scoped "
            f"rows (organization_id IS NOT NULL) for keys {scoped}. Dropping the column "
            "would turn them into operator settings and UNIQUE(key) could not be "
            "restored. Delete those rows first, after copying anything worth keeping."
        )

    op.drop_index("uq_app_settings_organization_key", table_name="app_settings")
    op.drop_index("uq_app_settings_operator_key", table_name="app_settings")
    op.create_index("ix_app_settings_key", "app_settings", ["key"], unique=True)
    op.drop_constraint("app_settings_organization_id_fkey", "app_settings", type_="foreignkey")
    op.drop_column("app_settings", "organization_id")

    op.drop_index("ix_audit_log_organization_created", table_name="audit_log")
    op.drop_constraint("audit_log_organization_id_fkey", "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "organization_id")

    op.drop_column("invitations", "org_role")
    for table in _OWNED_TABLES:
        op.drop_index(f"ix_{table}_organization_id", table_name=table)
        op.drop_constraint(f"{table}_organization_id_fkey", table, type_="foreignkey")
        op.drop_column(table, "organization_id")

    op.drop_column("users", "is_platform_admin")
    op.drop_index("ix_organization_members_user_id", table_name="organization_members")
    op.drop_table("organization_members")
    op.drop_index("ix_organizations_slug", table_name="organizations")
    op.drop_table("organizations")
    postgresql.ENUM(name=_ORG_ROLE_ENUM).drop(bind, checkfirst=True)
