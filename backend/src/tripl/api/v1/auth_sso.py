"""Signing in through an organization's identity provider: ``/api/v1/auth/sso`` (F20).

Unauthenticated by nature, like the rest of ``/auth``. See
:mod:`tripl.services.sso_login_service` for the flow.

* ``GET /discover?email=`` — which organizations sign this address in (the
  status bucket).
* ``GET /{org}/start?next=`` — 302 to the provider (OIDC authorization URL, or
  the SAML HTTP-Redirect binding). ``next`` is kept only when it is a
  same-origin path.
* ``GET /{org}/callback`` — the OIDC provider's redirect back. 302 to ``next``
  signed in, to ``/sso/link?ticket=`` when an existing account must confirm
  the link first, or to ``/auth?sso_error=<code>``.
* ``POST /{org}/saml/acs`` — the SAML provider's HTTP-POST (form fields
  ``SAMLResponse``, ``RelayState``); the same three outcomes, as 303s.
* ``GET /{org}/saml/metadata`` — tripl's SP metadata for an organization
  configured for SAML (its URL is also tripl's entity id); 404 otherwise.
* ``GET /link?ticket=`` — what a link ticket would link, and whether this
  browser must sign in to the account first (the status bucket).
* ``POST /link`` — confirm it from a session of the account: links, joins,
  signs in (the login bucket).

``start`` and ``callback`` share the ``sso`` rate-limit bucket (not the
password-login one) and answer an empty bucket with
``/auth?sso_error=rate_limited``, never a JSON 429 in the middle of a redirect.

The ``state`` is also bound to the browser that started the sign-in (an
HttpOnly cookie compared at the callback), so a callback URL carried to another
browser signs nobody in there. The SAML ACS is a cross-site form POST, which
a ``SameSite=Lax`` cookie does not accompany: a SAML start sets its own cookie,
``tripl_saml_state``, ``SameSite=None; Secure; HttpOnly`` and limited to this
router's path — so SAML sign-in needs https (browsers treat ``localhost`` as
secure). There is no CSRF middleware to exempt the ACS from: the app's CSRF
defence is the ``SameSite=Lax`` session cookie, and the ACS acts on no session.
"""

from __future__ import annotations

import hmac
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import SessionDep
from tripl.api.v1._auth_redirects import app_base_url, error_redirect
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
from tripl.models.org_sso import PROTOCOL_SAML
from tripl.models.user import User
from tripl.schemas.org_sso import (
    SsoDiscoverOrg,
    SsoDiscoverResponse,
    SsoLinkConfirm,
    SsoLinkPreview,
    SsoLinkResult,
)
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import (
    auth_service,
    org_sso_service,
    saml_login_service,
    sso_login_service,
)
from tripl.services.oidc import flow as oidc_flow
from tripl.services.oidc.flow import SignInFlowError
from tripl.services.sso_login_service import (
    NeedsLink,
    SignedIn,
)

router = APIRouter(prefix="/auth/sso", tags=["auth"])

STATE_COOKIE = "tripl_sso_state"
SAML_STATE_COOKIE = "tripl_saml_state"
_STATE_COOKIE_PATH = "/api/v1/auth/sso"
_MAX_STATE = 512
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


def _error_redirect(
    base: str, code: str, *, status_code: int = status.HTTP_302_FOUND
) -> RedirectResponse:
    response = error_redirect(base, code, status_code=status_code)
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
    response.delete_cookie(
        key=SAML_STATE_COOKIE,
        httponly=True,
        samesite="none",
        secure=True,
        path=_STATE_COOKIE_PATH,
    )


def _state_bound(request: Request, cookie: str, state: str | None) -> bool:
    bound = request.cookies.get(cookie)
    if state is not None and len(state) > _MAX_STATE:
        return False
    return bool(state and bound and hmac.compare_digest(bound.encode(), state.encode()))


