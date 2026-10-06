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
* anyone else with a ``project_members`` row holds exactly the role it says
  (``editor`` or ``viewer``), and a ``none`` row is no access at all. The row is
  authoritative, below the organization default as well as above it: a member
  opted out with ``none``, or held to ``viewer`` under an ``editor`` default,
  stays there. Migration ``c9e1a3b5d7f9`` capped the rows of the former
  instance viewers once;
* an organization ``member`` with no row gets the organization's
  ``default_project_role`` (``none``, ``viewer`` or ``editor``; F20). ``none``
  — the default — keeps projects invisible to members without a row. Someone
  who is not a member of the project's organization gets nothing from it;
* no role means ``None``: the project does not exist for that user. Callers turn
  that into the same 404 an unknown slug gets, never a 403, so a non-member
  cannot even learn that the slug is taken. A project-bound API key hitting
  another project's slug gets that same 404 (``api.deps._enforce_project_scope``);
* a platform admin (``users.is_platform_admin``) gets nothing from that flag
  here: it is an operator role, not an organization or project one. The one
  exception is a live read-only step-in (F20 PR14), which
  ``services.org_resolution`` records on the bound organization: there, and
  only for that user in that organization, a non-member answers organization
  role ``member`` and project role ``viewer`` on every project
  (:func:`_step_in_role`). Writes are refused before any of this is asked
  (``api.deps``);
* inside a request the bound organization fences every answer: a project of
  another organization is ``None`` for everyone, so an id taken from a resource
  (a photo, a comment, a reviewer) cannot reach across organizations.
  There is no instance-wide role to consult;
* an installed extension may grant project roles on top
  (``Extension.project_grants``, e.g. to an organization group): ``editor`` or
  ``viewer``, never ``owner``, and only to a member of the project's own
  organization through a grant row of that same organization. The higher of
  the grant and the role above wins; a grant never touches an organization
  role (:func:`org_role_of` does not read grants). With no extension there are
  no grants and every answer is exactly the four arms above.

Two helpers carry the rule, and every surface goes through one of them:
:func:`effective_role` (Python, over the rows :func:`_role_rows` reads) and
:func:`project_member_clause` (SQL, for the lists and fan-outs). They state the
same four arms in the same order; change them together.

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
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from sqlalchemy import Select, and_, case, exists, false, func, null, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import QueryableAttribute, aliased
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from tripl import extensions
from tripl.middleware.org_context import current_org_id, require_org_id, stepped_in
from tripl.models.domain_enums import OrganizationRole, OrganizationStatus, ProjectMemberRole
from tripl.models.organization import Organization, OrganizationMember
from tripl.models.platform_step_in import PlatformStepIn
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
    if role is None:
        # The step-in arm: a platform admin reading the bound organization.
        return OrganizationRole.member if stepped_in(user_id, org_id) else None
    return OrganizationRole(str(role))


async def is_org_admin(session: AsyncSession, user: User, org_id: uuid.UUID | None = None) -> bool:
    """Whether ``user`` is owner or admin of ``org_id`` (default: the bound org)."""
    target = org_id if org_id is not None else require_org_id()
    return is_org_admin_role(await org_role_of(session, user.id, target))


async def is_org_owner(session: AsyncSession, user: User, org_id: uuid.UUID | None = None) -> bool:
    """Whether ``user`` is an owner of ``org_id`` (default: the bound org)."""
    target = org_id if org_id is not None else require_org_id()
    return await org_role_of(session, user.id, target) == OrganizationRole.owner


#: ``project_members.role`` / ``organizations.default_project_role`` for "no access".
NO_ACCESS = ProjectMemberRole.none.value


def _granted(role: object | None) -> ProjectRole | None:
    """A ``project_member_role`` value as a project role; ``none``/``None`` is ``None``."""
    if role is None:
        return None
    value = str(role)
    if value == VIEWER:
        return VIEWER
    if value == EDITOR:
        return EDITOR
    return None


