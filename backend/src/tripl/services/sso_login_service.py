"""Signing in through an organization's identity provider (F20, GH #273).

The flow (``api.v1.auth_sso``), OIDC or SAML 2.0 by the organization's
``protocol``:

1. ``start`` stores a single-use login state (``state`` digest, where to land;
   OIDC: nonce and PKCE verifier; SAML: the AuthnRequest ID) and sends the
   browser to the provider (SAML: the HTTP-Redirect binding, ``RelayState``
   carrying the state).
2. ``callback`` (OIDC) consumes that state, exchanges the code and verifies the
   id_token (``sso_tokens``); the SAML ACS (``saml_login_service``) consumes it
   and verifies the posted response (``saml_response``). Both then hand the
   verified ``(issuer, subject, email)`` to :func:`complete_sign_in`, which
   requires the email in one of the organization's DNS-verified domains and:

   * the identity ``(issuer, sub, organization)`` is linked: that user signs in;
   * no account has the address: one is created (verified, never a platform
     admin), joins the organization as ``member``, is linked and signs in;
   * an account has the address: nothing signs in yet. A link ticket is issued
     and the person confirms in the app (``confirm_link``) — an existing
     account is linked only for a DNS-verified domain AND after confirmation
     (critique #27). The confirmation must come from the ACCOUNT's owner, not
     merely from whoever signed in at the provider: the browser holds a live
     session of that account (signed in with its password or another way
     first). The one exception is an account nobody ever proved the address
     of (a hosted sign-up still unverified): the provider's verified address is
     the first proof, so the account is taken over clean — its password,
     sessions, keys and pending tokens are all dropped — never shared with
     whoever registered it;
   * an account removed from the organization is not brought back by signing
     in again (``org_sso_service.membership_blocked``) until it accepts a new
     invitation.

Every session this issues is ``auth_method='sso'`` for the organization. Errors
are :class:`SsoFlowError` with a short code the browser is sent back with
(``/auth?sso_error=<code>``); the provider's text never reaches it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlencode

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.auth_utils import hash_password, hash_session_token, normalize_email
from tripl.crypto import decrypt_value, encrypt_value
from tripl.models.api_key import ApiKey
from tripl.models.domain_enums import OrganizationRole, OrganizationStatus
from tripl.models.email_verification_token import EmailVerificationToken
from tripl.models.org_sso import (
    PROTOCOL_OIDC,
    PROTOCOL_SAML,
    OrgSsoConfig,
    OrgSsoDomain,
    SsoLinkTicket,
    SsoLoginState,
    UserSsoIdentity,
)
from tripl.models.organization import Organization, OrganizationMember
from tripl.models.password_reset_token import PasswordResetToken
from tripl.models.user import User
from tripl.models.user_session import UserSession
from tripl.services import (
    audit_service,
    auth_service,
    email_verification_service,
    org_sso_service,
    saml_response,
    saml_xml,
    sso_http,
    sso_tokens,
)
from tripl.services.sso_http import IdpError
from tripl.services.sso_tokens import IdTokenClaims

logger = logging.getLogger(__name__)

STATE_TTL = timedelta(minutes=10)
LINK_TICKET_TTL = timedelta(minutes=10)
_TOKEN_BYTES = 32
_VERIFIER_BYTES = 64
_MAX_NEXT = 2048

# The codes the browser can come back with. Stable: the SPA maps them to text.
ERR_UNAVAILABLE = "sso_unavailable"
ERR_STATE = "invalid_state"
ERR_IDP = "idp_error"
ERR_DENIED = "idp_denied"
ERR_TOKEN = "invalid_token"
ERR_EMAIL_MISSING = "email_missing"
ERR_EMAIL_UNVERIFIED = "email_not_verified"
ERR_DOMAIN = "email_domain_not_allowed"
ERR_FAILED = "sso_failed"
ERR_REMOVED = "membership_removed"
ERR_RATE_LIMITED = "rate_limited"
ERR_SAML = saml_response.ERR_INVALID
ERR_SAML_SIGNATURE = saml_response.ERR_SIGNATURE
ERR_SAML_UNSOLICITED = saml_response.ERR_UNSOLICITED
ERR_SAML_REPLAY = "saml_replay"
ERR_ENCRYPTED = saml_response.ERR_ENCRYPTED
# Not sent to ``/auth``: answers of the link routes.
ERR_LINK = "invalid_link"
ERR_LINK_SIGN_IN = "link_sign_in_required"


class SsoFlowError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _flow_code(error: IdpError) -> str:
    if error.code.startswith("id_token_"):
        return ERR_TOKEN
    if error.code == "email_missing":
        return ERR_EMAIL_MISSING
    return ERR_IDP


def safe_next(value: str | None) -> str:
    """``value`` when it is a same-origin path, else ``/``.

    A path starts with one ``/`` (never ``//`` or ``/\\``, which browsers read
    as another host) and carries no backslash or control character.
    """
    if not value or len(value) > _MAX_NEXT:
        return "/"
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "/"
    return value


def _digest(raw: str) -> str:
    return hash_session_token(raw)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ── lookups ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SsoOrg:
    id: uuid.UUID
    slug: str
    name: str
    config: OrgSsoConfig


async def enabled_org(session: AsyncSession, org_slug: str) -> SsoOrg:
    """An active organization with SSO enabled, else ``sso_unavailable``."""
    row = (
        await session.execute(
            select(Organization, OrgSsoConfig)
            .join(OrgSsoConfig, OrgSsoConfig.organization_id == Organization.id)
            .where(
                Organization.slug == org_slug,
                Organization.status == OrganizationStatus.active.value,
                OrgSsoConfig.enabled.is_(True),
            )
        )
    ).first()
    if row is None:
        raise SsoFlowError(ERR_UNAVAILABLE)
    org, config = cast(tuple[Organization, OrgSsoConfig], tuple(row))
    return SsoOrg(id=org.id, slug=org.slug, name=org.name, config=config)


async def discover(session: AsyncSession, email: str) -> list[tuple[str, str]]:
    """``(slug, name)`` of the SSO-enabled organizations owning ``email``'s verified domain."""
    address = normalize_email(email)
    if "@" not in address:
        return []
    domain = address.rsplit("@", 1)[1].rstrip(".")
    rows = await session.execute(
        select(Organization.slug, Organization.name)
        .join(OrgSsoDomain, OrgSsoDomain.organization_id == Organization.id)
        .join(OrgSsoConfig, OrgSsoConfig.organization_id == Organization.id)
        .where(
            OrgSsoDomain.domain == domain,
            OrgSsoDomain.verified_at.is_not(None),
            OrgSsoConfig.enabled.is_(True),
            Organization.status == OrganizationStatus.active.value,
        )
        .order_by(Organization.name)
    )
    return [(slug, name) for slug, name in rows.all()]


