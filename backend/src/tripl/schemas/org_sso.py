"""Single sign-on (OIDC and SAML 2.0) request and response bodies (F20, GH #273)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tripl.models.org_sso import DEFAULT_SSO_SCOPES, NAMEID_EMAIL, SAML_ENTITY_ID_MAX_LENGTH
from tripl.schemas.auth import AuthUserResponse

_SCOPE_TOKEN = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+$")
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$"
)
#: The TXT record's name is this label in front of the domain.
VERIFICATION_LABEL = "_tripl-verification"
VERIFICATION_PREFIX = "tripl-verification="

#: The NameID formats an owner may request from a SAML provider. Transient is
#: not one: it names nobody twice.
SAML_NAME_ID_FORMATS = (
    NAMEID_EMAIL,
    "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
    "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
)
#: A pasted IdP metadata document or certificate list, at most.
SAML_MAX_XML_CHARS = 256 * 1024
SAML_MAX_CERTS_CHARS = 64 * 1024


def normalize_domain(value: str) -> str:
    """Lowercase, no surrounding space or trailing dot; ``ValueError`` if not a domain."""
    domain = value.strip().lower().rstrip(".")
    if not _DOMAIN.match(domain):
        raise ValueError("Enter a domain name such as example.com")
    return domain


class OrgSsoConfigUpdate(BaseModel):
    """``PUT /orgs/{org}/sso``.

    ``protocol`` picks the provider's kind. Its own fields are required (OIDC:
    ``issuer``, ``client_id``; SAML: ``saml_idp_entity_id``,
    ``saml_idp_sso_url``, ``saml_idp_certs``); the other protocol's fields are
    saved only when given non-null, and kept as stored when omitted or null,
    so switching back loses nothing. ``client_secret`` omitted or null keeps
    the stored one; ``scopes`` null is the default set.
    """

    model_config = ConfigDict(extra="forbid")

    protocol: Literal["oidc", "saml"] = "oidc"
    issuer: str | None = Field(default=None, min_length=1, max_length=512)
    client_id: str | None = Field(default=None, min_length=1, max_length=255)
    client_secret: str | None = Field(default=None, max_length=4096)
    scopes: str | None = Field(default=DEFAULT_SSO_SCOPES, max_length=512)
    saml_idp_entity_id: str | None = Field(
        default=None, min_length=1, max_length=SAML_ENTITY_ID_MAX_LENGTH
    )
    saml_idp_sso_url: str | None = Field(default=None, min_length=1, max_length=2048)
    #: One or more PEM certificates (several while one rotates).
    saml_idp_certs: str | None = Field(default=None, min_length=1, max_length=SAML_MAX_CERTS_CHARS)
    saml_name_id_format: str = Field(default=NAMEID_EMAIL, max_length=255)
    #: The attribute carrying the email; omitted or null: the NameID is the email.
    saml_email_attribute: str | None = Field(default=None, max_length=255)
    enabled: bool = False
    sso_required: bool = False

    @field_validator("issuer", "client_id", "saml_idp_entity_id", "saml_idp_sso_url")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped or any(ch.isspace() or ord(ch) < 32 for ch in stripped):
            raise ValueError("must not be empty or contain whitespace")
        return stripped

    @field_validator("saml_idp_certs")
    @classmethod
    def _certs(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\x00" in value:
            raise ValueError("must not contain NUL")
        return value.strip() or None

    @field_validator("saml_name_id_format")
    @classmethod
    def _name_id_format(cls, value: str) -> str:
        stripped = value.strip()
        if stripped not in SAML_NAME_ID_FORMATS:
            raise ValueError("unsupported NameID format")
        return stripped

    @field_validator("saml_email_attribute")
    @classmethod
    def _email_attribute(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in stripped):
            raise ValueError("must not contain control characters")
        return stripped or None

    @model_validator(mode="after")
    def _required_per_protocol(self) -> Self:
        if self.protocol == "oidc":
            missing = [name for name in ("issuer", "client_id") if getattr(self, name) is None]
        else:
            missing = [
                name
                for name in ("saml_idp_entity_id", "saml_idp_sso_url", "saml_idp_certs")
                if getattr(self, name) is None
            ]
        if missing:
            raise ValueError(f"{', '.join(missing)} required for protocol {self.protocol}")
        return self

    @field_validator("client_secret")
    @classmethod
    def _secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if "\x00" in stripped:
            raise ValueError("must not contain NUL")
        return stripped or None

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, value: str | None) -> str | None:
        # Null: the default set for OIDC, "keep what is saved" for SAML.
        if value is None or not value.strip():
            return None
        tokens = value.split()
        if not all(_SCOPE_TOKEN.match(token) for token in tokens):
            raise ValueError("scopes are space-separated OAuth scope tokens")
        if "openid" not in tokens:
            raise ValueError("scopes must include openid")
        return " ".join(dict.fromkeys(tokens))


class SamlCertificateInfo(BaseModel):
    #: SHA-256 over the DER, colon-separated uppercase hex.
    fingerprint_sha256: str
    subject: str
    not_before: datetime
    not_after: datetime
    expired: bool


class OrgSsoConfigResponse(BaseModel):
    configured: bool
    protocol: Literal["oidc", "saml"]
    issuer: str
    client_id: str
    #: The secret itself is write-only.
    client_secret_configured: bool
    scopes: str
    enabled: bool
    sso_required: bool
    #: What to register at the identity provider.
    redirect_uri: str
    #: Where members start signing in.
    login_url: str
    saml_idp_entity_id: str
    saml_idp_sso_url: str
    #: The IdP certificates as saved (public), and what they are.
    saml_idp_certs: str
    saml_cert_info: list[SamlCertificateInfo]
    saml_name_id_format: str
    saml_email_attribute: str | None
    #: What to register at a SAML identity provider (tripl's metadata URL is
    #: also its entity id).
    saml_sp_entity_id: str
    saml_acs_url: str
    saml_metadata_url: str


class OrgSsoConfigSaved(OrgSsoConfigResponse):
    #: API keys revoked because this save turned "SSO required" on.
    revoked_api_keys: int = 0


class OrgSsoTestResult(BaseModel):
    ok: bool
    #: A short stable code when ``ok`` is false (``idp_issuer_mismatch``, ...).
    error_code: str | None = None
    message: str
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    jwks_uri: str | None = None
    token_endpoint_auth_method: str | None = None
    #: SAML: the configured certificates.
    saml_cert_info: list[SamlCertificateInfo] | None = None


class SamlMetadataImport(BaseModel):
    """``POST /orgs/{org}/sso/saml/metadata-import``: the IdP's metadata XML, pasted."""

    model_config = ConfigDict(extra="forbid")

    xml: str = Field(min_length=1, max_length=SAML_MAX_XML_CHARS)


class SamlMetadataImportResult(BaseModel):
    """What the metadata says, to fill the form with; nothing is saved."""

    saml_idp_entity_id: str
    saml_idp_sso_url: str
    saml_idp_certs: str
    saml_cert_info: list[SamlCertificateInfo]


class OrgSsoDomainCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1, max_length=253)

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return normalize_domain(value)


class OrgSsoDomainResponse(BaseModel):
    id: uuid.UUID
    domain: str
    verified: bool
    verified_at: datetime | None
    #: The DNS TXT record the owner publishes to prove the domain.
    txt_record_name: str
    txt_record_value: str
    created_at: datetime


class SsoDiscoverOrg(BaseModel):
    slug: str
    name: str
    login_url: str


class SsoDiscoverResponse(BaseModel):
    orgs: list[SsoDiscoverOrg]


class SsoLinkPreview(BaseModel):
    email: str
    org_slug: str
    org_name: str
    expires_at: datetime
    #: This browser must sign in to the account (password or another way)
    #: before it can confirm the link.
    sign_in_required: bool


class SsoLinkConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket: str = Field(min_length=1, max_length=512)


class SsoLinkResult(BaseModel):
    #: Where the SPA goes next (a same-origin path).
    next: str
    user: AuthUserResponse