_RANK: dict[ProjectRole | None, int] = {None: 0, VIEWER: 1, EDITOR: 2, OWNER: 3}


def _higher(a: ProjectRole | None, b: ProjectRole | None) -> ProjectRole | None:
    return a if _RANK[a] >= _RANK[b] else b


def effective_role(
    org_role: str | None,
    membership_role: object | None,
    default_role: object | None,
    granted: object | None = None,
) -> ProjectRole | None:
    """The project role from the caller's org role, membership row and org default.

    Pure. ``org_role`` MUST be the caller's role in the project's OWN
    organization (the callers here join it from the project row); ``None`` for
    a non-member. ``membership_role`` is the raw ``project_members.role`` (a
    ``ProjectMemberRole`` or its string value), ``None`` for no row.
    ``default_role`` is that organization's ``default_project_role``.

    In order: an org owner/admin is ``owner``; a row decides (``none`` is no
    access, even under a wider default); an org member without a row gets the
    default; anyone else gets nothing. :func:`project_member_clause` is the SQL
    twin.

    ``granted`` is an extension's grant (``"editor"``/``"viewer"``, anything
    else counts for nothing): it lifts the role above to itself, and only for a
    member of the organization (``org_role`` set), never to ``owner``.
    """
    if is_org_admin_role(org_role):
        return OWNER
    if membership_role is not None:
        base = _granted(membership_role)
    elif org_role is None:
        base = None
    else:
        base = _granted(default_role)
    if org_role is None:
        return base
    return _higher(base, _granted(granted))


def _step_in_role(
    user_id: uuid.UUID, org_id: uuid.UUID, role: ProjectRole | None
) -> ProjectRole | None:
    """The step-in arm of every project answer: ``viewer`` where there was none.

    Only when the bound organization is a live step-in of ``user_id``'s and the
    project belongs to it (F20 PR14). A real role always wins.
    """
    if role is None and stepped_in(user_id, org_id):
        return VIEWER
    return role


#: A column of the enclosing query (for fan-out joins) or a plain value.
_UuidOperand = ColumnElement[uuid.UUID] | QueryableAttribute[uuid.UUID] | uuid.UUID


#: The roles an extension grant may carry. Never ``owner``.
GRANTABLE_ROLES: tuple[ProjectRole, ...] = (EDITOR, VIEWER)
_GRANT_COLUMNS = 4


def _extension_grants() -> Subquery | None:
    """Every extension's grant rows as one subquery, ``None`` when there are none.

    Columns, by position: organization id, project id, user id, role
    (``Extension.project_grants``). A SELECT of another shape is a bug in the
    extension and raises, so the request fails rather than answering without it.
    """
    selects = extensions.project_grants()
    if not selects:
        return None
    for statement in selects:
        if len(statement.selected_columns) != _GRANT_COLUMNS:
            raise TypeError("Extension.project_grants must select exactly four columns")
    combined = selects[0] if len(selects) == 1 else union_all(*selects)
    return combined.subquery()


def _grant_match(
    grants: Subquery,
    user_id: _UuidOperand,
    project_id: _UuidOperand,
    project_org_id: _UuidOperand,
) -> list[ColumnElement[bool]]:
    """A grant row for ``user_id`` in ``project_id`` that counts.

    Only a row of the project's OWN organization, with a grantable role, for a
    current member of that organization: a grant cannot reach across
    organizations, outlive an organization membership, or make an owner.
    """
    org_col, project_col, user_col, role_col = list(grants.c)[:_GRANT_COLUMNS]
    # The membership is a join at the same level, never a nested EXISTS: a
    # subquery two levels down does not correlate with the enclosing query's
    # columns (``users.id`` in a fan-out) and would match any user.
    member = aliased(OrganizationMember)
    return [
        project_col == project_id,
        user_col == user_id,
        org_col == project_org_id,
        role_col.in_(GRANTABLE_ROLES),
        member.organization_id == project_org_id,
        member.user_id == user_id,
    ]


