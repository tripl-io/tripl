"""Which organization an authenticated request acts in (F20 PR2, GH #273).

Called from the auth dependency (:mod:`tripl.api.deps`) once the caller is
known and before any project slug is resolved. The rules, in order:

1. An org named in the URL (``/api/v1/orgs/{org}/...``) must exist, else 404
   "Organization not found".
2. An API key belongs to one organization: that is the org. A URL naming a
   different one gets the same 404, so a key is no oracle for other orgs.
3. A cookie session with an org in the URL must be a member of it, else the
   same 404.
4. No org in the URL: a self-hosted instance acts in the default organization
   (no query, exactly as before organizations existed); a hosted one acts in the
   user's only organization, and answers 400 "Organization required" when the
   user has none or several. There is never a fallback.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import DEPLOYMENT_SELF_HOSTED, settings
from tripl.middleware.org_context import OrgRef
from tripl.models.organization import (
    DEFAULT_ORG_ID,
    DEFAULT_ORG_SLUG,
    Organization,
    OrganizationMember,
)
from tripl.models.user import User

ORG_NOT_FOUND = "Organization not found"
ORG_REQUIRED = "Organization required"


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ORG_NOT_FOUND)


async def _org_by_slug(session: AsyncSession, slug: str) -> OrgRef | None:
    org_id: uuid.UUID | None = await session.scalar(
        select(Organization.id).where(Organization.slug == slug)
    )
    return None if org_id is None else OrgRef(id=org_id, slug=slug)


async def _org_by_id(session: AsyncSession, org_id: uuid.UUID) -> OrgRef:
    if org_id == DEFAULT_ORG_ID:
        return OrgRef(id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG)
    slug: str | None = await session.scalar(
        select(Organization.slug).where(Organization.id == org_id)
    )
    if slug is None:
        # The key's organization_id is a foreign key; a missing row means it was
        # deleted under the key, which then reaches nothing.
        raise _not_found()
    return OrgRef(id=org_id, slug=slug)


async def _is_member(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    member_id: uuid.UUID | None = await session.scalar(
        select(OrganizationMember.id).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    return member_id is not None


async def _only_org_of(session: AsyncSession, user_id: uuid.UUID) -> OrgRef:
    rows = (
        await session.execute(
            select(Organization.id, Organization.slug)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(OrganizationMember.user_id == user_id)
            .limit(2)
        )
    ).all()
    if len(rows) != 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ORG_REQUIRED)
    org_id, slug = rows[0]
    return OrgRef(id=org_id, slug=slug)


async def resolve_request_org(
    session: AsyncSession,
    *,
    user: User,
    key_org_id: uuid.UUID | None,
    path_org_slug: str | None,
) -> OrgRef:
    """The organization this request acts in; see the module docstring for the rules."""
    path_org: OrgRef | None = None
    if path_org_slug is not None:
        path_org = await _org_by_slug(session, path_org_slug)
        if path_org is None:
            raise _not_found()

    if key_org_id is not None:
        if path_org is not None:
            if path_org.id != key_org_id:
                raise _not_found()
            return path_org
        return await _org_by_id(session, key_org_id)

    if path_org is not None:
        if not await _is_member(session, path_org.id, user.id):
            raise _not_found()
        return path_org

    if settings.deployment_mode == DEPLOYMENT_SELF_HOSTED:
        return OrgRef(id=DEFAULT_ORG_ID, slug=DEFAULT_ORG_SLUG)
    return await _only_org_of(session, user.id)