def _outcome_redirect(
    base: str, outcome: SignedIn | NeedsLink, *, status_code: int
) -> RedirectResponse:
    if isinstance(outcome, NeedsLink):
        target = "/sso/link?" + urlencode({"ticket": outcome.ticket})
        response = RedirectResponse(f"{base}{target}", status_code=status_code)
    else:
        response = RedirectResponse(f"{base}{outcome.next_path}", status_code=status_code)
        _set_session_cookie(response, outcome.session_token)
    _clear_state_cookie(response)
    return response


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
    except SignInFlowError:
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
    except SignInFlowError as exc:
        if exc.code == oidc_flow.ERR_UNAVAILABLE:
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
        return _error_redirect(base, oidc_flow.ERR_RATE_LIMITED)
    try:
        started = await sso_login_service.start(
            session, org_slug=org_slug, next_path=next_path, app_base_url=base
        )
    except SignInFlowError as exc:
        return _error_redirect(base, exc.code)
    response = RedirectResponse(started.authorization_url, status_code=status.HTTP_302_FOUND)
    max_age = int(sso_login_service.STATE_TTL.total_seconds())
    if started.protocol == PROTOCOL_SAML:
        # The ACS is a cross-site POST: only a SameSite=None cookie comes with it.
        response.set_cookie(
            key=SAML_STATE_COOKIE,
            value=started.state,
            httponly=True,
            max_age=max_age,
            samesite="none",
            secure=True,
            path=_STATE_COOKIE_PATH,
        )
    else:
        response.set_cookie(
            key=STATE_COOKIE,
            value=started.state,
            httponly=True,
            max_age=max_age,
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
        return _error_redirect(base, oidc_flow.ERR_RATE_LIMITED)
    if not _state_bound(request, STATE_COOKIE, state):
        return _error_redirect(base, oidc_flow.ERR_STATE)
    try:
        outcome = await sso_login_service.callback(
            session,
            org_slug=org_slug,
            code=code,
            raw_state=state,
            idp_error=error,
            app_base_url=base,
        )
    except SignInFlowError as exc:
        return _error_redirect(base, exc.code)
    return _outcome_redirect(base, outcome, status_code=status.HTTP_302_FOUND)


@router.post(
    "/{org_slug}/saml/acs",
    response_class=RedirectResponse,
    status_code=status.HTTP_303_SEE_OTHER,
)
async def saml_acs(
    request: Request,
    session: SessionDep,
    org_slug: str,
    # No max_length: an oversized field must end in the sso_error redirect,
    # not a JSON 422 (the body-size middleware bounds it; the service caps it).
    saml_response: Annotated[FreeTextFilter | None, Form(alias="SAMLResponse")] = None,
    relay_state: Annotated[FreeTextFilter | None, Form(alias="RelayState")] = None,
) -> RedirectResponse:
    """The SAML provider's HTTP-POST back; lands like the OIDC callback (303s).

    Unauthenticated; authorized by the single-use state in ``RelayState``,
    bound to this browser by the ``tripl_saml_state`` cookie, and by the
    signed assertion answering that state's AuthnRequest. IdP-initiated
    (unsolicited) responses are refused: they carry no state.
    """
    base = await app_base_url(session, request)
    see_other = status.HTTP_303_SEE_OTHER
    if not await allow(sso_rate_limiter, request):
        return _error_redirect(base, oidc_flow.ERR_RATE_LIMITED, status_code=see_other)
    if not relay_state:
        # IdP-initiated: no sign-in of this browser to answer.
        return _error_redirect(base, sso_login_service.ERR_SAML_UNSOLICITED, status_code=see_other)
    if not _state_bound(request, SAML_STATE_COOKIE, relay_state):
        return _error_redirect(base, oidc_flow.ERR_STATE, status_code=see_other)
    try:
        outcome = await saml_login_service.acs(
            session,
            org_slug=org_slug,
            saml_response_b64=saml_response,
            relay_state=relay_state,
            app_base_url=base,
        )
    except SignInFlowError as exc:
        return _error_redirect(base, exc.code, status_code=see_other)
    return _outcome_redirect(base, outcome, status_code=see_other)


@router.get(
    "/{org_slug}/saml/metadata",
    response_class=Response,
    dependencies=[Depends(enforce(status_rate_limiter))],
    responses={200: {"content": {"application/samlmetadata+xml": {}}}},
)
async def saml_metadata(request: Request, session: SessionDep, org_slug: str) -> Response:
    """tripl's SAML SP metadata for the organization (unsigned; public by nature).

    Assertions must be signed (``WantAssertionsSigned``); tripl's AuthnRequests
    are not (it holds no SP key). 404 unless the organization is configured
    for SAML.
    """
    base = await app_base_url(session, request)
    document = await saml_login_service.sp_metadata(session, org_slug=org_slug, app_base_url=base)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return Response(content=document, media_type="application/samlmetadata+xml")