def _grant_column(user_id: uuid.UUID) -> ColumnElement[Any]:
    """The best extension grant of ``user_id`` in the enclosing query's ``Project``.

    ``1`` for ``editor``, ``2`` for ``viewer``, NULL for none
    (:func:`_grant_role` reads it back). NULL whenever no extension grants.
    """
    grants = _extension_grants()
    if grants is None:
        return null()
    role_col = list(grants.c)[3]
    return (
        select(func.min(case((role_col == EDITOR, 1), else_=2)))
        .where(*_grant_match(grants, user_id, Project.id, Project.organization_id))
        .scalar_subquery()
    )


def _grant_role(value: object | None) -> ProjectRole | None:
    return {1: EDITOR, 2: VIEWER}.get(value) if isinstance(value, int) else None


def _role_rows(user_id: uuid.UUID, *, with_grants: bool = True) -> Select[Any]:
    """``(project id, org role, membership role, project org id, org default, grant)``.

    One row per project, for one user. The org role and the default are joined
    from the PROJECT's organization, never from the request, so an admin of
    another organization contributes nothing. Feed ``row[1]``, ``row[2]``,
    ``row[4]`` and ``_grant_role(row[5])`` to :func:`effective_role`
    (:func:`_row_role`). ``with_grants=False`` leaves the extension grants out
    (``grant`` is NULL).
    """
    return (
        select(
            Project.id,
            OrganizationMember.role,
            ProjectMember.role,
            Project.organization_id,
            Organization.default_project_role,
            _grant_column(user_id) if with_grants else null(),
        )
        .select_from(Project)
        .join(Organization, Organization.id == Project.organization_id)
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


def _row_role(row: Any) -> ProjectRole | None:
    """:func:`effective_role` over one :func:`_role_rows` row (no step-in arm)."""
    return effective_role(row[1], row[2], row[4], _grant_role(row[5]))


async def _member_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    fenced: bool,
    with_grants: bool = True,
) -> ProjectRole | None:
    statement = _role_rows(user_id, with_grants=with_grants).where(Project.id == project_id)
    if fenced:
        statement = _in_bound_org(statement)
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    return _step_in_role(user_id, row[3], _row_role(row))


async def member_role(
    session: AsyncSession, user: User, project_id: uuid.UUID
) -> ProjectRole | None:
    """``user``'s role in the project with ``project_id``; ``None`` for a non-member.

    Always reads the project row: a missing project, or one outside the bound
    organization, is ``None`` whatever the caller's organization role
    (critique #4 — callers pass ids taken from resources).
    """
    return await _member_role(session, user.id, project_id, fenced=True)


async def direct_member_role(
    session: AsyncSession, user: User, project_id: uuid.UUID
) -> ProjectRole | None:
    """:func:`member_role` without the extensions' grants.

    The role the organization role, the membership row and the organization
    default give on their own (and the step-in arm), for an extension that
    needs to tell its own grants apart from the core's roles.
    """
    return await _member_role(session, user.id, project_id, fenced=True, with_grants=False)


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
    for row in rows.all():
        role = _step_in_role(user.id, row[3], _row_role(row))
        if role is not None:
            roles[row[0]] = role
    return roles


async def is_project_org_admin(session: AsyncSession, user: User, project_id: uuid.UUID) -> bool:
    """Whether ``user`` is owner/admin of the organization owning ``project_id``.

    That is exactly project role ``"owner"``: comment moderation, the member
    manager check and data-source redaction ask this.
    """
    return await member_role(session, user, project_id) == OWNER


