"""Sign in through the instance's OpenID Connect provider (``OIDC_*``).

Any provider that speaks OpenID Connect — Okta, Microsoft Entra ID, Keycloak,
Authentik, Auth0, ... — for the whole instance, beside Google, behind one
button on the sign-in page labelled ``OIDC_BUTTON_LABEL``. The flow is
``instance_login``'s.

An address the provider marks verified signs in to the account that has it, so
the provider must verify addresses before it vouches for them. With
``OIDC_ALLOWED_DOMAINS`` set, only addresses at those domains sign in at all —
platform admins included. ``OIDC_AUTO_CREATE_USERS=false`` signs in existing
accounts only.

Per-organization SSO, requiring it, verified domains and SCIM are the
Enterprise edition's.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import settings
from tripl.services import instance_login
from tripl.services.instance_login import Provider, SignedIn, StartedLogin
from tripl.services.oidc.id_tokens import IdTokenClaims

CALLBACK_PATH = "/api/v1/auth/oidc/callback"


def provider() -> Provider:
    return Provider(
        key="oidc",
        issuer=settings.oidc_issuer.rstrip("/"),
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        scopes=settings.oidc_scopes,
        callback_path=CALLBACK_PATH,
        audit_action="user.oidc_sign_in",
        allowed_domains=tuple(settings.oidc_allowed_domains),
        create_accounts=settings.oidc_auto_create_users,
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
) -> SignedIn:
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
