"""Sign in with Google: the instance-wide OAuth client.

Not an organization's SSO: one Google client for the whole instance,
configured by the operator (``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET``),
behind one button on the sign-in page. The flow is ``instance_login``'s,
shared with any other OpenID Connect provider (``oidc_login_service``); this
is Google's preset: its issuer, its account chooser, and
``GOOGLE_ALLOWED_DOMAINS``, which a platform admin may sign in from outside
of.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import settings
from tripl.services import instance_login
from tripl.services.instance_login import Provider, StartedLogin, home_org_id
from tripl.services.oidc.id_tokens import IdTokenClaims

GOOGLE_ISSUER = "https://accounts.google.com"
SCOPES = "openid email profile"
CALLBACK_PATH = "/api/v1/auth/google/callback"

__all__ = [
    "CALLBACK_PATH",
    "GOOGLE_ISSUER",
    "callback",
    "enabled",
    "home_org_id",
    "provider",
    "start",
]


def provider() -> Provider:
    return Provider(
        key="google",
        issuer=GOOGLE_ISSUER,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
        callback_path=CALLBACK_PATH,
        audit_action="user.google_sign_in",
        allowed_domains=tuple(settings.google_allowed_domains),
        admins_bypass_domains=True,
        extra_params={"prompt": "select_account"},
    )


def enabled() -> bool:
    return provider().configured


async def start(*, next_path: str | None, app_base_url: str) -> StartedLogin:
    return await instance_login.start(provider(), next_path=next_path, app_base_url=app_base_url)


def _authenticate(*, code: str, redirect_to: str, verifier: str, nonce: str) -> IdTokenClaims:
    """Discovery, code exchange, JWKS and id_token checks. Blocking."""
    return instance_login.authenticate(
        provider(), code=code, redirect_to=redirect_to, verifier=verifier, nonce=nonce
    )


async def callback(
    session: AsyncSession,
    *,
    code: str | None,
    state: str | None,
    idp_error: str | None,
    cookie: str | None,
    app_base_url: str,
) -> instance_login.SignedIn:
    """Finish the sign-in (``instance_login.callback``). Commits."""
    return await instance_login.callback(
        provider(),
        session,
        code=code,
        state=state,
        idp_error=idp_error,
        cookie=cookie,
        app_base_url=app_base_url,
        # Looked up at call time: the tests replace it.
        authenticate=lambda **kwargs: _authenticate(**kwargs),
    )
