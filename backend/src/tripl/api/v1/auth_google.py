"""Sign in with Google: ``start`` and Google's ``callback``.

Browser redirects, like an organization's SSO (``auth_sso``): every outcome
lands back in the app, a failure as ``/auth?sso_error=<code>`` with the same
codes, so the sign-in page explains it the same way.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import RedirectResponse

from tripl.api.deps import SessionDep
from tripl.api.v1._auth_redirects import app_base_url, error_redirect
from tripl.api.v1.auth import _set_session_cookie
from tripl.config import settings
from tripl.middleware.rate_limit import allow, sso_rate_limiter
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import google_login_service
from tripl.services.oidc.flow import ERR_RATE_LIMITED, SignInFlowError

router = APIRouter(prefix="/auth/google", tags=["auth"])

STATE_COOKIE = "tripl_google_state"
#: Only Google's redirect back needs the cookie.
_STATE_COOKIE_PATH = "/api/v1/auth/google"


def _clear_state(response: RedirectResponse) -> RedirectResponse:
    response.delete_cookie(
        key=STATE_COOKIE,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path=_STATE_COOKIE_PATH,
    )
    return response


@router.get("/start", response_class=RedirectResponse, status_code=status.HTTP_302_FOUND)
async def start(
    request: Request,
    session: SessionDep,
    next_path: Annotated[FreeTextFilter | None, Query(alias="next", max_length=2048)] = None,
) -> RedirectResponse:
    """Send the browser to Google's account chooser."""
    base = await app_base_url(session, request)
    if not await allow(sso_rate_limiter, request):
        return error_redirect(base, ERR_RATE_LIMITED)
    try:
        started = await google_login_service.start(next_path=next_path, app_base_url=base)
    except SignInFlowError as exc:
        return error_redirect(base, exc.code)
    response = RedirectResponse(started.authorization_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=STATE_COOKIE,
        value=started.cookie,
        httponly=True,
        max_age=int(google_login_service.STATE_TTL.total_seconds()),
        # Lax: Google's redirect back is a top-level GET, which carries it.
        samesite="lax",
        secure=settings.session_cookie_secure,
        path=_STATE_COOKIE_PATH,
    )
    return response


@router.get("/callback", response_class=RedirectResponse, status_code=status.HTTP_302_FOUND)
async def callback(
    request: Request,
    session: SessionDep,
    code: Annotated[FreeTextFilter | None, Query(max_length=4096)] = None,
    state: Annotated[FreeTextFilter | None, Query(max_length=512)] = None,
    error: Annotated[FreeTextFilter | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    """Google's redirect back: signed in and into the app, or back to sign-in."""
    base = await app_base_url(session, request)
    if not await allow(sso_rate_limiter, request):
        return _clear_state(error_redirect(base, ERR_RATE_LIMITED))
    try:
        outcome = await google_login_service.callback(
            session,
            code=code,
            state=state,
            idp_error=error,
            cookie=request.cookies.get(STATE_COOKIE),
            app_base_url=base,
        )
    except SignInFlowError as exc:
        return _clear_state(error_redirect(base, exc.code))
    response = RedirectResponse(f"{base}{outcome.next_path}", status_code=status.HTTP_302_FOUND)
    _set_session_cookie(response, outcome.session_token)
    return _clear_state(response)
