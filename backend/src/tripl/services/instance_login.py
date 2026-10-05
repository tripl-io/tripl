"""Signing in through an instance-wide OpenID Connect provider.

Not an organization's SSO (Enterprise): one OAuth client for the whole
instance, configured by the operator in the environment, behind one button on
the sign-in page. Two providers use it: Google (``google_login_service``) and
any other OpenID Connect provider (``oidc_login_service``: Okta, Microsoft
Entra ID, Keycloak, ...). Each is a :class:`Provider`; this module is the flow
they share. The OIDC mechanics — discovery, PKCE, code exchange, JWKS and
id_token checks — are ``tripl.services.oidc``'s, so there is one
implementation of the protocol.

The login state (``state``, ``nonce``, PKCE verifier, where to land, expiry)
travels in an encrypted, http-only cookie bound to the browser that started
the sign-in. The callback checks the returned ``state`` against it, re-checks
where it lands, and the cookie is cleared on the way out.

Who signs in, given an address the provider verified:

* an account with that address signs in. One nobody ever proved the address of
  (a hosted sign-up still unverified) is first taken over clean
  (``oidc_accounts.reclaim_if_unclaimed``);
* otherwise an account is created, when the provider creates accounts, sign-up
  is open and the address is in the provider's allowed domains (when it lists
  any). Self-hosted it joins the default organization as the password sign-up
  would; hosted the tenancy policy gives it an organization of its own.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import tenancy
from tripl.auth_utils import normalize_email
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

STATE_TTL = timedelta(minutes=10)
#: Sign-up is closed (or the provider creates no accounts) and no account has the address.
ERR_SIGNUP_CLOSED = "signup_closed"

_TOKEN_BYTES = 32
_VERIFIER_BYTES = 64


@dataclass(frozen=True)
class Provider:
    """One instance-wide provider, as the operator configured it."""

    #: ``google`` or ``oidc``.
    key: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: str
    #: Where the provider sends the browser back, under ``APP_BASE_URL``.
    callback_path: str
    audit_action: str
    #: Lower-case domains an address must be at; empty: any.
    allowed_domains: tuple[str, ...] = ()
    #: Whether a first sign-in may create the account.
    create_accounts: bool = True
    #: Whether a platform admin signs in from outside ``allowed_domains``.
    admins_bypass_domains: bool = False
    #: More parameters for the authorization request (Google: the account chooser).
    extra_params: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id and self.client_secret)

    def redirect_uri(self, app_base_url: str) -> str:
        return f"{app_base_url.rstrip('/')}{self.callback_path}"


#: Discovery, code exchange, JWKS and id_token checks: blocking, and the seam tests replace.
Authenticate = Callable[..., IdTokenClaims]


@dataclass(frozen=True)
class StartedLogin:
    authorization_url: str
    #: The encrypted login state, for the http-only cookie.
    cookie: str


@dataclass(frozen=True)
class SignedIn:
    user: User
    session_token: str
    next_path: str


async def start(provider: Provider, *, next_path: str | None, app_base_url: str) -> StartedLogin:
    """Build the provider's sign-in URL and the cookie that carries the login state."""
    if not provider.configured:
        raise SignInFlowError(ERR_UNAVAILABLE)
    try:
        discovery = await asyncio.to_thread(idp_http.fetch_discovery, provider.issuer)
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
            "client_id": provider.client_id,
            "redirect_uri": provider.redirect_uri(app_base_url),
            "scope": provider.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": oidc_flow.pkce_challenge(verifier),
            "code_challenge_method": "S256",
            **provider.extra_params,
        }
    )
    return StartedLogin(
        authorization_url=f"{discovery.authorization_endpoint}?{query}", cookie=cookie
    )


def login_state(cookie: str | None, state: str | None) -> dict[str, Any]:
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


def authenticate(
    provider: Provider, *, code: str, redirect_to: str, verifier: str, nonce: str
) -> IdTokenClaims:
    """Discovery, code exchange, JWKS and id_token checks. Blocking."""
    discovery = idp_http.fetch_discovery(provider.issuer)
    id_token = id_tokens.exchange_code(
        discovery,
        client_id=provider.client_id,
        client_secret=provider.client_secret,
        code=code,
        redirect_uri=redirect_to,
        code_verifier=verifier,
    )
    keys = idp_http.fetch_jwks(discovery)
    return id_tokens.verify_id_token(
        id_token,
        keys=keys,
        issuer=provider.issuer,
        client_id=provider.client_id,
        nonce=nonce,
    )


async def callback(
    provider: Provider,
    session: AsyncSession,
    *,
    code: str | None,
    state: str | None,
    idp_error: str | None,
    cookie: str | None,
    app_base_url: str,
    authenticate: Authenticate,
) -> SignedIn:
    """Finish the sign-in; see the module docstring for who it signs in. Commits."""
    if not provider.configured:
        raise SignInFlowError(ERR_UNAVAILABLE)
    login = login_state(cookie, state)
    if idp_error:
        raise SignInFlowError(ERR_DENIED)
    if not code:
        raise SignInFlowError(ERR_IDP)
    try:
        claims = await asyncio.to_thread(
            authenticate,
            code=code,
            redirect_to=provider.redirect_uri(app_base_url),
            verifier=str(login.get("verifier", "")),
            nonce=str(login.get("nonce", "")),
        )
    except IdpError as exc:
        raise SignInFlowError(oidc_flow.flow_code(exc)) from None
    if not claims.email_verified:
        raise SignInFlowError(ERR_EMAIL_UNVERIFIED)
    user = await sign_in_verified(provider, session, email=claims.email, name=claims.name)
    token = await auth_service.create_session_for_user(session, user.id)
    await audit_service.record(
        session,
        user=user,
        action=provider.audit_action,
        target_type="user",
        target_id=user.id,
        target_name=user.email,
        payload={"issuer": provider.issuer},
        organization_id=await home_org_id(session, user.id),
    )
    await session.refresh(user)
    # Re-checked: without an encryption key the cookie is only encoded.
    return SignedIn(
        user=user, session_token=token, next_path=safe_next(str(login.get("next") or "/"))
    )


def _domain_allowed(provider: Provider, email: str) -> bool:
    allowed = provider.allowed_domains
    return not allowed or email.rsplit("@", 1)[-1] in allowed


async def sign_in_verified(
    provider: Provider, session: AsyncSession, *, email: str, name: str | None
) -> User:
    """The account a provider-verified ``email`` signs in to, created if it may be. No commit."""
    email = normalize_email(email)
    user: User | None = await session.scalar(select(User).where(User.email == email))
    if user is not None:
        bypass = provider.admins_bypass_domains and user.is_platform_admin
        if not _domain_allowed(provider, email) and not bypass:
            raise SignInFlowError(ERR_DOMAIN)
        await oidc_accounts.reclaim_if_unclaimed(session, user)
    else:
        if not _domain_allowed(provider, email):
            raise SignInFlowError(ERR_DOMAIN)
        if not provider.create_accounts:
            raise SignInFlowError(ERR_SIGNUP_CLOSED)
        user = await _create_account(session, email=email, name=name)
    # The provider proved the address: the verification a hosted sign-up waits
    # for, and with it the PLATFORM_ADMIN_EMAILS grant that verification carries.
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
