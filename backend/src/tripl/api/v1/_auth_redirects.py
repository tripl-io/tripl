"""Where a browser sign-in flow sends the browser: the app's origin, and back to ``/auth``.

Shared by "Sign in with Google" (``auth_google``) and an organization's single
sign-on (``auth_sso``): every outcome lands back in the app, a failure as
``/auth?sso_error=<code>`` (``tripl.services.oidc.flow``).
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.services import app_settings_service


async def app_base_url(session: AsyncSession, request: Request) -> str:
    """The operator's ``app_base_url``, else the URL this request came in on."""
    overrides = await app_settings_service.get_service_overrides(session)
    configured = app_settings_service.build_runtime_config(overrides).app_base_url
    return (configured or str(request.base_url)).rstrip("/")


def error_redirect(
    base: str, code: str, *, status_code: int = status.HTTP_302_FOUND
) -> RedirectResponse:
    """Back to the sign-in page with the flow's error ``code``."""
    return RedirectResponse(
        f"{base}/auth?" + urlencode({"sso_error": code}), status_code=status_code
    )
