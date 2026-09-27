"""organization roles become the source of truth: re-run the backfill, cap viewers (F20 PR4)

From this release on every permission check reads ``organization_members`` and
``project_members``; ``users.role`` is no longer read. Two data steps make the
switch lossless:

1. :func:`backfill_organizations` — a frozen copy of the PR1 backfill
   (``b8d0f2a4c6e8``). PR1's run was a snapshot: users and invitations created
   after it (tripl-t8q4, tripl-xyzg) have no default-organization membership,
   no platform-admin flag and no ``invitations.org_role``, and an owner promoted
   since is not flagged. Re-running it is idempotent and only adds:
   instance owner -> org ``owner``, editor/viewer -> ``member``; platform admin
   for every instance owner when ``DEPLOYMENT_MODE=self_hosted``, else for the
   ``PLATFORM_ADMIN_EMAILS``; ``invitations.org_role`` where NULL.
2. :func:`cap_viewer_project_roles` — an instance ``viewer`` used to be capped
   at project role ``viewer`` whatever their ``project_members`` row said. The
   cap read ``users.role``; with that gone the row is authoritative, so every
   ``editor`` row of an instance viewer becomes ``viewer`` here, once. After
   this the project role alone decides (an org admin who later promotes such a
   member to ``editor`` on a project means it).

Both are dialect-neutral Core on ``sa.table()`` so the tests drive them on
SQLite and on PostgreSQL through asyncpg; no bind parameter is repeated in raw
SQL.

Downgrade is a no-op: the memberships and flags are what the previous release
already expects to find (it wrote the same shape in PR1), and the capped rows
are exactly what that release's ``users.role`` cap produced at read time.

Revision ID: c9e1a3b5d7f9
Revises: b8d0f2a4c6e8
Create Date: 2026-09-27 18:00:00.000000

"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "c9e1a3b5d7f9"
down_revision: str | None = "b8d0f2a4c6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies, not imports: this revision must keep meaning what it meant when
# it ran, whatever the application's constants become.
DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000d0f1")
DEFAULT_ORG_SLUG = "default"
DEFAULT_ORG_NAME = "Default organization"
SELF_HOSTED = "self_hosted"
HOSTED = "hosted"

_ORG_ROLE_VALUES = ("owner", "admin", "member")
_USER_ROLE_VALUES = ("owner", "editor", "viewer")
_PROJECT_ROLE_VALUES = ("editor", "viewer")
_OWNED_TABLES = ("projects", "data_sources", "api_keys", "invitations", "audit_log")

# Typed enums for the data statements: on PostgreSQL the binds are cast to the
# native type (comparing a native enum to a varchar is an error there); on SQLite
# they are plain strings. ``create_constraint=False`` — nothing here emits DDL.
_user_role = sa.Enum(*_USER_ROLE_VALUES, name="user_role", create_constraint=False)
_org_role = sa.Enum(*_ORG_ROLE_VALUES, name="organization_member_role", create_constraint=False)
_project_role = sa.Enum(*_PROJECT_ROLE_VALUES, name="project_member_role", create_constraint=False)

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
)
_project_members = sa.table(
    "project_members",
    sa.column("user_id", sa.Uuid()),
    sa.column("role", _project_role),
)


def _org_role_for(user_role: str) -> str:
    return "owner" if user_role == "owner" else "member"


def backfill_organizations(
    connection: Connection,
    *,
    deployment_mode: str,
    platform_admin_emails: Sequence[str],
) -> dict[str, int]:
    """The PR1 backfill again, for everything created since it ran. Idempotent.

    Only ADDS: a membership that already exists (whatever its role), a
    platform-admin flag that is already set, an organization already assigned,
    an ``org_role`` already filled, are all left as they are.
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

    for table_name in _OWNED_TABLES:
        table = sa.table(table_name, sa.column("organization_id", sa.Uuid()))
        connection.execute(
            sa.update(table)
            .where(table.c.organization_id.is_(None))
            .values(organization_id=DEFAULT_ORG_ID)
        )
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

    filled = 0
    for user_role in _USER_ROLE_VALUES:
        result = connection.execute(
            sa.update(_invitations)
            .where(_invitations.c.org_role.is_(None), _invitations.c.role == user_role)
            .values(org_role=_org_role_for(user_role))
        )
        filled += result.rowcount
    counts["invitation_roles_filled"] = filled
    return counts


def cap_viewer_project_roles(connection: Connection) -> int:
    """Turn every ``editor`` row of an instance ``viewer`` into ``viewer``.

    Returns the number of rows changed. Idempotent: a second run finds none.
    """
    viewers = sa.select(_users.c.id).where(_users.c.role == "viewer")
    result = connection.execute(
        sa.update(_project_members)
        .where(_project_members.c.role == "editor", _project_members.c.user_id.in_(viewers))
        .values(role="viewer")
    )
    return result.rowcount


def upgrade() -> None:
    from tripl.config import settings

    connection = op.get_bind()
    counts = backfill_organizations(
        connection,
        deployment_mode=settings.deployment_mode,
        platform_admin_emails=settings.platform_admin_emails,
    )
    counts["viewer_rows_capped"] = cap_viewer_project_roles(connection)
    logging.getLogger("alembic.runtime.migration").info("organization roles backfill: %s", counts)


def downgrade() -> None:
    """Nothing to undo; see the module docstring."""