# ── start ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StartedLogin:
    authorization_url: str
    #: The raw ``state``; the route also binds it to the browser in a cookie.
    state: str
    protocol: str = PROTOCOL_OIDC


async def _purge_states(session: AsyncSession, now: datetime) -> None:
    """Housekeeping: states nobody came back for."""
    await session.execute(
        delete(SsoLoginState)
        .where(SsoLoginState.expires_at < now - STATE_TTL)
        .execution_options(synchronize_session=False)
    )


async def _start_saml(
    session: AsyncSession, org: SsoOrg, *, next_path: str | None, app_base_url: str
) -> StartedLogin:
    """An unsigned AuthnRequest on the HTTP-Redirect binding; ``RelayState`` is the state."""
    config = org.config
    if not config.saml_idp_sso_url or not saml_xml.check_https_url(config.saml_idp_sso_url):
        raise SsoFlowError(ERR_UNAVAILABLE)
    now = datetime.now(UTC)
    await _purge_states(session, now)
    state = secrets.token_urlsafe(_TOKEN_BYTES)
    request_id = saml_xml.new_request_id()
    session.add(
        SsoLoginState(
            organization_id=org.id,
            state_hash=_digest(state),
            nonce="",
            code_verifier="",
            request_id=request_id,
            next_path=safe_next(next_path),
            expires_at=now + STATE_TTL,
        )
    )
    await session.commit()
    request_xml = saml_xml.authn_request(
        request_id=request_id,
        issue_instant=now,
        destination=config.saml_idp_sso_url,
        acs_url=org_sso_service.saml_acs_url(app_base_url, org.slug),
        sp_entity_id=org_sso_service.saml_sp_entity_id(app_base_url, org.slug),
        name_id_format=config.saml_name_id_format,
    )
    return StartedLogin(
        authorization_url=saml_xml.authn_request_url(config.saml_idp_sso_url, request_xml, state),
        state=state,
        protocol=PROTOCOL_SAML,
    )


