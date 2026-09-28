"""Signing in through an organization's identity provider: ``/api/v1/auth/sso`` (F20).

Unauthenticated by nature, like the rest of ``/auth``. See
:mod:`tripl.services.sso_login_service` for the flow.

* ``GET /discover?email=`` — which organizations sign this address in (the
  status bucket).
* ``GET /{org}/start?next=`` — 302 to the provider. ``next`` is kept only when
  it is a same-origin path.
* ``GET /{org}/callback`` — the provider's redirect back. 302 to ``next``
  signed in, to ``/sso/link?ticket=`` when an existing account must confirm
  the link first, or to ``/auth?sso_error=<code>``.
* ``GET /link?ticket=`` — what a link ticket would link, and whether this
  browser must sign in to the account first (the status bucket).
* ``POST /link`` — confirm it from a session of the account: links, joins,
  signs in (the login bucket).

``start`` and ``callback`` share the ``sso`` rate-limit bucket (not the
password-login one) and answer an empty bucket with
``/auth?sso_error=rate_limited``, never a JSON 429 in the middle of a redirect.

The ``state`` is also bound to the browser that started the sign-in (an
HttpOnly cookie compared at the callback), so a callback URL carried to another
browser signs nobody in there.
"""

from __future__ import annotations

import hmac
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import SessionDep
from tripl.api.v1.auth import _set_session_cookie
from tripl.auth_utils import hash_session_token
from tripl.config import settings
from tripl.middleware.rate_limit import (
    allow,
    enforce,
    login_rate_limiter,
    sso_rate_limiter,
    status_rate_limiter,
)
from tripl.models.user import User
from tripl.schemas.org_sso import (
    SsoDiscoverOrg,
    SsoDiscoverResponse,
    SsoLinkConfirm,
    SsoLinkPreview,
    SsoLinkResult,
)
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import app_settings_service, auth_service, org_sso_service, sso_login_service
from tripl.services.sso_login_service import NeedsLink, SsoFlowError

router = APIRouter(prefix="/auth/sso", tags=["auth"])

STATE_COOKIE = "tripl_sso_state"
_STATE_COOKIE_PATH = "/api/v1/auth/sso"
_LINK_INVALID = "This link is invalid or has expired."
_LINK_SIGN_IN = "Sign in to this account first, then confirm the link."
_LINK_REMOVED = (
    "This account was removed from the organization. Ask an administrator to invite you again."
)


async def _browser_session(
    session: AsyncSession, request: Request
) -> tuple[User | None, str | None]:
    """The browser's signed-in account and its session token digest, if any."""
    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        return None, None
    user = await auth_service.get_user_by_session_token(session, cookie)
    if user is None:
        return None, None
    return user, hash_session_token(cookie)


async def app_base_url(session: AsyncSession, request: Request) -> str:
    """The operator's ``app_base_url``, else the URL this request came in on."""
    overrides = await app_settings_service.get_service_overrides(session)
    configured = app_settings_service.build_runtime_config(overrides).app_base_url
    return (configured or str(request.base_url)).rstrip("/")


def _to_app(base: str, path: str) -> RedirectResponse:
    return RedirectResponse(f"{base}{path}", status_code=status.HTTP_302_FOUND)


def _error_redirect(base: str, code: str) -> RedirectResponse:
    response = _to_app(base, "/auth?" + urlencode({"sso_error": code}))
    _clear_state_cookie(response)
    return response


def _clear_state_cookie(response: Response) -> None:
    response.delete_cookie(
        key=STATE_COOKIE,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path=_STATE_COOKIE_PATH,
    )


@router.get(
    "/discover",
    response_model=SsoDiscoverResponse,
    dependencies=[Depends(enforce(status_rate_limiter))],
)
async def discover(
    session: SessionDep,
    email: Annotated[FreeTextFilter, Query(min_length=3, max_length=320)],
) -> SsoDiscoverResponse:
    """The organizations with SSO enabled that own this address's verified domain."""
    orgs = await sso_login_service.discover(session, email)
    return SsoDiscoverResponse(
        orgs=[
            SsoDiscoverOrg(slug=slug, name=name, login_url=org_sso_service.login_path(slug))
            for slug, name in orgs
        ]
    )


