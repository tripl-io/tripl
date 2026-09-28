"""An organization's single sign-on configuration and domains (F20, GH #273).

Only an owner of the organization changes any of it (``api.v1.org_sso``). The
rules kept here:

* the client secret is encrypted with the operator key and never read back;
* the issuer is an https URL; on a hosted instance its host must be public, at
  save time here and again before every request (``sso_http``);
* enabling SSO needs at least one DNS-verified domain; requiring it needs SSO
  enabled; turning "required" on revokes the organization's API keys that were
  not minted from an SSO session of it, owners' included (owners keep password
  SIGN-IN as the break-glass, not keys);
* a domain is proven by a TXT record ``_tripl-verification.<domain>`` holding
  ``tripl-verification=<token>``; a domain verified by one organization is a
  409 for every other.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

import dns.exception
import dns.resolver
from fastapi import HTTPException, status
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.alerting_validation import reject_private_host
from tripl.config import DEPLOYMENT_HOSTED, settings
from tripl.crypto import decrypt_value, encrypt_value
from tripl.models.api_key import ApiKey
from tripl.models.org_sso import (
    DEFAULT_SSO_SCOPES,
    OrgSsoConfig,
    OrgSsoDomain,
    SsoLinkTicket,
    SsoLoginState,
    SsoMembershipBlock,
    UserSsoIdentity,
)
from tripl.schemas.org_sso import (
    VERIFICATION_LABEL,
    VERIFICATION_PREFIX,
    OrgSsoConfigResponse,
    OrgSsoConfigUpdate,
    OrgSsoDomainResponse,
)

logger = logging.getLogger(__name__)

DNS_TIMEOUT_SECONDS = 5.0
_TOKEN_BYTES = 24

DOMAIN_NOT_FOUND = "Domain not found"
DOMAIN_TAKEN = "This domain is verified by another organization"
DOMAIN_EXISTS = "This domain is already added"
ENABLE_NEEDS_DOMAIN = "Verify at least one domain before enabling single sign-on"
REQUIRED_NEEDS_ENABLED = "Single sign-on must be enabled to require it"
SECRET_REQUIRED = "A client secret is required"
LAST_DOMAIN = "Disable single sign-on before removing its last verified domain"


def login_path(org_slug: str) -> str:
    """Where a browser starts signing in to ``org_slug`` (a same-origin path)."""
    return f"/api/v1/auth/sso/{org_slug}/start"


def redirect_uri(app_base_url: str, org_slug: str) -> str:
    """The callback URL registered at the identity provider."""
    return f"{app_base_url.rstrip('/')}/api/v1/auth/sso/{org_slug}/callback"


async def get_config(session: AsyncSession, org_id: uuid.UUID) -> OrgSsoConfig | None:
    config: OrgSsoConfig | None = await session.scalar(
        select(OrgSsoConfig).where(OrgSsoConfig.organization_id == org_id)
    )
    return config


def client_secret(config: OrgSsoConfig) -> str:
    return decrypt_value(config.client_secret_encrypted)


def config_response(
    config: OrgSsoConfig | None, *, org_slug: str, app_base_url: str
) -> OrgSsoConfigResponse:
    return OrgSsoConfigResponse(
        configured=config is not None,
        issuer="" if config is None else config.issuer,
        client_id="" if config is None else config.client_id,
        client_secret_configured=config is not None and bool(config.client_secret_encrypted),
        scopes=DEFAULT_SSO_SCOPES if config is None else config.scopes,
        enabled=config is not None and config.enabled,
        sso_required=config is not None and config.enabled and config.sso_required,
        redirect_uri=redirect_uri(app_base_url, org_slug),
        login_url=login_path(org_slug),
    )


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def check_issuer(issuer: str) -> None:
    """422 unless ``issuer`` is an https URL (with a public host, hosted)."""
    parsed = urlparse(issuer)
    if parsed.scheme != "https" or not parsed.hostname:
        raise _unprocessable("The issuer must be an https URL")
    if parsed.query or parsed.fragment:
        raise _unprocessable("The issuer must not carry a query or fragment")
    if settings.deployment_mode == DEPLOYMENT_HOSTED:
        try:
            await asyncio.to_thread(reject_private_host, parsed.hostname, field="Issuer")
        except ValueError as exc:
            raise _unprocessable(str(exc)) from None


async def has_verified_domain(session: AsyncSession, org_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(OrgSsoDomain.id)
        .where(OrgSsoDomain.organization_id == org_id, OrgSsoDomain.verified_at.is_not(None))
        .limit(1)
    )
    return found is not None


async def revoke_non_sso_keys(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Revoke the organization's live keys not minted from its SSO session. No commit.

    Owners' keys included: the owner break-glass is a password SIGN-IN (to fix
    a broken provider), not a long-lived credential that outlives the switch.
    """
    statement = (
        update(ApiKey)
        .where(
            ApiKey.organization_id == org_id,
            ApiKey.revoked_at.is_(None),
            (ApiKey.created_with_sso_org_id.is_(None)) | (ApiKey.created_with_sso_org_id != org_id),
        )
        .values(revoked_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(statement)
    return int(getattr(result, "rowcount", 0) or 0)


@dataclass(frozen=True)
class SavedConfig:
    config: OrgSsoConfig
    changed: list[str]
    revoked_api_keys: int


async def save_config(
    session: AsyncSession, org_id: uuid.UUID, data: OrgSsoConfigUpdate
) -> SavedConfig:
    """Create or replace the configuration. Flushes, does not commit.

    ``changed`` names the fields that changed (``client_secret`` included when a
    new one was given; never its value).
    """
    await check_issuer(data.issuer)
    config = await get_config(session, org_id)
    if data.client_secret is None and (config is None or not config.client_secret_encrypted):
        raise _unprocessable(SECRET_REQUIRED)
    if data.sso_required and not data.enabled:
        raise _unprocessable(REQUIRED_NEEDS_ENABLED)
    if data.enabled and not await has_verified_domain(session, org_id):
        raise _conflict(ENABLE_NEEDS_DOMAIN)

    was_required = config is not None and config.enabled and config.sso_required
    if config is None:
        config = OrgSsoConfig(organization_id=org_id, issuer=data.issuer, client_id=data.client_id)
        session.add(config)
        changed = ["issuer", "client_id", "scopes", "enabled", "sso_required"]
    else:
        changed = [
            name
            for name in ("issuer", "client_id", "scopes", "enabled", "sso_required")
            if getattr(config, name) != getattr(data, name)
        ]
    config.issuer = data.issuer
    config.client_id = data.client_id
    config.scopes = data.scopes
    config.enabled = data.enabled
    config.sso_required = data.sso_required
    if data.client_secret is not None:
        config.client_secret_encrypted = encrypt_value(data.client_secret)
        changed.append("client_secret")
    revoked = 0
    if data.enabled and data.sso_required and not was_required:
        revoked = await revoke_non_sso_keys(session, org_id)
    await session.flush()
    return SavedConfig(config=config, changed=changed, revoked_api_keys=revoked)


async def sso_required(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Whether the organization requires its members to sign in through SSO."""
    found = await session.scalar(
        select(OrgSsoConfig.id).where(
            OrgSsoConfig.organization_id == org_id,
            OrgSsoConfig.enabled.is_(True),
            OrgSsoConfig.sso_required.is_(True),
        )
    )
    return found is not None


# ── domains ─────────────────────────────────────────────────────────────────


def txt_record_name(domain: str) -> str:
    return f"{VERIFICATION_LABEL}.{domain}"


def txt_record_value(token: str) -> str:
    return f"{VERIFICATION_PREFIX}{token}"


def domain_response(row: OrgSsoDomain) -> OrgSsoDomainResponse:
    return OrgSsoDomainResponse(
        id=row.id,
        domain=row.domain,
        verified=row.verified_at is not None,
        verified_at=row.verified_at,
        txt_record_name=txt_record_name(row.domain),
        txt_record_value=txt_record_value(row.verification_token),
        created_at=row.created_at,
    )


async def list_domains(session: AsyncSession, org_id: uuid.UUID) -> list[OrgSsoDomain]:
    rows = await session.scalars(
        select(OrgSsoDomain)
        .where(OrgSsoDomain.organization_id == org_id)
        .order_by(OrgSsoDomain.domain)
    )
    return list(rows.all())


async def _verified_elsewhere(session: AsyncSession, org_id: uuid.UUID, domain: str) -> bool:
    found = await session.scalar(
        select(OrgSsoDomain.id).where(
            OrgSsoDomain.domain == domain,
            OrgSsoDomain.verified_at.is_not(None),
            OrgSsoDomain.organization_id != org_id,
        )
    )
    return found is not None


async def add_domain(session: AsyncSession, org_id: uuid.UUID, domain: str) -> OrgSsoDomain:
    """Claim ``domain`` (normalized by the schema), unverified. Flushes."""
    if await _verified_elsewhere(session, org_id, domain):
        raise _conflict(DOMAIN_TAKEN)
    existing = await session.scalar(
        select(OrgSsoDomain.id).where(
            OrgSsoDomain.organization_id == org_id, OrgSsoDomain.domain == domain
        )
    )
    if existing is not None:
        raise _conflict(DOMAIN_EXISTS)
    row = OrgSsoDomain(
        organization_id=org_id,
        domain=domain,
        verification_token=secrets.token_urlsafe(_TOKEN_BYTES),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise _conflict(DOMAIN_EXISTS) from None
    await session.refresh(row)
    return row


async def get_domain(
    session: AsyncSession, org_id: uuid.UUID, domain_id: uuid.UUID
) -> OrgSsoDomain:
    row = await session.get(OrgSsoDomain, domain_id)
    if row is None or row.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=DOMAIN_NOT_FOUND)
    return row


async def remove_domain(session: AsyncSession, org_id: uuid.UUID, row: OrgSsoDomain) -> None:
    """Delete ``row``; 409 when it is the last verified domain of an enabled SSO."""
    if row.verified_at is not None:
        config = await get_config(session, org_id)
        if config is not None and config.enabled:
            others = await session.scalar(
                select(OrgSsoDomain.id)
                .where(
                    OrgSsoDomain.organization_id == org_id,
                    OrgSsoDomain.verified_at.is_not(None),
                    OrgSsoDomain.id != row.id,
                )
                .limit(1)
            )
            if others is None:
                raise _conflict(LAST_DOMAIN)
    await session.delete(row)
    await session.flush()


def _lookup_txt(name: str) -> list[str]:
    """Every TXT string at ``name``; empty when there are none. Blocking.

    The seam tests replace: the only place that touches DNS.
    """
    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT_SECONDS
    resolver.lifetime = DNS_TIMEOUT_SECONDS
    try:
        answer = resolver.resolve(name, "TXT")
    except dns.exception.DNSException:
        return []
    values: list[str] = []
    for rdata in answer:
        strings = getattr(rdata, "strings", ())
        values.append(b"".join(strings).decode("utf-8", errors="replace"))
    return values


async def verify_domain(session: AsyncSession, org_id: uuid.UUID, row: OrgSsoDomain) -> bool:
    """Look the TXT record up; mark ``row`` verified when it holds the token.

    Returns whether it is verified now. 409 when another organization verified
    the domain first. Flushes.
    """
    if row.verified_at is not None:
        return True
    if await _verified_elsewhere(session, org_id, row.domain):
        raise _conflict(DOMAIN_TAKEN)
    expected = txt_record_value(row.verification_token)
    values = await asyncio.to_thread(_lookup_txt, txt_record_name(row.domain))
    # Bytes: ``compare_digest`` raises TypeError on a non-ASCII ``str``, and a
    # TXT value is anything its zone holds.
    expected_bytes = expected.encode("utf-8")
    if not any(
        secrets.compare_digest(value.strip().encode("utf-8"), expected_bytes) for value in values
    ):
        return False
    row.verified_at = datetime.now(UTC)
    try:
        await session.flush()
    except IntegrityError:
        # Another organization verified it between the check and this write.
        await session.rollback()
        raise _conflict(DOMAIN_TAKEN) from None
    return True


async def verified_domains(session: AsyncSession, org_id: uuid.UUID) -> set[str]:
    rows = await session.scalars(
        select(OrgSsoDomain.domain).where(
            OrgSsoDomain.organization_id == org_id, OrgSsoDomain.verified_at.is_not(None)
        )
    )
    return set(rows.all())


# ── membership and organization lifecycle ──────────────────────────────────


async def drop_identities(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> int:
    """Member removal: forget ``user_id``'s IdP identities here and block SSO rejoin. No commit.

    Without the block, the removed person would sign in through the provider,
    get a link ticket for their account and confirm their way back in. The
    block holds until they accept a new invitation (:func:`lift_membership_block`).
    Returns how many identities went.
    """
    result = await session.execute(
        delete(UserSsoIdentity)
        .where(UserSsoIdentity.organization_id == org_id, UserSsoIdentity.user_id == user_id)
        .execution_options(synchronize_session=False)
    )
    blocked = await session.scalar(
        select(SsoMembershipBlock.id).where(
            SsoMembershipBlock.organization_id == org_id, SsoMembershipBlock.user_id == user_id
        )
    )
    if blocked is None:
        session.add(SsoMembershipBlock(organization_id=org_id, user_id=user_id))
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)


async def membership_blocked(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Whether ``user_id`` was removed from the organization and not invited back."""
    found = await session.scalar(
        select(SsoMembershipBlock.id).where(
            SsoMembershipBlock.organization_id == org_id, SsoMembershipBlock.user_id == user_id
        )
    )
    return found is not None


async def lift_membership_block(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """An admin let ``user_id`` back in (an accepted invitation). No commit."""
    await session.execute(
        delete(SsoMembershipBlock)
        .where(SsoMembershipBlock.organization_id == org_id, SsoMembershipBlock.user_id == user_id)
        .execution_options(synchronize_session=False)
    )


async def delete_org_sso(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Every SSO row of a purged organization (they would cascade; spelled out)."""
    for model in (
        SsoLinkTicket,
        SsoLoginState,
        SsoMembershipBlock,
        UserSsoIdentity,
        OrgSsoDomain,
        OrgSsoConfig,
    ):
        await session.execute(
            delete(model)
            .where(model.organization_id == org_id)
            .execution_options(synchronize_session=False)
        )
