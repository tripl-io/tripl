"""The platform console: organizations, users and read-only step-ins (F20 PR14).

The HTTP layer is ``api/v1/platform_console.py`` (platform admins, browser
session only); this module holds the rules and raises plain exceptions:

* :class:`ConsoleOrgNotFoundError` / :class:`ConsoleUserNotFoundError` /
  :class:`StepInNotFoundError` — 404;
* :class:`ConsoleConflictError` — 409, with the message to show.

Everything the console reads is metadata and counts: organization names,
members' emails and roles, project slugs and names. Never a project's plan,
data, settings or credentials — reading those takes a step-in, which is
read-only, time-limited and audited in the target organization.

Audit rows: ``org.suspend`` / ``org.unsuspend`` and ``platform.step_in`` /
``platform.step_in_end`` are filed in the TARGET organization (its owners read
them in their own audit feed), with the acting platform admin as the user.
``platform.admin_grant`` / ``platform.admin_revoke`` have no organization
(platform scope). The ``tripl-admin`` console script writes the same two
through :func:`set_platform_admin_sync`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ScalarSelect, Select, func, null, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.elements import ColumnElement

from tripl.models.audit_log import AuditLog
from tripl.models.domain_enums import OrganizationRole, OrganizationStatus
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.platform_step_in import PlatformStepIn
from tripl.models.project import Project
from tripl.models.user import User
from tripl.schemas.organization import ORG_SLUG_PATTERN
from tripl.schemas.platform_console import (
    PlatformOrgDetail,
    PlatformOrgItem,
    PlatformOrgList,
    PlatformOrgMember,
    PlatformOrgProject,
    PlatformUserItem,
    PlatformUserList,
    StepInResponse,
)
from tripl.services import audit_service
from tripl.services.step_in_expiry import close_expired_step_ins

_ORG_SLUG = re.compile(ORG_SLUG_PATTERN)

ORG_NOT_FOUND = "Organization not found"
USER_NOT_FOUND = "User not found"
STEP_IN_NOT_FOUND = "Step-in not found"
DEFAULT_ORG_UNSUSPENDABLE = "The default organization cannot be suspended"
ORG_BEING_DELETED = "The organization is being deleted"
ORG_ALREADY_SUSPENDED = "The organization is already suspended"
ORG_NOT_SUSPENDED = "The organization is not suspended"
CANNOT_REVOKE_SELF = "You cannot revoke your own platform admin role"
LAST_PLATFORM_ADMIN = "Cannot revoke the last platform admin"
STEP_IN_AS_MEMBER = "You are a member of this organization; open it directly"
STEP_IN_ALREADY_ENDED = "The step-in has already ended"


class ConsoleOrgNotFoundError(LookupError):
    """No organization with that slug."""


class ConsoleUserNotFoundError(LookupError):
    """No user with that id or email."""


class StepInNotFoundError(LookupError):
    """No step-in with that id belongs to the caller."""


class ConsoleConflictError(Exception):
    """The request conflicts with the target's state (409); ``str()`` is the message."""


class LastPlatformAdminError(ConsoleConflictError):
    """Revoking would leave the instance without a platform admin."""


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_clause(q: str, *columns: InstrumentedAttribute[str | None]) -> ColumnElement[bool]:
    """Case-insensitive substring match on any of ``columns``; ``%`` and ``_`` are literal."""
    needle = f"%{_escape_like(q.strip().lower())}%"
    return or_(*(func.lower(column).like(needle, escape="\\") for column in columns))


# ── organizations ─────────────────────────────────────────────────────────────


def _member_count() -> ScalarSelect[int]:
    return (
        select(func.count(OrganizationMember.id))
        .where(OrganizationMember.organization_id == Organization.id)
        .correlate(Organization)
        .scalar_subquery()
    )


def _project_count() -> ScalarSelect[int]:
    return (
        select(func.count(Project.id))
        .where(Project.organization_id == Organization.id)
        .correlate(Organization)
        .scalar_subquery()
    )


