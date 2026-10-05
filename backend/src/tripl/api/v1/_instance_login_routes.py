"""The two routes of an instance-wide sign-in: ``start`` and the provider's ``callback``.

Browser redirects, like an organization's SSO: every outcome lands back in the
app, a failure as ``/auth?sso_error=<code>``, so the sign-in page explains it
the same way. Google's (``auth_google``) and the instance's OpenID Connect
provider's (``auth_oidc``) are built here, each with its own login-state cookie.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Protocol

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import SessionDep
from tripl.api.v1._auth_redirects import app_base_url, error_redirect
from tripl.api.v1.auth import _set_session_cookie
from tripl.config import settings
from tripl.middleware.rate_limit import allow, sso_rate_limiter
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services.instance_login import STATE_TTL, SignedIn, StartedLogin
from tripl.services.oidc.flow import ERR_RATE_LIMITED, SignInFlowError


class Start(Protocol):
    async def __call__(self, *, next_path: str | None, app_base_url: str) -> StartedLogin: ...


class Callback(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        *,
        code: str | None,
        state: str | None,
        idp_error: str | None,
        cookie: str | None,
        app_base_url: str,
    ) -> SignedIn: ...


def build_router(
    *,
    prefix: str,
    state_cookie: str,
    start_login: Start,
    finish_login: Callback,
    start_description: str,
    callback_description: str,
    state_ttl: timedelta = STATE_TTL,
) -> APIRouter:
    """``{prefix}/start`` and ``{prefix}/callback`` under ``/api/v1``."""
    router = APIRouter(prefix=prefix, tags=["auth"])
    #: Only the provider's redirect back needs the cookie.
    cookie_path = f"/api/v1{prefix}"

    def clear_state(response: RedirectResponse) -> RedirectResponse:
        response.delete_cookie(
            key=state_cookie,
            httponly=True,
            samesite="lax",
            secure=settings.session_cookie_secure,
            path=cookie_path,
        )
        return response

    @router.get(
        "/start",
        response_class=RedirectResponse,
        status_code=status.HTTP_302_FOUND,
        description=start_description,
    )
    async def start(
        request: Request,
        session: SessionDep,
        next_path: Annotated[FreeTextFilter | None, Query(alias="next", max_length=2048)] = None,
    ) -> RedirectResponse:
        base = await app_base_url(session, request)
        if not await allow(sso_rate_limiter, request):
            return error_redirect(base, ERR_RATE_LIMITED)
        try:
            started = await start_login(next_path=next_path, app_base_url=base)
        except SignInFlowError as exc:
            return error_redirect(base, exc.code)
        response = RedirectResponse(started.authorization_url, status_code=status.HTTP_302_FOUND)
        response.set_cookie(
            key=state_cookie,
            value=started.cookie,
            httponly=True,
            max_age=int(state_ttl.total_seconds()),
            # Lax: the provider's redirect back is a top-level GET, which carries it.
            samesite="lax",
            secure=settings.session_cookie_secure,
            path=cookie_path,
        )
        return response

    @router.get(
        "/callback",
        response_class=RedirectResponse,
        status_code=status.HTTP_302_FOUND,
        description=callback_description,
    )
    async def callback(
        request: Request,
        session: SessionDep,
        code: Annotated[FreeTextFilter | None, Query(max_length=4096)] = None,
        state: Annotated[FreeTextFilter | None, Query(max_length=512)] = None,
        error: Annotated[FreeTextFilter | None, Query(max_length=256)] = None,
    ) -> RedirectResponse:
        base = await app_base_url(session, request)
        if not await allow(sso_rate_limiter, request):
            return clear_state(error_redirect(base, ERR_RATE_LIMITED))
        try:
            outcome = await finish_login(
                session,
                code=code,
                state=state,
                idp_error=error,
                cookie=request.cookies.get(state_cookie),
                app_base_url=base,
            )
        except SignInFlowError as exc:
            return clear_state(error_redirect(base, exc.code))
        response = RedirectResponse(f"{base}{outcome.next_path}", status_code=status.HTTP_302_FOUND)
        _set_session_cookie(response, outcome.session_token)
        return clear_state(response)

    return router
