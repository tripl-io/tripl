"""Sign in with Google: the instance-wide OAuth client.

Not an organization's SSO (``sso_login_service``): one Google client for the
whole instance, configured by the operator (``GOOGLE_CLIENT_ID`` /
``GOOGLE_CLIENT_SECRET``), behind one button on the sign-in page. The OIDC
mechanics are the SSO flow's own — discovery, PKCE, code exchange, JWKS and
id_token checks (``idp_http``, ``id_tokens``) — so there is one implementation
of the protocol, not two.

The login state (``state``, ``nonce``, PKCE verifier, where to land, expiry)
travels in an encrypted, http-only cookie bound to the browser that started
the sign-in, rather than in ``sso_login_states``, whose rows belong to an
organization. The callback checks the returned ``state`` against it, re-checks
where it lands, and the cookie is cleared on the way out.

Who signs in, given a Google-verified address:

* an account with that address signs in. One nobody ever proved the address of
  (a hosted sign-up still unverified) is first taken over clean, exactly as an
  organization's SSO does (``oidc_accounts.reclaim_if_unclaimed``);
* otherwise an account is created, where sign-up is open and the address is in
  ``GOOGLE_ALLOWED_DOMAINS`` (when that lists any). Self-hosted it joins the
  default organization as the password sign-up would; hosted it gets an
  organization of its own, and so does an account left in none (its sandbox
  was retired as idle, the Enterprise idle-organization purge).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import tenancy
from tripl.auth_utils import normalize_email
from tripl.config import settings
from tripl.crypto import decrypt_value, encrypt_value
from tripl.models.domain_enums import OrganizationRole
from tripl.models.organization import DEFAULT_ORG_ID, OrganizationMember
from tripl.models.user import User
from tripl.services import (
    audit_service,
    auth_service,
    email_verification_service,
)
from tripl.services.oidc import accounts as oidc_accounts
from tripl.services.oidc import flow as oidc_flow
from tripl.services.oidc import id_tokens, idp_http
from tripl.services.oidc.flow import (
    ERR_DENIED,
    ERR_DOMAIN,
    ERR_EMAIL_UNVERIFIED,
    ERR_IDP,
    ERR_STATE,
    ERR_UNAVAILABLE,
    SignInFlowError,
    safe_next,
)
from tripl.services.oidc.id_tokens import IdTokenClaims
from tripl.services.oidc.idp_http import IdpError

GOOGLE_ISSUER = "https://accounts.google.com"
SCOPES = "openid email profile"
STATE_TTL = timedelta(minutes=10)
CALLBACK_PATH = "/api/v1/auth/google/callback"
#: Sign-up is closed and no account has the address.
ERR_SIGNUP_CLOSED = "signup_closed"

_TOKEN_BYTES = 32
_VERIFIER_BYTES = 64


def enabled() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def redirect_uri(app_base_url: str) -> str:
    return f"{app_base_url.rstrip('/')}{CALLBACK_PATH}"


@dataclass(frozen=True)
class StartedGoogleLogin:
    authorization_url: str
    #: The encrypted login state, for the http-only cookie.
    cookie: str


async def start(*, next_path: str | None, app_base_url: str) -> StartedGoogleLogin:
    """Build Google's sign-in URL and the cookie that carries the login state."""
    if not enabled():
        raise SignInFlowError(ERR_UNAVAILABLE)
    try:
        discovery = await asyncio.to_thread(idp_http.fetch_discovery, GOOGLE_ISSUER)
    except IdpError:
        raise SignInFlowError(ERR_IDP) from None
    state = secrets.token_urlsafe(_TOKEN_BYTES)
    nonce = secrets.token_urlsafe(_TOKEN_BYTES)
    verifier = secrets.token_urlsafe(_VERIFIER_BYTES)
    cookie = encrypt_value(
        json.dumps(
            {
                "state": state,
                "nonce": nonce,
                "verifier": verifier,
                "next": safe_next(next_path),
                "exp": (datetime.now(UTC) + STATE_TTL).timestamp(),
            }
        )
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.google_client_id,
            "redirect_uri": redirect_uri(app_base_url),
            "scope": SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": oidc_flow.pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    return StartedGoogleLogin(
        authorization_url=f"{discovery.authorization_endpoint}?{query}", cookie=cookie
    )


def _login_state(cookie: str | None, state: str | None) -> dict[str, Any]:
    """The cookie's login state, when it is ours, unexpired and for this ``state``."""
    if not cookie or not state:
        raise SignInFlowError(ERR_STATE)
    try:
        payload = json.loads(decrypt_value(cookie))
    except InvalidToken, ValueError, TypeError:
        raise SignInFlowError(ERR_STATE) from None
    if not isinstance(payload, dict):
        raise SignInFlowError(ERR_STATE)
    try:
        expires = float(payload.get("exp", 0))
    except TypeError, ValueError:
        raise SignInFlowError(ERR_STATE) from None
    if expires < datetime.now(UTC).timestamp():
        raise SignInFlowError(ERR_STATE)
    expected = str(payload.get("state", ""))
    if not secrets.compare_digest(expected.encode(), state.encode()):
        raise SignInFlowError(ERR_STATE)
    return payload


