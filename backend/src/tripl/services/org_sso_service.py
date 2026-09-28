"""An organization's single sign-on configuration and domains (F20, GH #273).

Only an owner of the organization changes any of it (``api.v1.org_sso``). The
rules kept here:

* the provider is OIDC or SAML 2.0 (``protocol``); the other protocol's
  settings are kept as saved when a save omits them;
* the client secret is encrypted with the operator key and never read back;
* the issuer is an https URL; on a hosted instance its host must be public, at
  save time here and again before every request (``sso_http``);
* SAML: the IdP's SSO URL is https (tripl never calls it: the browser is sent
  there), its certificates parse and none has expired — checked when a value
  is new (or its protocol is being switched on), not when a save echoes back
  what is stored;
* a new trust anchor (protocol switched, SAML entity id changed, or no saved
  SAML certificate kept) drops the org's linked identities and link tickets
  of the old provider: members confirm their link again;
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
from cryptography import x509
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
    NAMEID_EMAIL,
    PROTOCOL_SAML,
    SAML_ISSUER_PREFIX,
    OrgSsoConfig,
    OrgSsoDomain,
    SamlAssertionId,
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
    SamlCertificateInfo,
)
from tripl.services import saml_xml

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
SAML_SSO_URL_HTTPS = "The identity provider's sign-in URL must be an https URL"
SAML_CERT_EXPIRED = "A certificate of the identity provider has expired; remove it"

_OIDC_FIELDS = ("issuer", "client_id", "scopes")
_SAML_FIELDS = (
    "saml_idp_entity_id",
    "saml_idp_sso_url",
    "saml_idp_certs",
    "saml_name_id_format",
    "saml_email_attribute",
)


def login_path(org_slug: str) -> str:
    """Where a browser starts signing in to ``org_slug`` (a same-origin path)."""
    return f"/api/v1/auth/sso/{org_slug}/start"


def redirect_uri(app_base_url: str, org_slug: str) -> str:
    """The callback URL registered at the identity provider."""
    return f"{app_base_url.rstrip('/')}/api/v1/auth/sso/{org_slug}/callback"


def saml_sp_entity_id(app_base_url: str, org_slug: str) -> str:
    """tripl's SAML entity id for ``org_slug``: also where its metadata is served."""
    return f"{app_base_url.rstrip('/')}/api/v1/auth/sso/{org_slug}/saml/metadata"


def saml_acs_url(app_base_url: str, org_slug: str) -> str:
    """Where the provider posts its SAML response (Assertion Consumer Service)."""
    return f"{app_base_url.rstrip('/')}/api/v1/auth/sso/{org_slug}/saml/acs"


def saml_identity_issuer(entity_id: str) -> str:
    """The ``issuer`` a SAML identity is stored under: prefixed, never an OIDC issuer."""
    return SAML_ISSUER_PREFIX + entity_id


def idp_issuer(config: OrgSsoConfig) -> str:
    """The identity of the configured provider: the OIDC issuer, or ``saml:`` and the
    SAML entity id.

    Linked identities and link tickets are keyed by it. The prefix keeps an
    owner from pointing SAML at an entity id equal to the OIDC issuer the
    members linked through (their own certificate, someone else's identity).
    """
    if config.protocol == PROTOCOL_SAML:
        entity_id = config.saml_idp_entity_id or ""
        return saml_identity_issuer(entity_id) if entity_id else ""
    return config.issuer or ""


def _fingerprints(pem_text: str | None) -> frozenset[str]:
    return frozenset(info.fingerprint_sha256 for info in describe_certs(pem_text))


def _trust_anchor_changed(config: OrgSsoConfig, values: dict[str, object], protocol: str) -> bool:
    """Whether the new settings name a different provider than the saved ones.

    Protocol switched; SAML entity id changed; or the new SAML certificates
    share none with the saved ones (who holds the signing key may have changed).
    An OIDC issuer change needs nothing here: identities are keyed by the
    issuer, which tripl checks against the provider's own discovery document.
    """
    if config.protocol != protocol:
        return True
    if protocol != PROTOCOL_SAML:
        return False
    if values.get("saml_idp_entity_id") != config.saml_idp_entity_id:
        return True
    old = _fingerprints(config.saml_idp_certs)
    new_certs = values.get("saml_idp_certs")
    new = _fingerprints(None if new_certs is None else str(new_certs))
    return not old or not (old & new)