async def start(
    session: AsyncSession, *, org_slug: str, next_path: str | None, app_base_url: str
) -> StartedLogin:
    """Store a login state and build the provider's sign-in URL. Commits."""
    org = await enabled_org(session, org_slug)
    if org.config.protocol == PROTOCOL_SAML:
        return await _start_saml(session, org, next_path=next_path, app_base_url=app_base_url)
    if not org.config.issuer or not org.config.client_id:
        raise SsoFlowError(ERR_UNAVAILABLE)
    try:
        discovery = await asyncio.to_thread(sso_http.fetch_discovery, org.config.issuer)
    except IdpError as exc:
        logger.warning("SSO start for %s failed: %s", org.slug, exc.code)
        raise SsoFlowError(ERR_IDP) from None

    now = datetime.now(UTC)
    await _purge_states(session, now)
    state = secrets.token_urlsafe(_TOKEN_BYTES)
    nonce = secrets.token_urlsafe(_TOKEN_BYTES)
    verifier = secrets.token_urlsafe(_VERIFIER_BYTES)
    session.add(
        SsoLoginState(
            organization_id=org.id,
            state_hash=_digest(state),
            nonce=nonce,
            code_verifier=encrypt_value(verifier),
            next_path=safe_next(next_path),
            expires_at=now + STATE_TTL,
        )
    )
    await session.commit()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": org.config.client_id,
            "redirect_uri": org_sso_service.redirect_uri(app_base_url, org.slug),
            "scope": org.config.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    separator = "&" if "?" in discovery.authorization_endpoint else "?"
    return StartedLogin(
        authorization_url=f"{discovery.authorization_endpoint}{separator}{query}", state=state
    )


# ── callback ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SignedIn:
    user: User
    session_token: str
    next_path: str


@dataclass(frozen=True)
class NeedsLink:
    ticket: str


@dataclass(frozen=True)
class VerifiedIdentity:
    """Who the provider vouched for, whatever the protocol.

    ``issuer``: the OIDC issuer or the SAML IdP entity id; ``subject``: the
    ``sub`` claim or the NameID.
    """

    issuer: str
    subject: str
    email: str
    name: str | None