@router.get(
    "/link",
    response_model=SsoLinkPreview,
    dependencies=[Depends(enforce(status_rate_limiter))],
)
async def preview_link(
    request: Request,
    session: SessionDep,
    ticket: Annotated[FreeTextFilter, Query(min_length=1, max_length=512)],
) -> SsoLinkPreview:
    """The account and organization a link ticket names; 400 when it is not live.

    ``sign_in_required``: this browser must sign in to the account before it
    can confirm.
    """
    signed_in, _digest = await _browser_session(session, request)
    try:
        preview = await sso_login_service.preview_link(
            session, ticket, session_user_id=None if signed_in is None else signed_in.id
        )
    except SsoFlowError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_LINK_INVALID) from None
    return SsoLinkPreview(
        email=preview.email,
        org_slug=preview.org_slug,
        org_name=preview.org_name,
        expires_at=preview.expires_at,
        sign_in_required=preview.sign_in_required,
    )


@router.post(
    "/link",
    response_model=SsoLinkResult,
    dependencies=[Depends(enforce(login_rate_limiter))],
)
async def confirm_link(
    request: Request, response: Response, session: SessionDep, data: SsoLinkConfirm
) -> SsoLinkResult:
    """Link the identity to the existing account and sign in with an SSO session.

    Needs a browser session OF THAT ACCOUNT (401 otherwise, the ticket stays
    usable): the ticket proves only the provider sign-in, and whoever runs a
    verified domain's provider can name any of its addresses. The exception is
    an account whose address was never verified (hosted sign-up): it is taken
    over clean, its password, sessions and keys dropped. The account joins the
    organization as ``member`` when it is not in it yet, and its address counts
    as verified. 400 for an unknown, used or expired ticket; 403 for an account
    removed from the organization; 409 when the organization no longer signs in
    through SSO.
    """
    session_user, session_digest = await _browser_session(session, request)
    try:
        signed_in, _org_id = await sso_login_service.confirm_link(
            session, data.ticket, session_user=session_user, session_token_hash=session_digest
        )
    except SsoFlowError as exc:
        if exc.code == sso_login_service.ERR_UNAVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This organization no longer signs in through single sign-on.",
            ) from None
        if exc.code == sso_login_service.ERR_LINK_SIGN_IN:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=_LINK_SIGN_IN
            ) from None
        if exc.code == sso_login_service.ERR_REMOVED:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=_LINK_REMOVED
            ) from None
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_LINK_INVALID) from None
    _set_session_cookie(response, signed_in.session_token)
    return SsoLinkResult(
        next=signed_in.next_path,
        user=await auth_service.build_auth_user_response(session, signed_in.user),
    )


@router.get(
    "/{org_slug}/start",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
)
async def start(
    request: Request,
    session: SessionDep,
    org_slug: str,
    next_path: Annotated[FreeTextFilter | None, Query(alias="next", max_length=2048)] = None,
) -> RedirectResponse:
    """Send the browser to the organization's identity provider."""
    base = await app_base_url(session, request)
    if not await allow(sso_rate_limiter, request):
        return _error_redirect(base, sso_login_service.ERR_RATE_LIMITED)
    try:
        started = await sso_login_service.start(
            session, org_slug=org_slug, next_path=next_path, app_base_url=base
        )
    except SsoFlowError as exc:
        return _error_redirect(base, exc.code)
    response = RedirectResponse(started.authorization_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=STATE_COOKIE,
        value=started.state,
        httponly=True,
        max_age=int(sso_login_service.STATE_TTL.total_seconds()),
        samesite="lax",
        secure=settings.session_cookie_secure,
        path=_STATE_COOKIE_PATH,
    )
    return response


@router.get(
    "/{org_slug}/callback",
    response_class=RedirectResponse,
    status_code=status.HTTP_302_FOUND,
)
async def callback(
    request: Request,
    session: SessionDep,
    org_slug: str,
    code: Annotated[FreeTextFilter | None, Query(max_length=4096)] = None,
    state: Annotated[FreeTextFilter | None, Query(max_length=512)] = None,
    error: Annotated[FreeTextFilter | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    """The provider's redirect back; see the module docstring for where it lands."""
    base = await app_base_url(session, request)
    if not await allow(sso_rate_limiter, request):
        return _error_redirect(base, sso_login_service.ERR_RATE_LIMITED)
    bound = request.cookies.get(STATE_COOKIE)
    if not state or not bound or not hmac.compare_digest(bound.encode(), state.encode()):
        return _error_redirect(base, sso_login_service.ERR_STATE)
    try:
        outcome = await sso_login_service.callback(
            session,
            org_slug=org_slug,
            code=code,
            raw_state=state,
            idp_error=error,
            app_base_url=base,
        )
    except SsoFlowError as exc:
        return _error_redirect(base, exc.code)
    if isinstance(outcome, NeedsLink):
        response = _to_app(base, "/sso/link?" + urlencode({"ticket": outcome.ticket}))
    else:
        response = _to_app(base, outcome.next_path)
        _set_session_cookie(response, outcome.session_token)
    _clear_state_cookie(response)
    return response