def project_member_clause(
    user_id: _UuidOperand,
    project_id: _UuidOperand,
) -> ColumnElement[bool]:
    """SQL: ``user_id`` holds a role in ``project_id``; :func:`effective_role`'s twin.

    An owner/admin membership of the organization that owns the project; OR a
    ``project_members`` row other than ``none``; OR no row at all, a membership
    of that organization, and an organization ``default_project_role`` other
    than ``none``; OR an extension grant that counts (:func:`_grant_match`).
    Built on aliases so the subqueries never correlate with a
    ``project_members``/``projects``/``organizations`` table of the enclosing
    query; the arguments may be columns of that query (for fan-out joins) or
    plain values.
    """
    grants = _extension_grants()
    granted: ColumnElement[bool] = false()
    if grants is not None:
        grant_project = aliased(Project)
        granted = exists().where(
            grant_project.id == project_id,
            *_grant_match(grants, user_id, grant_project.id, grant_project.organization_id),
        )
    row = aliased(ProjectMember)
    any_row = aliased(ProjectMember)
    admin = aliased(OrganizationMember)
    org_member = aliased(OrganizationMember)
    admin_project = aliased(Project)
    project = aliased(Project)
    org = aliased(Organization)
    return or_(
        exists().where(
            admin_project.id == project_id,
            admin.organization_id == admin_project.organization_id,
            admin.user_id == user_id,
            admin.role.in_(sorted(ORG_ADMIN_ROLES)),
        ),
        exists().where(
            row.project_id == project_id,
            row.user_id == user_id,
            row.role != NO_ACCESS,
        ),
        and_(
            ~exists().where(any_row.project_id == project_id, any_row.user_id == user_id),
            exists().where(
                project.id == project_id,
                org.id == project.organization_id,
                org.default_project_role != NO_ACCESS,
                org_member.organization_id == project.organization_id,
                org_member.user_id == user_id,
            ),
        ),
        granted,
    )


def members_among_stmt(project_id: uuid.UUID, user_ids: Collection[uuid.UUID]) -> Select[uuid.UUID]:
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
    project's organization (no row needed), anyone holding a membership row
    other than ``none``, and the organization's members without a row when its
    ``default_project_role`` grants access.
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
    if target is not None and stepped_in(user.id, target):
        # A step-in reads every project of the organization it names.
        statement = select(Project.id).where(Project.organization_id == target)
        return set((await session.scalars(statement)).all())
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
    return _step_in_role(user.id, row[3], _row_role(row))


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
    user, an org admin demoted to a member without a row (under a ``none``
    default), a member opted out with a ``none`` row, or a member whose access
    came from a default that was since lowered to ``none``, loses the stream.

    A project that no longer exists ends the stream for everyone. A demo reset
    re-creates the project under the same slug with a new id, so a stream left
    on the old id's channel would never see another event; ending it lets the
    client reconnect and resolve the new id. Not fenced by the bound
    organization: the stream's project was resolved in it when it opened, and
    the role is always read against the project's own organization.

    A member's stream also ends once that organization is no longer
    ``active`` (suspended or being deleted); a platform admin's step-in stream
    survives a suspension (a step-in reads one) but not a deletion.
    """
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None:
            return False
        row = (await session.execute(_role_rows(user.id).where(Project.id == project_id))).first()
        if row is None:
            return False
        # A suspension (or deletion) of the project's organization ends its
        # members' streams: the next request would get the 403 (or the 404).
        org_status: str | None = await session.scalar(
            select(Organization.status).where(Organization.id == row[3])
        )
        if org_status is None:
            return False
        if _row_role(row) is not None:
            return str(org_status) == OrganizationStatus.active.value
        if not stepped_in(user.id, row[3]) or not user.is_platform_admin:
            return False
        # A step-in reads a suspended organization too, never a deleting one.
        if str(org_status) == OrganizationStatus.deleting.value:
            return False
        # A step-in stream ends with the step-in: re-read it, it may have been
        # ended or run out since the stream opened.
        live: uuid.UUID | None = await session.scalar(
            select(PlatformStepIn.id).where(
                PlatformStepIn.user_id == user.id,
                PlatformStepIn.organization_id == row[3],
                PlatformStepIn.ended_at.is_(None),
                PlatformStepIn.expires_at > datetime.now(UTC),
            )
        )
        return live is not None
