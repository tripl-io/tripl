"""The platform console (F20 PR14, GH #273): organizations, users, step-ins.

Everything under ``/api/v1/platform`` is the operator's, and org-free (no
organization is resolved or bound: ``deps._ORG_FREE_PATH_PREFIXES``). Every
route takes :data:`PlatformAdminUserDep`: ``users.is_platform_admin`` from a
browser session; an API key is refused whatever its scope or owner.

* ``GET /platform/orgs`` and ``GET /platform/orgs/{org_slug}`` — metadata and
  counts only, never project content.
* ``POST /platform/orgs/{org_slug}/suspend`` / ``.../unsuspend`` — audited in
  the target organization.
* ``GET /platform/users`` and ``POST /platform/users/{user_id}/platform-admin``.
* ``POST /platform/orgs/{org_slug}/step-in``, ``POST /platform/step-ins/{id}/end``
  and ``GET /platform/step-ins`` — the read-only step-in; see
  ``services.org_resolution`` for what it admits and ``api.deps`` for the
  read-only fence.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from tripl.api.deps import PlatformAdminUserDep, SessionDep
from tripl.models.domain_enums import OrganizationStatus
from tripl.schemas.platform_console import (
    OrgSuspendRequest,
    PlatformAdminGrant,
    PlatformOrgDetail,
    PlatformOrgList,
    PlatformUserItem,
    PlatformUserList,
    StepInRequest,
    StepInResponse,
)
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import platform_console_service as console

router = APIRouter(prefix="/platform", tags=["platform"])


def _conflict(exc: console.ConsoleConflictError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _org_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=console.ORG_NOT_FOUND)


@router.get("/orgs", response_model=PlatformOrgList)
async def list_platform_orgs(
    session: SessionDep,
    _current_user: PlatformAdminUserDep,
    # FreeTextFilter: ``q`` binds into a LIKE, and a NUL would abort in asyncpg.
    q: Annotated[
        FreeTextFilter | None,
        Query(max_length=255, description="Substring of the slug or name."),
    ] = None,
    org_status: Annotated[OrganizationStatus | None, Query(alias="status")] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PlatformOrgList:
    """Every organization with its status, member and project counts and owners."""
    return await console.list_orgs(session, q=q, status=org_status, limit=limit, offset=offset)


@router.get("/orgs/{org_slug}", response_model=PlatformOrgDetail)
async def get_platform_org(
    session: SessionDep, org_slug: str, _current_user: PlatformAdminUserDep
) -> PlatformOrgDetail:
    """One organization: its members (email, name, role) and projects (slug, name)."""
    try:
        return await console.get_org(session, org_slug)
    except console.ConsoleOrgNotFoundError:
        raise _org_not_found() from None


@router.post("/orgs/{org_slug}/suspend", response_model=PlatformOrgDetail)
async def suspend_platform_org(
    session: SessionDep,
    org_slug: str,
    data: OrgSuspendRequest,
    current_user: PlatformAdminUserDep,
) -> PlatformOrgDetail:
    """Suspend an organization: its members get 403 and its scheduled jobs stop."""
    try:
        return await console.suspend_org(session, org_slug, reason=data.reason, actor=current_user)
    except console.ConsoleOrgNotFoundError:
        raise _org_not_found() from None
    except console.ConsoleConflictError as exc:
        raise _conflict(exc) from None


@router.post("/orgs/{org_slug}/unsuspend", response_model=PlatformOrgDetail)
async def unsuspend_platform_org(
    session: SessionDep, org_slug: str, current_user: PlatformAdminUserDep
) -> PlatformOrgDetail:
    """Put a suspended organization back; everything resumes where it stopped."""
    try:
        return await console.unsuspend_org(session, org_slug, actor=current_user)
    except console.ConsoleOrgNotFoundError:
        raise _org_not_found() from None
    except console.ConsoleConflictError as exc:
        raise _conflict(exc) from None


@router.get("/users", response_model=PlatformUserList)
async def list_platform_users(
    session: SessionDep,
    _current_user: PlatformAdminUserDep,
    q: Annotated[
        FreeTextFilter | None,
        Query(max_length=320, description="Substring of the email or name."),
    ] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> PlatformUserList:
    """Every account, with its platform-admin flag and organization count."""
    return await console.list_users(session, q=q, limit=limit, offset=offset)


@router.post("/users/{user_id}/platform-admin", response_model=PlatformUserItem)
async def set_platform_admin(
    session: SessionDep,
    user_id: uuid.UUID,
    data: PlatformAdminGrant,
    current_user: PlatformAdminUserDep,
) -> PlatformUserItem:
    """Grant or revoke the platform-admin flag. Not yourself, not the last admin."""
    try:
        await console.set_platform_admin(session, user_id, grant=data.grant, actor=current_user)
        return await console.get_user_item(session, user_id)
    except console.ConsoleUserNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=console.USER_NOT_FOUND
        ) from None
    except console.ConsoleConflictError as exc:
        raise _conflict(exc) from None


@router.post(
    "/orgs/{org_slug}/step-in",
    response_model=StepInResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_step_in(
    session: SessionDep,
    org_slug: str,
    data: StepInRequest,
    current_user: PlatformAdminUserDep,
) -> StepInResponse:
    """Open a read-only step-in: a reason, a time limit, audited in the organization."""
    try:
        return await console.start_step_in(
            session,
            org_slug,
            reason=data.reason,
            ttl_minutes=data.ttl_minutes,
            actor=current_user,
        )
    except console.ConsoleOrgNotFoundError:
        raise _org_not_found() from None
    except console.ConsoleConflictError as exc:
        raise _conflict(exc) from None


@router.post("/step-ins/{step_in_id}/end", response_model=StepInResponse)
async def end_step_in(
    session: SessionDep, step_in_id: uuid.UUID, current_user: PlatformAdminUserDep
) -> StepInResponse:
    """End one of the caller's step-ins now."""
    try:
        return await console.end_step_in(session, step_in_id, actor=current_user)
    except console.StepInNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=console.STEP_IN_NOT_FOUND
        ) from None
    except console.ConsoleConflictError as exc:
        raise _conflict(exc) from None


@router.get("/step-ins", response_model=list[StepInResponse])
async def list_step_ins(
    session: SessionDep,
    current_user: PlatformAdminUserDep,
    active: bool | None = Query(None, description="true: live only; false: ended or expired."),
) -> list[StepInResponse]:
    """The caller's own step-ins, newest first."""
    return await console.list_step_ins(session, actor=current_user, active=active)
