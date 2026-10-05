"""Which organization an authenticated request acts in (F20 PR2, GH #273).

Called from the auth dependency (:mod:`tripl.api.deps`) once the caller is
known and before any project slug is resolved. The rules, in order:

1. An org named in the URL (``/api/v1/orgs/{org}/...``) must exist, else 404
   "Organization not found".
2. An API key belongs to one organization: that is the org. A URL naming a
   different one gets the same 404, so a key is no oracle for other orgs.
3. A cookie session with an org in the URL must be a member of it, else the
   same 404 — except the default organization on a single-team instance, which
   every user acts in without a membership row (rule 4), so its org-qualified
   URL answers exactly like the legacy one. Accounts created between the PR1
   migration and this release have no membership row at all.
4. No org in the URL: the tenancy policy decides (``tenancy.TenancyPolicy.orgless_org``).
   A single-team instance acts in the default organization (no query, exactly
   as before organizations existed). A multi-tenant one acts in the user's only
   organization and answers 400 "Organization required" (``ORG_REQUIRED``) when
   the user has none or several. There is never a fallback.

Only ``active`` organizations resolve (F20 PR6): one an owner has asked to
delete is ``deleting`` until the purge job removes it, and answers exactly like
an organization that does not exist — its URLs, its API keys and a hosted
member's legacy paths all 404 (or, for the last, stop counting it).

A ``suspended`` organization (F20 PR14) still exists for its members, so the
membership check runs first and a stranger still gets the 404; a member, or an
API key of it, then gets 403 "This organization is suspended" on every
org-scoped request — org-qualified and legacy paths alike.

The one step-in arm (F20 PR14): a platform admin's browser session with a live
step-in to the organization (``platform_step_ins``) and no membership of it is
admitted as if it were a member, with the step-in recorded on the bound
:class:`OrgRef` (``step_in_user_id``). ``services.project_access`` turns that
into organization role ``member`` and project role ``viewer``; ``api.deps``
refuses every write. A step-in reads a suspended organization too: looking at
one is what it is for.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import tenancy
from tripl.middleware.org_context import OrgRef
from tripl.models.domain_enums import OrganizationStatus
from tripl.models.organization import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_SLUG,
    Organization,
    OrganizationMember,
)
from tripl.models.platform_step_in import PlatformStepIn
from tripl.models.user import User
from tripl.services.step_in_expiry import close_expired_step_ins

ORG_NOT_FOUND = "Organization not found"
ORG_REQUIRED = "Organization required"
ORG_SUSPENDED = "This organization is suspended"

#: The one predicate "this organization can be acted in". SQL column on the left.
ORG_IS_ACTIVE = Organization.status == OrganizationStatus.active.value
#: "This organization exists for its members": active or suspended, never
#: ``deleting``. A suspended one is listed (with its status) but refused.
ORG_IS_VISIBLE = Organization.status != OrganizationStatus.deleting.value


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORG_NOT_FOUND)


def suspended_error() -> HTTPException:
    """The 403 every org-scoped request of a suspended organization's members gets."""
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORG_SUSPENDED)


def is_suspended(org_status: object) -> bool:
    return str(org_status) == OrganizationStatus.suspended.value


async def active_step_in_id(
    session: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID
) -> uuid.UUID | None:
    """The id of ``user_id``'s live step-in to ``org_id``, or ``None``.

    Live: not ended and not expired. Models only, so ``project_access`` can ask
    it too (the event stream re-checks it between events).
    """
    step_in_id: uuid.UUID | None = await session.scalar(
        select(PlatformStepIn.id)
        .where(
            PlatformStepIn.user_id == user_id,
            PlatformStepIn.organization_id == org_id,
            PlatformStepIn.ended_at.is_(None),
            PlatformStepIn.expires_at > datetime.now(UTC),
        )
        .order_by(PlatformStepIn.expires_at.desc())
        .limit(1)
    )
    return step_in_id


async def _org_by_slug(session: AsyncSession, slug: str) -> tuple[OrgRef, bool] | None:
    """``(org, suspended)`` for an active or suspended organization, else ``None``."""
    row = (
        await session.execute(
            select(Organization.id, Organization.status).where(
                Organization.slug == slug, ORG_IS_VISIBLE
            )
        )
    ).first()
    if row is None:
        return None
    org_id, org_status = row
    return OrgRef(id=org_id, slug=slug), is_suspended(org_status)