async def consume_state(session: AsyncSession, org: SsoOrg, raw_state: str) -> SsoLoginState:
    """Mark the state used (single use, even when the rest fails). Commits."""
    now = datetime.now(UTC)
    row = cast(
        SsoLoginState | None,
        await session.scalar(
            select(SsoLoginState).where(SsoLoginState.state_hash == _digest(raw_state))
        ),
    )
    if row is None or row.organization_id != org.id:
        raise SsoFlowError(ERR_STATE)
    claimed = await session.execute(
        update(SsoLoginState)
        .where(
            SsoLoginState.id == row.id,
            SsoLoginState.used_at.is_(None),
            SsoLoginState.expires_at > now,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if int(getattr(claimed, "rowcount", 0) or 0) != 1:
        raise SsoFlowError(ERR_STATE)
    return row


def _authenticate(
    config: OrgSsoConfig,
    *,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    nonce: str,
) -> IdTokenClaims:
    """Discovery, code exchange, JWKS and id_token checks. Blocking."""
    issuer, client_id = config.issuer or "", config.client_id or ""
    discovery = sso_http.fetch_discovery(issuer)
    id_token = sso_tokens.exchange_code(
        discovery,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )
    keys = sso_http.fetch_jwks(discovery)
    return sso_tokens.verify_id_token(
        id_token, keys=keys, issuer=issuer, client_id=client_id, nonce=nonce
    )


async def _member_role(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
    role: str | None = await session.scalar(
        select(OrganizationMember.role).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    return None if role is None else str(role)


async def _sign_in(
    session: AsyncSession,
    org: SsoOrg,
    user: User,
    *,
    action: str,
    next_path: str,
    joined: bool | None = None,
    reclaimed: bool | None = None,
) -> SignedIn:
    """An ``sso`` session for ``org`` and its audit row (in the organization). Commits."""
    token = await auth_service.create_session_for_user(session, user.id, sso_organization_id=org.id)
    await audit_service.record(
        session,
        user=user,
        action=action,
        target_type="user",
        target_id=user.id,
        target_name=user.email,
        payload={
            "protocol": org.config.protocol,
            "issuer": org_sso_service.idp_issuer(org.config),
            **({} if joined is None else {"joined_organization": joined}),
            **({} if reclaimed is None else {"reclaimed_unverified_account": reclaimed}),
        },
        organization_id=org.id,
    )
    await session.refresh(user)
    return SignedIn(user=user, session_token=token, next_path=next_path)


async def _linked_user(session: AsyncSession, org: SsoOrg, claims: VerifiedIdentity) -> User | None:
    """The user the identity is linked to, while they are still a member."""
    identity = cast(
        UserSsoIdentity | None,
        await session.scalar(
            select(UserSsoIdentity).where(
                UserSsoIdentity.issuer == claims.issuer,
                UserSsoIdentity.subject == claims.subject,
                UserSsoIdentity.organization_id == org.id,
            )
        ),
    )
    if identity is None:
        return None
    if await _member_role(session, org.id, identity.user_id) is None:
        # Left the organization some other way: the link goes with it, and
        # this sign-in is treated as a first one.
        await session.delete(identity)
        await session.flush()
        return None
    return await session.get(User, identity.user_id)


async def _unusable_password_hash() -> str:
    """A real scrypt hash of a secret nobody knows.

    The account signs in through its provider (a password reset can still give
    it a password). A real hash, not a marker: ``/auth/login`` then spends the
    same scrypt time on it as on any account or an unknown address, so the
    response time does not tell SSO-only accounts apart.
    """
    return await asyncio.to_thread(hash_password, secrets.token_urlsafe(_TOKEN_BYTES))


async def _provision(session: AsyncSession, org: SsoOrg, claims: VerifiedIdentity) -> User:
    """A new account for ``claims``: verified, a plain member, linked. Flushes."""
    user = User(
        email=claims.email,
        name=claims.name,
        password_hash=await _unusable_password_hash(),
        is_platform_admin=False,
    )
    email_verification_service.mark_verified(user)
    session.add(user)
    await session.flush()
    auth_service.add_organization_membership(
        session, user, organization_id=org.id, org_role=OrganizationRole.member
    )
    session.add(
        UserSsoIdentity(
            user_id=user.id,
            organization_id=org.id,
            issuer=claims.issuer,
            subject=claims.subject,
        )
    )
    await session.flush()
    return user


async def _issue_link_ticket(
    session: AsyncSession, org: SsoOrg, user: User, claims: VerifiedIdentity, next_path: str
) -> NeedsLink:
    now = datetime.now(UTC)
    await session.execute(
        delete(SsoLinkTicket)
        .where(SsoLinkTicket.expires_at < now - LINK_TICKET_TTL)
        .execution_options(synchronize_session=False)
    )
    ticket = secrets.token_urlsafe(_TOKEN_BYTES)
    session.add(
        SsoLinkTicket(
            ticket_hash=_digest(ticket),
            user_id=user.id,
            organization_id=org.id,
            issuer=claims.issuer,
            subject=claims.subject,
            next_path=next_path,
            expires_at=now + LINK_TICKET_TTL,
        )
    )
    await session.commit()
    return NeedsLink(ticket=ticket)


async def callback(
    session: AsyncSession,
    *,
    org_slug: str,
    code: str | None,
    raw_state: str | None,
    idp_error: str | None,
    app_base_url: str,
) -> SignedIn | NeedsLink:
    """Finish an OIDC sign-in; see the module docstring for the outcomes."""
    org = await enabled_org(session, org_slug)
    if org.config.protocol != PROTOCOL_OIDC:
        raise SsoFlowError(ERR_UNAVAILABLE)
    if not raw_state:
        raise SsoFlowError(ERR_STATE)
    state = await consume_state(session, org, raw_state)
    if state.request_id is not None:
        # A SAML sign-in's state: it ends at the ACS, never here.
        raise SsoFlowError(ERR_STATE)
    if idp_error:
        raise SsoFlowError(ERR_DENIED)
    if not code:
        raise SsoFlowError(ERR_IDP)
    try:
        claims = await asyncio.to_thread(
            _authenticate,
            org.config,
            client_secret=org_sso_service.client_secret(org.config),
            code=code,
            redirect_uri=org_sso_service.redirect_uri(app_base_url, org.slug),
            code_verifier=decrypt_value(state.code_verifier),
            nonce=state.nonce,
        )
    except IdpError as exc:
        logger.warning("SSO callback for %s refused: %s", org.slug, exc.code)
        raise SsoFlowError(_flow_code(exc)) from None
    if not claims.email_verified:
        raise SsoFlowError(ERR_EMAIL_UNVERIFIED)
    identity = VerifiedIdentity(
        issuer=claims.issuer, subject=claims.subject, email=claims.email, name=claims.name
    )
    return await complete_sign_in(session, org, identity, next_path=state.next_path)


async def complete_sign_in(
    session: AsyncSession, org: SsoOrg, identity: VerifiedIdentity, *, next_path: str
) -> SignedIn | NeedsLink:
    """What a verified provider sign-in leads to, the same for OIDC and SAML.

    The provider vouched for ``identity.email`` (OIDC: ``email_verified``;
    SAML: the signed assertion). It must be in one of the organization's
    DNS-verified domains. Then the linked user signs in, a new account is
    provisioned (JIT), or an existing account gets a link ticket to confirm;
    see the module docstring.
    """
    email = identity.email
    domain = email.rsplit("@", 1)[-1]
    if "@" not in email or domain not in await org_sso_service.verified_domains(session, org.id):
        raise SsoFlowError(ERR_DOMAIN)

    try:
        linked = await _linked_user(session, org, identity)
        if linked is not None:
            return await _sign_in(
                session, org, linked, action="user.sso_login", next_path=next_path
            )
        existing = cast(User | None, await session.scalar(select(User).where(User.email == email)))
        if existing is None:
            user = await _provision(session, org, identity)
            return await _sign_in(
                session, org, user, action="user.sso_provision", next_path=next_path
            )
        if await _rejoin_blocked(session, org.id, existing.id):
            raise SsoFlowError(ERR_REMOVED)
        return await _issue_link_ticket(session, org, existing, identity, next_path)
    except IntegrityError:
        # A concurrent sign-in created the account or the link first.
        await session.rollback()
        raise SsoFlowError(ERR_FAILED) from None


# ── linking an existing account ─────────────────────────────────────────────


@dataclass(frozen=True)
class LinkPreview:
    email: str
    org_slug: str
    org_name: str
    expires_at: datetime
    #: The browser must be signed in to the account (not just at the provider)
    #: before confirming; see :func:`confirm_link`.
    sign_in_required: bool


def _unclaimed(user: User) -> bool:
    """An account nobody has proved the address of: a hosted sign-up still unverified.

    Self-hosted accounts are verified at creation, and a platform admin is
    never treated as unclaimed.
    """
    return user.email_verified_at is None and not user.is_platform_admin


async def _rejoin_blocked(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Not a member, and removed from the organization since (no new invitation)."""
    if await _member_role(session, org_id, user_id) is not None:
        return False
    return await org_sso_service.membership_blocked(session, org_id, user_id)


async def _reclaim(session: AsyncSession, user: User, now: datetime) -> None:
    """Take over an unclaimed account for the provider-verified person. No commit.

    Whoever registered the address without proving it loses everything they
    held: the password becomes unusable, and every session, API key, password
    reset and verification token of the account goes.
    """
    user.password_hash = await _unusable_password_hash()
    for model in (UserSession, PasswordResetToken, EmailVerificationToken):
        await session.execute(
            delete(model)
            .where(model.user_id == user.id)
            .execution_options(synchronize_session=False)
        )
    await session.execute(
        update(ApiKey)
        .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )


async def reclaim_if_unclaimed(session: AsyncSession, user: User) -> bool:
    """Take ``user`` over clean if nobody ever proved the address (:func:`_unclaimed`).

    For SCIM provisioning (F20), which vouches for an address in one of the
    organization's verified domains the way a provider-verified sign-in does.
    Returns whether it did. No commit.
    """
    if not _unclaimed(user):
        return False
    await _reclaim(session, user, datetime.now(UTC))
    return True


async def _live_ticket(session: AsyncSession, raw_ticket: str) -> SsoLinkTicket:
    row = cast(
        SsoLinkTicket | None,
        await session.scalar(
            select(SsoLinkTicket).where(SsoLinkTicket.ticket_hash == _digest(raw_ticket))
        ),
    )
    if row is None or row.used_at is not None or row.expires_at <= datetime.now(UTC):
        raise SsoFlowError(ERR_LINK)
    return row


async def preview_link(
    session: AsyncSession, raw_ticket: str, *, session_user_id: uuid.UUID | None
) -> LinkPreview:
    """What the ticket links; ``session_user_id`` is the browser's signed-in account."""
    row = await _live_ticket(session, raw_ticket)
    org = await session.get(Organization, row.organization_id)
    user = await session.get(User, row.user_id)
    if org is None or user is None:
        raise SsoFlowError(ERR_LINK)
    return LinkPreview(
        email=user.email,
        org_slug=org.slug,
        org_name=org.name,
        expires_at=row.expires_at,
        sign_in_required=not _unclaimed(user) and session_user_id != user.id,
    )


async def confirm_link(
    session: AsyncSession,
    raw_ticket: str,
    *,
    session_user: User | None,
    session_token_hash: str | None,
) -> tuple[SignedIn, uuid.UUID]:
    """Link the ticket's identity to its account, join the organization, sign in.

    Returns the sign-in and the organization id. The ticket is single use; the
    organization must still have SSO enabled.

    Proof of the ACCOUNT is required, not only of the provider sign-in that
    minted the ticket (whoever runs a verified domain's provider can put any
    of its addresses in an id_token): ``session_user`` — the browser's live
    session — must be the ticket's account, else ``link_sign_in_required``
    and the ticket stays usable. That session is replaced by the SSO one. An
    unclaimed account (:func:`_unclaimed`) needs no session and is taken over
    clean (:func:`_reclaim`). An account removed from the organization is
    refused (``membership_removed``).
    """
    row = await _live_ticket(session, raw_ticket)
    org_slug = await session.scalar(
        select(Organization.slug).where(Organization.id == row.organization_id)
    )
    if org_slug is None:
        raise SsoFlowError(ERR_LINK)
    try:
        org = await enabled_org(session, org_slug)
    except SsoFlowError:
        raise SsoFlowError(ERR_UNAVAILABLE) from None
    if org_sso_service.idp_issuer(org.config) != row.issuer:
        # The provider changed after the ticket was issued.
        raise SsoFlowError(ERR_LINK)
    user = await session.get(User, row.user_id)
    if user is None:
        raise SsoFlowError(ERR_LINK)
    unclaimed = _unclaimed(user)
    if not unclaimed and (session_user is None or session_user.id != user.id):
        raise SsoFlowError(ERR_LINK_SIGN_IN)
    if await _rejoin_blocked(session, org.id, user.id):
        raise SsoFlowError(ERR_REMOVED)
    now = datetime.now(UTC)
    claimed = await session.execute(
        update(SsoLinkTicket)
        .where(SsoLinkTicket.id == row.id, SsoLinkTicket.used_at.is_(None))
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    if int(getattr(claimed, "rowcount", 0) or 0) != 1:
        await session.rollback()
        raise SsoFlowError(ERR_LINK)
    if unclaimed:
        await _reclaim(session, user, now)
    elif session_token_hash is not None:
        # The session that proved the account is replaced by the SSO one.
        await session.execute(
            delete(UserSession)
            .where(
                UserSession.user_id == user.id,
                UserSession.session_token_hash == session_token_hash,
            )
            .execution_options(synchronize_session=False)
        )
    try:
        taken = await session.scalar(
            select(UserSsoIdentity.user_id).where(
                UserSsoIdentity.issuer == row.issuer,
                UserSsoIdentity.subject == row.subject,
                UserSsoIdentity.organization_id == org.id,
            )
        )
        if taken is not None and taken != user.id:
            raise SsoFlowError(ERR_FAILED)
        if taken is None:
            session.add(
                UserSsoIdentity(
                    user_id=user.id,
                    organization_id=org.id,
                    issuer=row.issuer,
                    subject=row.subject,
                )
            )
        joined = await _member_role(session, org.id, user.id) is None
        if joined:
            auth_service.add_organization_membership(
                session, user, organization_id=org.id, org_role=OrganizationRole.member
            )
        # The provider vouched for the address; that grants nothing further.
        email_verification_service.mark_verified(user, now=now)
        await session.flush()
        signed_in = await _sign_in(
            session,
            org,
            user,
            action="user.sso_link",
            next_path=row.next_path,
            joined=joined,
            reclaimed=unclaimed,
        )
    except IntegrityError:
        await session.rollback()
        raise SsoFlowError(ERR_FAILED) from None
    return signed_in, org.id
