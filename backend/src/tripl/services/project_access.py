"""Per-project membership: who may SEE a project, and who may EDIT inside it.

The one place that turns a user plus a project into a project role. Every other
surface asks this module rather than reading ``project_members`` or
``organization_members`` itself, so the rules below cannot drift between the
route gate, the ``can_mutate`` flag, the project list and the data-source
catalog (F20 PR4: organization roles are the source of truth):

* an ``owner`` or ``admin`` of the project's OWN organization
  (``organization_members``) is ``"owner"`` in every project of that
  organization, with no membership row. The organization is always joined from
  the project row, never taken from the caller, so an admin of one organization
  holds nothing in another (critique #4);
* anyone else holds exactly the role of their ``project_members`` row
  (``editor`` or ``viewer``). The row is authoritative: the old instance-role
  cap is gone, and migration ``c9e1a3b5d7f9`` capped the rows of the former
  instance viewers once;
* no row means ``None``: the project does not exist for that user. Callers turn
  that into the same 404 an unknown slug gets, never a 403, so a non-member
  cannot even learn that the slug is taken. A project-bound API key hitting
  another project's slug gets that same 404 (``api.deps._enforce_project_scope``);
* a platform admin (``users.is_platform_admin``) gets nothing from that flag
  here: it is an operator role, not an organization or project one;
* inside a request the bound organization fences every answer: a project of
  another organization is ``None`` for everyone, so an id taken from a resource
  (a photo, a comment, a reviewer) cannot reach across organizations.
  ``users.role`` is never read.

The one known exception: creating or re-slugging a project onto a slug that is
already taken in the same organization answers 409, member or not. Slugs are
unique per organization (F20 PR5) and the create/rename path has to refuse the
collision, so that response does reveal that the slug exists in that
organization — never in another one, where the same slug is free. It only
leaks the slug, never the project's contents.

Editing inside a project takes an :data:`EDITING_ROLES` role. This module
imports models only, never another service — apart from
:mod:`tripl.services.project_lookup`, itself models-only, for the org-scoped slug
clause — so any service can import it at module level.
"""

import uuid
from collections.abc import Collection, Iterable
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import QueryableAttribute, aliased
from sqlalchemy.sql.elements import ColumnElement

from tripl.middleware.org_context import current_org_id, require_org_id
from tripl.models.domain_enums import OrganizationRole
from tripl.models.organization import OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.services.project_lookup import PROJECT_NOT_FOUND, project_slug_clause

ProjectRole = Literal["owner", "editor", "viewer"]

OWNER: ProjectRole = "owner"
EDITOR: ProjectRole = "editor"
VIEWER: ProjectRole = "viewer"

# The project roles that may write inside a project.
EDITING_ROLES: frozenset[str] = frozenset({OWNER, EDITOR})

# The organization roles that own every project of their organization and
# administer the organization itself. ``owner`` differs from ``admin`` only on
# owner management (``user_service.update_org_role``) and, later, SSO and
# deletion.
ORG_ADMIN_ROLES: frozenset[str] = frozenset(
    {OrganizationRole.owner.value, OrganizationRole.admin.value}
)


def is_org_admin_role(role: str | None) -> bool:
    """Whether an organization role (``None`` for a non-member) is owner or admin."""
    return role is not None and str(role) in ORG_ADMIN_ROLES


def can_edit(role: str | None) -> bool:
    """Whether a project role (as :func:`member_role` answers it) may write."""
    return role in EDITING_ROLES