async def _org_by_id(session: AsyncSession, org_id: uuid.UUID) -> OrgRef:
    if org_id == DEFAULT_ORG_ID:
        # The default organization cannot be deleted or suspended.
        return OrgRef(id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG)
    row = (
        await session.execute(
            select(Organization.slug, Organization.status).where(
                Organization.id == org_id, ORG_IS_VISIBLE
            )
        )
    ).first()
    if row is None:
        # The key's organization_id is a foreign key; a missing row means it was
        # deleted under the key, which then reaches nothing — and so does a key
        # of an organization that is being deleted.
        raise _not_found()
    slug, org_status = row
    if is_suspended(org_status):
        raise suspended_error()
    return OrgRef(id=org_id, slug=slug)


async def _is_member(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    member_id: uuid.UUID | None = await session.scalar(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    return member_id is not None


async def _step_in_ref(session: AsyncSession, user: User, org: OrgRef) -> OrgRef | None:
    """``org`` bound as a step-in of ``user``'s, if they hold a live one.

    Only a platform admin steps in, and only while the flag holds: revoking it
    ends every step-in at once.
    """
    if not user.is_platform_admin:
        return None
    if await active_step_in_id(session, user.id, org.id) is None:
        # An expired one nobody ended is recorded now, the first time it is seen.
        await close_expired_step_ins(session, user, org_id=org.id)
        return None
    return OrgRef(id=org.id, slug=org.slug, step_in_user_id=user.id)


async def default_org_for(session: AsyncSession, user: User) -> OrgRef:
    """The default organization; a platform admin's step-in when they hold one.

    A single-team instance's answer for a URL that names no organization
    (``tenancy.TenancyPolicy.orgless_org``).

    Every other user acts in it without a query, as before organizations
    existed. A platform admin who is not a member and has stepped in reads it
    through the step-in, so the read-only fence applies to them there too.
    """
    default = OrgRef(id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG)
    if not user.is_platform_admin or await _is_member(session, DEFAULT_ORG_ID, user.id):
        return default
    return await _step_in_ref(session, user, default) or default


async def only_org_of(session: AsyncSession, user_id: uuid.UUID) -> OrgRef:
    """The user's single ACTIVE organization: a multi-tenant instance's org-less URL.

    A suspended membership never counts toward "single": a user of one active
    and one suspended organization acts in the active one. Only a user with no
    active membership at all but a suspended one gets the suspended 403; any
    other count is the 400 "Organization required", as before suspension.
    """
    rows = (
        await session.execute(
            select(Organization.id, Organization.slug, Organization.status)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(OrganizationMember.user_id == user_id, ORG_IS_VISIBLE)
        )
    ).all()
    active = [(org_id, slug) for org_id, slug, org_status in rows if not is_suspended(org_status)]
    if len(active) == 1:
        org_id, slug = active[0]
        return OrgRef(id=org_id, slug=slug)
    if not active and rows:
        raise suspended_error()
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ORG_REQUIRED)


async def resolve_request_org(
    session: AsyncSession,
    *,
    user: User,
    key_org_id: uuid.UUID | None,
    path_org_slug: str | None,
) -> OrgRef:
    """The organization this request acts in; see the module docstring for the rules."""
    path_org: OrgRef | None = None
    path_suspended = False
    if path_org_slug is not None:
        found = await _org_by_slug(session, path_org_slug)
        if found is None:
            raise _not_found()
        path_org, path_suspended = found

    if key_org_id is not None:
        if path_org is not None:
            if path_org.id != key_org_id:
                raise _not_found()
            if path_suspended:
                raise suspended_error()
            return path_org
        return await _org_by_id(session, key_org_id)

    policy = tenancy.policy()
    if path_org is not None:
        if not policy.multi_tenant and path_org.id == DEFAULT_ORG_ID:
            return await default_org_for(session, user)
        if await _is_member(session, path_org.id, user.id):
            if path_suspended:
                raise suspended_error()
            return path_org
        step_in = await _step_in_ref(session, user, path_org)
        if step_in is None:
            raise _not_found()
        return step_in

    return await policy.orgless_org(session, user)