async def forget_links(session: AsyncSession, org_id: uuid.UUID, issuer: str) -> int:
    """Drop the org's linked identities and pending link tickets for ``issuer``. No commit.

    Members confirm their link again at their next SSO sign-in. Returns how
    many identities went.
    """
    if not issuer:
        return 0
    result = await session.execute(
        delete(UserSsoIdentity)
        .where(UserSsoIdentity.organization_id == org_id, UserSsoIdentity.issuer == issuer)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        delete(SsoLinkTicket)
        .where(SsoLinkTicket.organization_id == org_id, SsoLinkTicket.issuer == issuer)
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def describe_certs(pem_text: str | None) -> list[SamlCertificateInfo]:
    """What the saved certificates are; empty when there are none (or they do not parse)."""
    if not pem_text:
        return []
    try:
        certs = saml_xml.load_certs(pem_text)
    except saml_xml.SamlXmlError:
        return []
    return [cert_info_response(cert) for cert in certs]


def cert_info_response(cert: x509.Certificate) -> SamlCertificateInfo:
    info = saml_xml.cert_info(cert)
    return SamlCertificateInfo(
        fingerprint_sha256=info.fingerprint_sha256,
        subject=info.subject,
        not_before=info.not_before,
        not_after=info.not_after,
        expired=info.expired,
    )


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
    sp_entity_id = saml_sp_entity_id(app_base_url, org_slug)
    return OrgSsoConfigResponse(
        configured=config is not None,
        protocol="saml" if config is not None and config.protocol == PROTOCOL_SAML else "oidc",
        issuer="" if config is None else config.issuer or "",
        client_id="" if config is None else config.client_id or "",
        client_secret_configured=config is not None and bool(config.client_secret_encrypted),
        scopes=DEFAULT_SSO_SCOPES if config is None else config.scopes,
        enabled=config is not None and config.enabled,
        sso_required=config is not None and config.enabled and config.sso_required,
        redirect_uri=redirect_uri(app_base_url, org_slug),
        login_url=login_path(org_slug),
        saml_idp_entity_id="" if config is None else config.saml_idp_entity_id or "",
        saml_idp_sso_url="" if config is None else config.saml_idp_sso_url or "",
        saml_idp_certs="" if config is None else config.saml_idp_certs or "",
        saml_cert_info=describe_certs(None if config is None else config.saml_idp_certs),
        saml_name_id_format=NAMEID_EMAIL if config is None else config.saml_name_id_format,
        saml_email_attribute=None if config is None else config.saml_email_attribute,
        saml_sp_entity_id=sp_entity_id,
        saml_acs_url=saml_acs_url(app_base_url, org_slug),
        saml_metadata_url=sp_entity_id,
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


def check_saml_sso_url(url: str) -> None:
    """422 unless ``url`` is https. Never fetched by tripl: the browser goes there."""
    if not saml_xml.check_https_url(url):
        raise _unprocessable(SAML_SSO_URL_HTTPS)


def normalize_saml_certs(pem_text: str) -> str:
    """The certificates re-encoded as PEM; 422 when one does not parse or has expired."""
    try:
        certs = saml_xml.load_certs(pem_text)
    except saml_xml.SamlXmlError as exc:
        raise _unprocessable(str(exc)) from None
    now = datetime.now(UTC)
    if any(cert.not_valid_after_utc <= now for cert in certs):
        raise _unprocessable(SAML_CERT_EXPIRED)
    return saml_xml.certs_pem(certs)


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
    #: Linked identities dropped because the provider's trust anchor changed.
    unlinked_identities: int = 0


async def save_config(
    session: AsyncSession, org_id: uuid.UUID, data: OrgSsoConfigUpdate
) -> SavedConfig:
    """Create or replace the configuration. Flushes, does not commit.

    The active protocol's fields are all written; the other protocol's only
    when the request gave them a value (omitted or null: kept as stored). ``changed`` names the
    fields that changed (``client_secret`` included when a new one was given;
    never its value).
    """
    given = data.model_fields_set
    saml = data.protocol == PROTOCOL_SAML

    def inactive(name: str) -> bool:
        return name in given and getattr(data, name) is not None

    write_oidc = [name for name in _OIDC_FIELDS if not saml or inactive(name)]
    write_saml = [name for name in _SAML_FIELDS if saml or inactive(name)]
    values: dict[str, object] = {name: getattr(data, name) for name in write_oidc + write_saml}
    if "scopes" in values and values["scopes"] is None:
        values["scopes"] = DEFAULT_SSO_SCOPES

    config = await get_config(session, org_id)
    active = set(_SAML_FIELDS if saml else _OIDC_FIELDS)
    switching = config is None or config.protocol != data.protocol

    def must_check(name: str) -> bool:
        """Validate a value that is new, or one the switch just made active.

        A value echoed back unchanged is not re-checked: a certificate that
        expired since, or an issuer whose host is unreachable now, must not
        block turning SSO off or saving the other protocol.
        """
        value = values.get(name)
        if value is None:
            return False
        if config is None:
            return True
        stored = getattr(config, name)
        if name == "saml_idp_certs":
            # Compared as certificates: the stored text is re-encoded PEM, and
            # the page echoes it back with its own whitespace.
            unchanged = bool(stored) and _fingerprints(stored) == _fingerprints(str(value))
        else:
            unchanged = stored == value
        if not unchanged:
            return True
        return switching and name in active

    if must_check("issuer"):
        await check_issuer(str(values["issuer"]))
    if must_check("saml_idp_sso_url"):
        check_saml_sso_url(str(values["saml_idp_sso_url"]))
    if must_check("saml_idp_certs"):
        values["saml_idp_certs"] = normalize_saml_certs(str(values["saml_idp_certs"]))
    elif config is not None and values.get("saml_idp_certs") is not None:
        # The same certificates echoed back: keep the stored encoding.
        values["saml_idp_certs"] = config.saml_idp_certs

    has_secret = config is not None and bool(config.client_secret_encrypted)
    if not saml and data.client_secret is None and not has_secret:
        raise _unprocessable(SECRET_REQUIRED)
    if data.sso_required and not data.enabled:
        raise _unprocessable(REQUIRED_NEEDS_ENABLED)
    if data.enabled and not await has_verified_domain(session, org_id):
        raise _conflict(ENABLE_NEEDS_DOMAIN)

    was_required = config is not None and config.enabled and config.sso_required
    unlinked = 0
    if config is not None and _trust_anchor_changed(config, values, data.protocol):
        unlinked = await forget_links(session, org_id, idp_issuer(config))
    tracked = ["protocol", *values, "enabled", "sso_required"]
    if config is None:
        config = OrgSsoConfig(organization_id=org_id)
        session.add(config)
        changed = [name for name in tracked if getattr(data, name) is not None]
    else:
        changed = [
            name
            for name in tracked
            if getattr(config, name) != values.get(name, getattr(data, name))
        ]
    config.protocol = data.protocol
    for name, value in values.items():
        setattr(config, name, value)
    config.enabled = data.enabled
    config.sso_required = data.sso_required
    if data.client_secret is not None:
        config.client_secret_encrypted = encrypt_value(data.client_secret)
        changed.append("client_secret")
    revoked = 0
    if data.enabled and data.sso_required and not was_required:
        revoked = await revoke_non_sso_keys(session, org_id)
    await session.flush()
    return SavedConfig(
        config=config, changed=changed, revoked_api_keys=revoked, unlinked_identities=unlinked
    )


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
        SamlAssertionId,
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
