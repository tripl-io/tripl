"""Per-project membership: who may SEE a project, and who may EDIT inside it.

The one place that turns a user plus a project into a project role. Every other
surface asks this module rather than reading ``project_members`` itself, so the
rules below cannot drift between the route gate, the ``can_mutate`` flag, the
project list and the data-source catalog:

* the instance owner (``User.role == "owner"``) is ``"owner"`` in every project,
  with no membership row;
* anyone else holds exactly the role of their ``project_members`` row, capped by
  their instance role: an instance ``viewer`` is never more than ``"viewer"``
  whatever the row says;
* no row means ``None``: the project does not exist for that user. Callers turn
  that into the same 404 an unknown slug gets, never a 403, so a non-member
  cannot even learn that the slug is taken. A project-bound API key hitting
  another project's slug gets that same 404 (``api.deps._enforce_project_scope``).

The one known exception: creating or re-slugging a project onto a slug that is
already taken answers 409, member or not. Slugs are unique instance-wide and
the create/rename path has to refuse the collision, so that response does
reveal that the slug exists. It is unavoidable without per-user slug
namespaces, and it only leaks the slug, never the project's contents.

Editing inside a project takes an :data:`EDITING_ROLES` role. This module
imports models only, never another service — apart from
:mod:`tripl.services.project_lookup`, itself models-only, for the org-scoped slug
clause — so any service can import it at module level.
"""

import uuid
from collections.abc import Iterable
from typing import Literal

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tripl.models.domain_enums import UserRole
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


def is_instance_owner(user: User) -> bool:
    return str(user.role) == UserRole.owner.value


def can_edit(role: str | None) -> bool:
    """Whether a project role (as :func:`member_role` answers it) may write."""
    return role in EDITING_ROLES


def effective_role(user: User, membership_role: object | None) -> ProjectRole | None:
    """The caller's project role from their membership row's role, if any.

    ``membership_role`` is the raw ``project_members.role`` (a
    ``ProjectMemberRole`` or its string value), ``None`` for no row.
    """
    if is_instance_owner(user):
        return OWNER
    if membership_role is None:
        return None
    if str(user.role) == UserRole.viewer.value or str(membership_role) == VIEWER:
        return VIEWER
    return EDITOR


async def member_role(
    session: AsyncSession, user: User, project_id: uuid.UUID
) -> ProjectRole | None:
    """``user``'s role in the project with ``project_id``; ``None`` for a non-member.

    Does not check that the project exists: for an instance owner it answers
    ``"owner"`` without a query.
    """
    if is_instance_owner(user):
        return OWNER
    row_role = await session.scalar(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
        )
    )
    return effective_role(user, row_role)


async def member_roles(
    session: AsyncSession, user: User, project_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, ProjectRole]:
    """:func:`member_role` for many projects in one query.

    Projects the user is not a member of are absent from the result. For an
    instance owner every requested id maps to ``"owner"``.
    """
    ids = set(project_ids)
    if not ids:
        return {}
    if is_instance_owner(user):
        return dict.fromkeys(ids, OWNER)
    rows = await session.execute(
        select(ProjectMember.project_id, ProjectMember.role).where(
            ProjectMember.user_id == user.id,
            ProjectMember.project_id.in_(ids),
        )
    )
    roles: dict[uuid.UUID, ProjectRole] = {}
    for project_id, row_role in rows.all():
        role = effective_role(user, row_role)
        if role is not None:
            roles[project_id] = role
    return roles


async def members_among(
    session: AsyncSession, project_id: uuid.UUID, user_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of ``user_ids`` that has a role in ``project_id``.

    :func:`member_role` for many users in one query: instance owners (no row
    needed) plus anyone holding a membership row. A deleted user drops out.
    Used to ignore per-project grants (event type ownership, reviewer
    assignments) that outlived their holder's membership.
    """
    ids = set(user_ids)
    if not ids:
        return set()
    owners = await session.scalars(
        select(User.id).where(User.id.in_(ids), User.role == UserRole.owner.value)
    )
    members = await session.scalars(
        select(ProjectMember.user_id)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id, ProjectMember.user_id.in_(ids))
    )
    return set(owners.all()) | set(members.all())


async def member_project_ids(session: AsyncSession, user: User) -> set[uuid.UUID] | None:
    """Ids of every project ``user`` may see; ``None`` means all of them (owner).

    Callers filter with ``visible is None or project_id in visible``.
    """
    if is_instance_owner(user):
        return None
    rows = await session.scalars(
        select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
    )
    return set(rows.all())


async def member_role_by_slug(session: AsyncSession, user: User, slug: str) -> ProjectRole | None:
    """:func:`member_role` for a slug; ``None`` for a non-member or an unknown slug.

    For an instance owner the slug is still resolved, so ``None`` then means the
    project does not exist.
    """
    if is_instance_owner(user):
        project_id: uuid.UUID | None = await session.scalar(
            select(Project.id).where(project_slug_clause(slug))
        )
        return OWNER if project_id is not None else None
    row_role = await session.scalar(
        select(ProjectMember.role)
        .join(Project, Project.id == ProjectMember.project_id)
        .where(project_slug_clause(slug), ProjectMember.user_id == user.id)
    )
    return effective_role(user, row_role)


async def require_project_access(
    request: Request, session: AsyncSession, user: User
) -> ProjectRole | None:
    """Admit only members to a ``/projects/{slug}/...`` route; 404 everyone else.

    Reads the path's ``slug``; a route without one is not project-scoped and
    passes with ``None``. The resolved role is stashed on
    ``request.state.project_role`` so the write gate (and anything else later in
    the request) reads it instead of querying again.

    An instance owner skips the membership query and passes even on an unknown
    slug: the route's own lookup answers that 404, exactly as before membership
    existed. For anyone else a missing project and a project they are not a
    member of are the same 404 with the same detail, so the response is no
    oracle for which slugs exist.
    """
    slug = request.path_params.get("slug")
    if not slug:
        return None
    role: ProjectRole | None
    if is_instance_owner(user):
        role = OWNER
    else:
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
    user, or an instance owner demoted to a non-member, loses the stream.

    A project that no longer exists ends the stream for everyone, the instance
    owner included (for whom :func:`member_role` answers without a query). A
    demo reset re-creates the project under the same slug with a new id, so a
    stream left on the old id's channel would never see another event; ending
    it lets the client reconnect and resolve the new id.
    """
    async with session_factory() as session:
        exists = await session.scalar(select(Project.id).where(Project.id == project_id))
        if exists is None:
            return False
        user = await session.get(User, user_id)
        if user is None:
            return False
        return await member_role(session, user, project_id) is not None