async def _owner_emails(
    session: AsyncSession, org_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    if not org_ids:
        return {}
    rows = await session.execute(
        select(OrganizationMember.organization_id, User.email)
        .join(User, User.id == OrganizationMember.user_id)
        .where(
            OrganizationMember.organization_id.in_(org_ids),
            OrganizationMember.role == OrganizationRole.owner.value,
        )
        .order_by(User.email)
    )
    owners: dict[uuid.UUID, list[str]] = {}
    for org_id, email in rows.all():
        owners.setdefault(org_id, []).append(email)
    return owners


def _org_item(org: Organization, members: int, projects: int, owners: list[str]) -> PlatformOrgItem:
    return PlatformOrgItem(
        id=org.id,
        slug=org.slug,
        name=org.name,
        status=OrganizationStatus(str(org.status)),
        is_default=org.id == DEFAULT_ORG_ID,
        created_at=org.created_at,
        suspended_at=org.suspended_at,
        suspended_reason=org.suspended_reason,
        member_count=int(members or 0),
        project_count=int(projects or 0),
        owner_emails=owners,
    )


async def list_orgs(
    session: AsyncSession,
    *,
    q: str | None,
    status: OrganizationStatus | None,
    limit: int,
    offset: int,
) -> PlatformOrgList:
    """Every organization, deleting ones included, with counts; by name."""
    conditions: list[ColumnElement[bool]] = []
    if q and q.strip():
        conditions.append(_search_clause(q, Organization.slug, Organization.name))
    if status is not None:
        conditions.append(Organization.status == status.value)
    total = int(await session.scalar(select(func.count(Organization.id)).where(*conditions)) or 0)
    rows = (
        await session.execute(
            select(Organization, _member_count(), _project_count())
            .where(*conditions)
            .order_by(Organization.name, Organization.slug)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    owners = await _owner_emails(session, [org.id for org, _m, _p in rows])
    return PlatformOrgList(
        items=[
            _org_item(org, members, projects, owners.get(org.id, []))
            for org, members, projects in rows
        ],
        total=total,
    )


async def _org_by_slug(session: AsyncSession, slug: str) -> Organization:
    if not _ORG_SLUG.fullmatch(slug):
        raise ConsoleOrgNotFoundError(slug)
    org: Organization | None = await session.scalar(
        select(Organization).where(Organization.slug == slug)
    )
    if org is None:
        raise ConsoleOrgNotFoundError(slug)
    return org


async def get_org(session: AsyncSession, slug: str) -> PlatformOrgDetail:
    """One organization's metadata, members and project list."""
    org = await _org_by_slug(session, slug)
    return await _org_detail(session, org)


async def _org_detail(session: AsyncSession, org: Organization) -> PlatformOrgDetail:
    members = (
        await session.execute(
            select(User.id, User.email, User.name, OrganizationMember.role)
            .join(User, User.id == OrganizationMember.user_id)
            .where(OrganizationMember.organization_id == org.id)
            .order_by(User.email)
        )
    ).all()
    projects = (
        await session.execute(
            select(Project.slug, Project.name, Project.created_at)
            .where(Project.organization_id == org.id)
            .order_by(Project.name, Project.slug)
        )
    ).all()
    owners = [
        email for _id, email, _name, role in members if str(role) == OrganizationRole.owner.value
    ]
    item = _org_item(org, len(members), len(projects), owners)
    return PlatformOrgDetail(
        **item.model_dump(),
        members=[
            PlatformOrgMember(
                user_id=user_id, email=email, name=name, role=OrganizationRole(str(role))
            )
            for user_id, email, name, role in members
        ],
        projects=[
            PlatformOrgProject(slug=project_slug, name=name, created_at=created_at)
            for project_slug, name, created_at in projects
        ],
    )


async def suspend_org(
    session: AsyncSession, slug: str, *, reason: str, actor: User
) -> PlatformOrgDetail:
    """Suspend an ``active`` organization and audit it in that organization. Commits.

    A compare-and-set on ``status = active``, so a suspension cannot land on an
    organization an owner has just asked to delete.
    """
    org = await _org_by_slug(session, slug)
    if org.id == DEFAULT_ORG_ID:
        raise ConsoleConflictError(DEFAULT_ORG_UNSUSPENDABLE)
    now = datetime.now(UTC)
    result = await session.execute(
        update(Organization)
        .where(Organization.id == org.id, Organization.status == OrganizationStatus.active.value)
        .values(
            status=OrganizationStatus.suspended.value,
            suspended_at=now,
            suspended_reason=reason,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        await session.rollback()
        await session.refresh(org)
        if str(org.status) == OrganizationStatus.deleting.value:
            raise ConsoleConflictError(ORG_BEING_DELETED)
        raise ConsoleConflictError(ORG_ALREADY_SUSPENDED)
    await audit_service.record(
        session,
        user=actor,
        action="org.suspend",
        target_type="organization",
        target_id=org.id,
        target_name=org.slug,
        payload={"slug": org.slug, "reason": reason},
        organization_id=org.id,
    )
    await session.refresh(org)
    return await _org_detail(session, org)


async def unsuspend_org(session: AsyncSession, slug: str, *, actor: User) -> PlatformOrgDetail:
    """Put a ``suspended`` organization back to ``active``; audited there. Commits."""
    org = await _org_by_slug(session, slug)
    previous_reason = org.suspended_reason
    result = await session.execute(
        update(Organization)
        .where(
            Organization.id == org.id,
            Organization.status == OrganizationStatus.suspended.value,
        )
        .values(
            status=OrganizationStatus.active.value,
            suspended_at=None,
            suspended_reason=None,
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        await session.rollback()
        await session.refresh(org)
        if str(org.status) == OrganizationStatus.deleting.value:
            raise ConsoleConflictError(ORG_BEING_DELETED)
        raise ConsoleConflictError(ORG_NOT_SUSPENDED)
    await audit_service.record(
        session,
        user=actor,
        action="org.unsuspend",
        target_type="organization",
        target_id=org.id,
        target_name=org.slug,
        payload={"slug": org.slug, "suspended_reason": previous_reason},
        organization_id=org.id,
    )
    await session.refresh(org)
    return await _org_detail(session, org)


# ── users and the platform-admin flag ────────────────────────────────────────


def _org_count() -> ScalarSelect[int]:
    return (
        select(func.count(OrganizationMember.id))
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(
            OrganizationMember.user_id == User.id,
            Organization.status != OrganizationStatus.deleting.value,
        )
        .correlate(User)
        .scalar_subquery()
    )


def _user_item(user: User, org_count: int) -> PlatformUserItem:
    return PlatformUserItem(
        id=user.id,
        email=user.email,
        name=user.name,
        is_platform_admin=bool(user.is_platform_admin),
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
        org_count=int(org_count or 0),
    )


async def list_users(
    session: AsyncSession, *, q: str | None, limit: int, offset: int
) -> PlatformUserList:
    """Every account, by email, with how many organizations it belongs to."""
    conditions: list[ColumnElement[bool]] = []
    if q and q.strip():
        conditions.append(_search_clause(q, User.email, User.name))
    total = int(await session.scalar(select(func.count(User.id)).where(*conditions)) or 0)
    rows = (
        await session.execute(
            select(User, _org_count())
            .where(*conditions)
            .order_by(User.email)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return PlatformUserList(items=[_user_item(user, count) for user, count in rows], total=total)


async def get_user_item(session: AsyncSession, user_id: uuid.UUID) -> PlatformUserItem:
    row = (await session.execute(select(User, _org_count()).where(User.id == user_id))).first()
    if row is None:
        raise ConsoleUserNotFoundError(user_id)
    user, count = row
    return _user_item(user, count)


@dataclass(frozen=True)
class AdminChange:
    """What :func:`set_platform_admin` did: the user, and whether the flag moved."""

    user: User
    changed: bool


def _admin_ids_for_update() -> Select[tuple[uuid.UUID]]:
    # FOR UPDATE on every admin row serializes concurrent revokes: the second
    # waits, then re-reads the set without the admin the first one revoked, so
    # two admins revoking each other cannot leave the instance without one.
    return select(User.id).where(User.is_platform_admin.is_(True)).with_for_update()


def _admin_audit_row(
    target: User, *, grant: bool, actor: User | None, via: str, marked_verified: bool = False
) -> AuditLog:
    """The platform-scope audit row of a grant or revoke (``organization_id`` NULL)."""
    payload: dict[str, object] = {"email": target.email, "via": via}
    if marked_verified:
        payload["marked_verified"] = True
    entry = AuditLog(
        user_id=actor.id if actor else None,
        user_email=actor.email if actor else "",
        project_id=None,
        project_slug="",
        branch_id=None,
        branch_name="",
        action="platform.admin_grant" if grant else "platform.admin_revoke",
        target_type="user",
        target_id=target.id,
        target_name=target.email[:255],
        payload=payload,
    )
    entry.organization_id = null()
    return entry


async def set_platform_admin(
    session: AsyncSession, user_id: uuid.UUID, *, grant: bool, actor: User
) -> AdminChange:
    """Grant or revoke the platform-admin flag from the console. Commits.

    Revoking yourself is refused (another admin must do it), and so is revoking
    the last platform admin. A no-op (granting an admin, revoking a
    non-admin) changes and audits nothing.
    """
    target: User | None = await session.get(User, user_id)
    if target is None:
        raise ConsoleUserNotFoundError(user_id)
    if not grant and target.id == actor.id:
        raise ConsoleConflictError(CANNOT_REVOKE_SELF)
    if bool(target.is_platform_admin) == grant:
        return AdminChange(user=target, changed=False)
    if not grant:
        admins = set((await session.scalars(_admin_ids_for_update())).all())
        if target.id not in admins:
            await session.rollback()
            await session.refresh(target)
            return AdminChange(user=target, changed=False)
        if admins == {target.id}:
            await session.rollback()
            raise LastPlatformAdminError(LAST_PLATFORM_ADMIN)
    target.is_platform_admin = grant
    await audit_service.record(
        session,
        user=actor,
        action="platform.admin_grant" if grant else "platform.admin_revoke",
        target_type="user",
        target_id=target.id,
        target_name=target.email,
        payload={"email": target.email, "via": "console"},
        organization_id=None,
    )
    await session.refresh(target)
    return AdminChange(user=target, changed=True)


def set_platform_admin_sync(session: Session, email: str, *, grant: bool) -> AdminChange:
    """:func:`set_platform_admin` for the ``tripl-admin`` console script. Commits.

    Keyed by email; the operator at the shell is not an account, so the audit
    row has no user. The last platform admin cannot be revoked here either.

    A grant also marks the address verified when it is not yet: the operator
    controls the instance and vouches for the account (hosted sign-in may
    require a verified address). The audit row says so
    (``{"marked_verified": true}``). An account that is already a platform
    admin but unverified is only marked verified: no grant row, since nothing
    was granted.
    """
    target: User | None = session.scalar(select(User).where(User.email == email))
    if target is None:
        raise ConsoleUserNotFoundError(email)
    marked_verified = grant and target.email_verified_at is None
    if marked_verified:
        target.email_verified_at = datetime.now(UTC)
    if bool(target.is_platform_admin) == grant:
        if marked_verified:
            session.commit()
            session.refresh(target)
        return AdminChange(user=target, changed=False)
    if not grant:
        admins = set(session.scalars(_admin_ids_for_update()).all())
        if target.id not in admins:
            session.rollback()
            session.refresh(target)
            return AdminChange(user=target, changed=False)
        if admins == {target.id}:
            session.rollback()
            raise LastPlatformAdminError(LAST_PLATFORM_ADMIN)
    target.is_platform_admin = grant
    session.add(
        _admin_audit_row(
            target, grant=grant, actor=None, via="tripl-admin", marked_verified=marked_verified
        )
    )
    session.commit()
    session.refresh(target)
    return AdminChange(user=target, changed=True)


def list_platform_admins_sync(session: Session) -> list[User]:
    """Every platform admin, by email."""
    return list(
        session.scalars(
            select(User).where(User.is_platform_admin.is_(True)).order_by(User.email)
        ).all()
    )


# ── read-only step-ins ────────────────────────────────────────────────────────


def _is_live(step_in: PlatformStepIn, now: datetime) -> bool:
    return step_in.ended_at is None and step_in.expires_at > now


def _step_in_response(step_in: PlatformStepIn, org: Organization, now: datetime) -> StepInResponse:
    return StepInResponse(
        id=step_in.id,
        org_slug=org.slug,
        org_name=org.name,
        reason=step_in.reason,
        created_at=step_in.created_at,
        expires_at=step_in.expires_at,
        ended_at=step_in.ended_at,
        active=_is_live(step_in, now),
    )


async def start_step_in(
    session: AsyncSession, slug: str, *, reason: str, ttl_minutes: int, actor: User
) -> StepInResponse:
    """Open a read-only step-in to an organization. Commits.

    Refused for an organization being deleted and for one the admin is a member
    of (they already read it as themselves). A live step-in of theirs to the
    same organization is ended first, filed as ``platform.step_in_end``, so
    there is never more than one.
    """
    org = await _org_by_slug(session, slug)
    if str(org.status) == OrganizationStatus.deleting.value:
        raise ConsoleConflictError(ORG_BEING_DELETED)
    member: uuid.UUID | None = await session.scalar(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.user_id == actor.id,
        )
    )
    if member is not None:
        raise ConsoleConflictError(STEP_IN_AS_MEMBER)
    # A step-in to this organization that ran out unrecorded is closed as
    # expired first, so the history never shows it superseded or still open.
    await close_expired_step_ins(session, actor, org_id=org.id)
    now = datetime.now(UTC)
    live = (
        await session.scalars(
            select(PlatformStepIn).where(
                PlatformStepIn.user_id == actor.id,
                PlatformStepIn.organization_id == org.id,
                PlatformStepIn.ended_at.is_(None),
                PlatformStepIn.expires_at > now,
            )
        )
    ).all()
    for previous in live:
        previous.ended_at = now
        await audit_service.record(
            session,
            user=actor,
            action="platform.step_in_end",
            target_type="organization",
            target_id=org.id,
            target_name=org.slug,
            payload={"step_in_id": str(previous.id), "superseded": True},
            organization_id=org.id,
            commit=False,
        )
    step_in = PlatformStepIn(
        user_id=actor.id,
        organization_id=org.id,
        reason=reason,
        created_at=now,
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    session.add(step_in)
    await session.flush()
    await audit_service.record(
        session,
        user=actor,
        action="platform.step_in",
        target_type="organization",
        target_id=org.id,
        target_name=org.slug,
        payload={
            "step_in_id": str(step_in.id),
            "reason": reason,
            "ttl_minutes": ttl_minutes,
            "expires_at": step_in.expires_at.isoformat(),
        },
        organization_id=org.id,
    )
    return _step_in_response(step_in, org, now)


async def end_step_in(
    session: AsyncSession, step_in_id: uuid.UUID, *, actor: User
) -> StepInResponse:
    """End one of the caller's step-ins now; audited in its organization. Commits."""
    row = (
        await session.execute(
            select(PlatformStepIn, Organization)
            .join(Organization, Organization.id == PlatformStepIn.organization_id)
            .where(PlatformStepIn.id == step_in_id, PlatformStepIn.user_id == actor.id)
        )
    ).first()
    if row is None:
        raise StepInNotFoundError(step_in_id)
    step_in, org = row
    if step_in.ended_at is not None:
        raise ConsoleConflictError(STEP_IN_ALREADY_ENDED)
    now = datetime.now(UTC)
    expired = step_in.expires_at <= now
    step_in.ended_at = now
    await audit_service.record(
        session,
        user=actor,
        action="platform.step_in_end",
        target_type="organization",
        target_id=org.id,
        target_name=org.slug,
        payload={"step_in_id": str(step_in.id), "expired": expired},
        organization_id=org.id,
    )
    return _step_in_response(step_in, org, now)


async def list_step_ins(
    session: AsyncSession, *, actor: User, active: bool | None, limit: int = 100
) -> list[StepInResponse]:
    """The caller's step-ins, newest first; ``active`` narrows to live or to finished ones.

    An expired step-in nobody ended is closed (and ``platform.step_in_end``
    ``{"expired": true}`` filed) first, so the list shows its ``ended_at``.
    """
    await close_expired_step_ins(session, actor)
    now = datetime.now(UTC)
    live = (PlatformStepIn.ended_at.is_(None)) & (PlatformStepIn.expires_at > now)
    statement = (
        select(PlatformStepIn, Organization)
        .join(Organization, Organization.id == PlatformStepIn.organization_id)
        .where(PlatformStepIn.user_id == actor.id)
        .order_by(PlatformStepIn.created_at.desc(), PlatformStepIn.id)
        .limit(limit)
    )
    if active is True:
        statement = statement.where(live)
    elif active is False:
        statement = statement.where(~live)
    rows = (await session.execute(statement)).all()
    return [_step_in_response(step_in, org, now) for step_in, org in rows]