def _authenticate(*, code: str, redirect_to: str, verifier: str, nonce: str) -> IdTokenClaims:
    """Discovery, code exchange, JWKS and id_token checks. Blocking."""
    discovery = idp_http.fetch_discovery(GOOGLE_ISSUER)
    id_token = id_tokens.exchange_code(
        discovery,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        code=code,
        redirect_uri=redirect_to,
        code_verifier=verifier,
    )
    keys = idp_http.fetch_jwks(discovery)
    return id_tokens.verify_id_token(
        id_token,
        keys=keys,
        issuer=GOOGLE_ISSUER,
        client_id=settings.google_client_id,
        nonce=nonce,
    )


@dataclass(frozen=True)
class GoogleSignedIn:
    user: User
    session_token: str
    next_path: str


async def callback(
    session: AsyncSession,
    *,
    code: str | None,
    state: str | None,
    idp_error: str | None,
    cookie: str | None,
    app_base_url: str,
) -> GoogleSignedIn:
    """Finish the sign-in; see the module docstring for who it signs in. Commits."""
    if not enabled():
        raise SignInFlowError(ERR_UNAVAILABLE)
    login = _login_state(cookie, state)
    if idp_error:
        raise SignInFlowError(ERR_DENIED)
    if not code:
        raise SignInFlowError(ERR_IDP)
    try:
        claims = await asyncio.to_thread(
            _authenticate,
            code=code,
            redirect_to=redirect_uri(app_base_url),
            verifier=str(login.get("verifier", "")),
            nonce=str(login.get("nonce", "")),
        )
    except IdpError as exc:
        raise SignInFlowError(oidc_flow.flow_code(exc)) from None
    if not claims.email_verified:
        raise SignInFlowError(ERR_EMAIL_UNVERIFIED)
    user = await sign_in_verified(session, email=claims.email, name=claims.name)
    token = await auth_service.create_session_for_user(session, user.id)
    await audit_service.record(
        session,
        user=user,
        action="user.google_sign_in",
        target_type="user",
        target_id=user.id,
        target_name=user.email,
        payload={"issuer": GOOGLE_ISSUER},
        organization_id=await home_org_id(session, user.id),
    )
    await session.refresh(user)
    # Re-checked: without an encryption key the cookie is only encoded.
    return GoogleSignedIn(
        user=user, session_token=token, next_path=safe_next(str(login.get("next") or "/"))
    )


def _domain_allowed(email: str) -> bool:
    allowed = settings.google_allowed_domains
    return not allowed or email.rsplit("@", 1)[-1] in allowed


async def sign_in_verified(session: AsyncSession, *, email: str, name: str | None) -> User:
    """The account a Google-verified ``email`` signs in to, created if it may be. No commit."""
    email = normalize_email(email)
    user: User | None = await session.scalar(select(User).where(User.email == email))
    if user is not None:
        if not _domain_allowed(email) and not user.is_platform_admin:
            raise SignInFlowError(ERR_DOMAIN)
        await oidc_accounts.reclaim_if_unclaimed(session, user)
    else:
        if not _domain_allowed(email):
            raise SignInFlowError(ERR_DOMAIN)
        user = await _create_account(session, email=email, name=name)
    # Google proved the address: the verification a hosted sign-up waits for,
    # and with it the PLATFORM_ADMIN_EMAILS grant that verification carries.
    email_verification_service.mark_verified(user)
    email_verification_service.grant_listed_platform_admin(user)
    await tenancy.policy().after_verified_sign_in(session, user)
    await session.flush()
    return user


async def _create_account(session: AsyncSession, *, email: str, name: str | None) -> User:
    hosted = tenancy.multi_tenant()
    if not hosted:
        await auth_service.acquire_owner_set_xact_lock(session, DEFAULT_ORG_ID)
    is_first_user = not hosted and not await auth_service.has_any_users(session)
    if not await auth_service.is_registration_allowed(session, is_first_user=is_first_user):
        raise SignInFlowError(ERR_SIGNUP_CLOSED)
    user = User(
        email=email,
        name=(name or "").strip() or None,
        password_hash=await oidc_flow.unusable_password_hash(),
        is_platform_admin=is_first_user,
    )
    session.add(user)
    await session.flush()
    if not hosted:
        # As the password sign-up does: the first account owns the instance.
        auth_service.add_organization_membership(
            session,
            user,
            organization_id=DEFAULT_ORG_ID,
            org_role=OrganizationRole.owner if is_first_user else OrganizationRole.member,
        )
    return user


async def home_org_id(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    org_id: uuid.UUID | None = await session.scalar(
        select(OrganizationMember.organization_id)
        .where(OrganizationMember.user_id == user_id)
        .order_by(OrganizationMember.created_at)
        .limit(1)
    )
    return org_id