async def org_role_of(
    session: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID
) -> OrganizationRole | None:
    """``user_id``'s role in organization ``org_id``; ``None`` for a non-member."""
    role: str | None = await session.scalar(
        select(OrganizationMember.role).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    return None if role is None else OrganizationRole(str(role))


async def is_org_admin(session: AsyncSession, user: User, org_id: uuid.UUID | None = None) -> bool:
    """Whether ``user`` is owner or admin of ``org_id`` (default: the bound org)."""
    target = org_id if org_id is not None else require_org_id()
    return is_org_admin_role(await org_role_of(session, user.id, target))


async def is_org_owner(session: AsyncSession, user: User, org_id: uuid.UUID | None = None) -> bool:
    """Whether ``user`` is an owner of ``org_id`` (default: the bound org)."""
    target = org_id if org_id is not None else require_org_id()
    return await org_role_of(session, user.id, target) == OrganizationRole.owner


def effective_role(org_role: str | None, membership_role: object | None) -> ProjectRole | None:
    """The project role from the caller's org role and membership row.

    Pure. ``org_role`` MUST be the caller's role in the project's OWN
    organization (the callers here join it from the project row); ``None`` for
    a non-member. ``membership_role`` is the raw ``project_members.role`` (a
    ``ProjectMemberRole`` or its string value), ``None`` for no row.
    """
    if is_org_admin_role(org_role):
        return OWNER
    if membership_role is None:
        return None
    return VIEWER if str(membership_role) == VIEWER else EDITOR


def _role_rows(user_id: uuid.UUID) -> Select[Any]:
    """``(project id, org role, membership role)`` per project, for one user.

    The org role is joined from the PROJECT's organization, never from the
    request, so an admin of another organization contributes nothing.
    """
    return (
        select(Project.id, OrganizationMember.role, ProjectMember.role)
        .select_from(Project)
        .outerjoin(
            OrganizationMember,
            and_(
                OrganizationMember.organization_id == Project.organization_id,
                OrganizationMember.user_id == user_id,
            ),
        )
        .outerjoin(
            ProjectMember,
            and_(ProjectMember.project_id == Project.id, ProjectMember.user_id == user_id),
        )
    )


def _in_bound_org(
    statement: Select[Any],
) -> Select[Any]:
    """Fence ``statement`` to the request's organization when one is bound.

    A worker or script with no organization bound sees every organization's
    projects, each answered with the caller's role in that project's own org.
    """
    org_id = current_org_id()
    return statement if org_id is None else statement.where(Project.organization_id == org_id)


async def _member_role(
    session: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID, *, fenced: bool
) -> ProjectRole | None:
    statement = _role_rows(user_id).where(Project.id == project_id)
    if fenced:
        statement = _in_bound_org(statement)
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    return effective_role(row[1], row[2])


async def member_role(
    session: AsyncSession, user: User, project_id: uuid.UUID
) -> ProjectRole | None:
    """``user``'s role in the project with ``project_id``; ``None`` for a non-member.

    Always reads the project row: a missing project, or one outside the bound
    organization, is ``None`` whatever the caller's organization role
    (critique #4 — callers pass ids taken from resources).
    """
    return await _member_role(session, user.id, project_id, fenced=True)


async def member_roles(
    session: AsyncSession, user: User, project_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, ProjectRole]:
    """:func:`member_role` for many projects in one query.

    Projects the user holds no role in (or that do not exist, or live outside
    the bound organization) are absent from the result.
    """
    ids = set(project_ids)
    if not ids:
        return {}
    rows = await session.execute(_in_bound_org(_role_rows(user.id).where(Project.id.in_(ids))))
    roles: dict[uuid.UUID, ProjectRole] = {}
    for project_id, org_role, row_role in rows.all():
        role = effective_role(org_role, row_role)
        if role is not None:
            roles[project_id] = role
    return roles


async def is_project_org_admin(session: AsyncSession, user: User, project_id: uuid.UUID) -> bool:
    """Whether ``user`` is owner/admin of the organization owning ``project_id``.

    That is exactly project role ``"owner"``: comment moderation, the member
    manager check and data-source redaction ask this.
    """
    return await member_role(session, user, project_id) == OWNER


#: A column of the enclosing query (for fan-out joins) or a plain value.
_UuidOperand = ColumnElement[uuid.UUID] | QueryableAttribute[uuid.UUID] | uuid.UUID


def project_member_clause(
    user_id: _UuidOperand,
    project_id: _UuidOperand,
) -> ColumnElement[bool]:
    """SQL: ``user_id`` holds a role in ``project_id``.

    A ``project_members`` row, OR an owner/admin membership of the organization
    that owns the project. Built on aliases so the subqueries never correlate
    with a ``project_members``/``projects`` table of the enclosing query; the
    arguments may be columns of that query (for fan-out joins) or plain values.
    """
    member = aliased(ProjectMember)
    org_member = aliased(OrganizationMember)
    project = aliased(Project)
    return or_(
        exists().where(member.project_id == project_id, member.user_id == user_id),
        exists().where(
            project.id == project_id,
            org_member.organization_id == project.organization_id,
            org_member.user_id == user_id,
            org_member.role.in_(sorted(ORG_ADMIN_ROLES)),
        ),
    )


def members_among_stmt(
    project_id: uuid.UUID, user_ids: Collection[uuid.UUID]
) -> Select[tuple[uuid.UUID]]:
    """The ids among ``user_ids`` with a role in ``project_id`` (existing users only).

    Shared by :func:`members_among` and ``notification_service``'s sync twin so
    the two cannot drift.
    """
    return select(User.id).where(
        User.id.in_(set(user_ids)), project_member_clause(User.id, project_id)
    )


async def members_among(
    session: AsyncSession, project_id: uuid.UUID, user_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of ``user_ids`` that has a role in ``project_id``.

    :func:`member_role` for many users in one query: owners and admins of the
    project's organization (no row needed) plus anyone holding a membership row.
    A deleted user drops out. Used to ignore per-project grants (event type
    ownership, reviewer assignments) that outlived their holder's membership.
    """
    ids = set(user_ids)
    if not ids:
        return set()
    rows = await session.scalars(members_among_stmt(project_id, ids))
    return set(rows.all())


async def member_project_ids(
    session: AsyncSession, user: User, org_id: uuid.UUID | None = None
) -> set[uuid.UUID]:
    """Ids of every project ``user`` may see in organization ``org_id``.

    ``org_id`` defaults to the bound organization; with none bound (a worker)
    it spans every organization, each project answered by the caller's role in
    its own org. Never ``None`` any more: an org owner/admin gets the ids of
    every project of that organization, not "everything on the instance".
    """
    target = org_id if org_id is not None else current_org_id()
    statement = select(Project.id).where(project_member_clause(user.id, Project.id))
    if target is not None:
        statement = statement.where(Project.organization_id == target)
    rows = await session.scalars(statement)
    return set(rows.all())


async def member_role_by_slug(session: AsyncSession, user: User, slug: str) -> ProjectRole | None:
    """:func:`member_role` for a slug; ``None`` for a non-member or an unknown slug.

    The slug is resolved in the bound organization.
    """
    row = (await session.execute(_role_rows(user.id).where(project_slug_clause(slug)))).first()
    if row is None:
        return None
    return effective_role(row[1], row[2])


async def require_project_access(
    request: Request, session: AsyncSession, user: User
) -> ProjectRole | None:
    """Admit only members to a ``/projects/{slug}/...`` route; 404 everyone else.

    Reads the path's ``slug``; a route without one is not project-scoped and
    passes with ``None``. The resolved role is stashed on
    ``request.state.project_role`` so the write gate (and anything else later in
    the request) reads it instead of querying again.

    A missing project and a project the caller holds no role in are the same
    404 with the same detail, for everyone (organization owners included), so
    the response is no oracle for which slugs exist.
    """
    slug = request.path_params.get("slug")
    if not slug:
        return None
    role = await member_role_by_slug(session, user, slug)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PROJECT_NOT_FOUND)
    request.state.project_role = role
    return role


async def still_member(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> bool:
    """Whether ``user_id`` still has a role in ``project_id``, read afresh.

    Opens (and closes) its own short-lived session, so the stream holds no
    pooled connection between checks. The user row is re-read too: a deleted
    user, or an org admin demoted to a member without a row, loses the stream.

    A project that no longer exists ends the stream for everyone. A demo reset
    re-creates the project under the same slug with a new id, so a stream left
    on the old id's channel would never see another event; ending it lets the
    client reconnect and resolve the new id. Not fenced by the bound
    organization: the stream's project was resolved in it when it opened, and
    the role is always read against the project's own organization.
    """
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return False
        return await _member_role(session, user.id, project_id, fenced=False) is not None
