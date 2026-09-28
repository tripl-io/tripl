"""An organization's single sign-on settings: ``/api/v1/orgs/{org}/sso`` (F20, GH #273).

Every route is for an OWNER of the path's organization, from a browser session
(``get_path_org_owner_user``): an admin, a member and an API key get 403, a
stranger 404. Owners only, reads included: the settings decide who can sign in
to the organization at all.

* ``GET/PUT /sso`` — the provider settings, OIDC or SAML 2.0 (``protocol``);
  the client secret is write-only (``client_secret_configured``); the SAML
  certificates are public and come back with their fingerprints and expiry,
  with the values to register at a SAML provider (entity id, ACS URL).
  Turning "SSO required" on revokes the organization's API keys not minted
  from its SSO session (owners' included); the count is in the answer and the
  audit row.
* ``POST /sso/test`` — OIDC: fetch the discovery document and check it (the
  ``sso_probe`` rate-limit bucket, as domain verification). SAML: check the
  saved SSO URL is https and the certificates parse and have not expired
  (nothing is fetched).
* ``POST /sso/saml/metadata-import`` — read a pasted IdP metadata document
  (entity id, HTTP-Redirect SSO URL, signing certificates) to fill the form
  with. Paste only: tripl fetches no metadata URL; nothing is saved.
* ``GET/POST /sso/domains``, ``DELETE /sso/domains/{domain_id}``,
  ``POST /sso/domains/{domain_id}/verify`` — the email domains, proven by DNS.

Every change is audited as ``org.sso.*``, never with a secret in the payload.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from tripl.api.deps import ManagedOrgDep, PathOrgOwnerUserDep, SessionDep
from tripl.api.v1.auth_sso import app_base_url
from tripl.middleware.rate_limit import enforce, sso_probe_rate_limiter
from tripl.models.org_sso import PROTOCOL_SAML
from tripl.schemas.org_sso import (
    OrgSsoConfigResponse,
    OrgSsoConfigSaved,
    OrgSsoConfigUpdate,
    OrgSsoDomainCreate,
    OrgSsoDomainResponse,
    OrgSsoTestResult,
    SamlMetadataImport,
    SamlMetadataImportResult,
)
from tripl.services import audit_service, org_sso_service, saml_xml, sso_http
from tripl.services.sso_http import IdpError

router = APIRouter(prefix="/orgs/{org}/sso", tags=["organizations"])


@router.get("", response_model=OrgSsoConfigResponse)
async def get_sso(
    request: Request, session: SessionDep, current_user: PathOrgOwnerUserDep, org: ManagedOrgDep
) -> OrgSsoConfigResponse:
    del current_user
    config = await org_sso_service.get_config(session, org.id)
    return org_sso_service.config_response(
        config, org_slug=org.slug, app_base_url=await app_base_url(session, request)
    )


@router.put("", response_model=OrgSsoConfigSaved)
async def put_sso(
    request: Request,
    session: SessionDep,
    data: OrgSsoConfigUpdate,
    current_user: PathOrgOwnerUserDep,
    org: ManagedOrgDep,
) -> OrgSsoConfigSaved:
    """Save the provider settings. 422 for a bad issuer (a private host, hosted)
    or a missing secret; 409 when enabling without a verified domain."""
    saved = await org_sso_service.save_config(session, org.id, data)
    await audit_service.record(
        session,
        user=current_user,
        action="org.sso.update",
        target_type="organization",
        target_id=org.id,
        target_name=org.slug,
        payload={
            "changed": saved.changed,
            "protocol": saved.config.protocol,
            "issuer": saved.config.issuer,
            "client_id": saved.config.client_id,
            "saml_idp_entity_id": saved.config.saml_idp_entity_id,
            "enabled": saved.config.enabled,
            "sso_required": saved.config.sso_required,
            "revoked_api_keys": saved.revoked_api_keys,
            "unlinked_identities": saved.unlinked_identities,
        },
        organization_id=org.id,
    )
    body = org_sso_service.config_response(
        saved.config, org_slug=org.slug, app_base_url=await app_base_url(session, request)
    )
    return OrgSsoConfigSaved(**body.model_dump(), revoked_api_keys=saved.revoked_api_keys)


_TEST_FAILED = "The identity provider's discovery document could not be used."
_TEST_MESSAGES = {
    "idp_insecure_url": "The issuer must be an https URL.",
    "idp_private_host": "The issuer must not point to a private or internal address.",
    "idp_issuer_mismatch": (
        "The discovery document names a different issuer than the one configured."
    ),
    "idp_bad_discovery": "The discovery document lacks a required https endpoint.",
    "idp_unsupported_client_auth": (
        "The provider supports neither client_secret_basic nor client_secret_post."
    ),
}


@router.post(
    "/test",
    response_model=OrgSsoTestResult,
    dependencies=[Depends(enforce(sso_probe_rate_limiter))],
)
async def probe_sso(
    session: SessionDep, current_user: PathOrgOwnerUserDep, org: ManagedOrgDep
) -> OrgSsoTestResult:
    """Check the saved provider settings. Changes nothing.

    OIDC: fetch the configured issuer's discovery document; ``ok`` false with a
    code when the issuer is unreachable, private (hosted), names another
    issuer, or lacks an https endpoint. SAML: the SSO URL is https and every
    certificate parses and is unexpired (``saml_insecure_url``,
    ``saml_bad_certificate``, ``saml_certificate_expired``); nothing is
    fetched. On a small rate-limit bucket of its own (OIDC makes tripl call out).
    """
    del current_user
    config = await org_sso_service.get_config(session, org.id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Single sign-on is not configured"
        )
    if config.protocol == PROTOCOL_SAML:
        return _probe_saml(config.saml_idp_sso_url, config.saml_idp_certs)
    if not config.issuer:
        return OrgSsoTestResult(
            ok=False, error_code="idp_insecure_url", message=_TEST_MESSAGES["idp_insecure_url"]
        )
    try:
        discovery = await asyncio.to_thread(sso_http.fetch_discovery, config.issuer)
        method = sso_http.token_auth_method(discovery)
    except IdpError as exc:
        # A fixed text per code: no status line, host or other detail of what
        # answered, so the test is no probe of the network behind the issuer.
        return OrgSsoTestResult(
            ok=False, error_code=exc.code, message=_TEST_MESSAGES.get(exc.code, _TEST_FAILED)
        )
    return OrgSsoTestResult(
        ok=True,
        message="The identity provider's discovery document is valid.",
        authorization_endpoint=discovery.authorization_endpoint,
        token_endpoint=discovery.token_endpoint,
        jwks_uri=discovery.jwks_uri,
        token_endpoint_auth_method=method,
    )


def _probe_saml(sso_url: str | None, certs_pem: str | None) -> OrgSsoTestResult:
    if not sso_url or not saml_xml.check_https_url(sso_url):
        return OrgSsoTestResult(
            ok=False,
            error_code="saml_insecure_url",
            message="The identity provider's sign-in URL must be an https URL.",
        )
    certificates = org_sso_service.describe_certs(certs_pem)
    if not certificates:
        return OrgSsoTestResult(
            ok=False,
            error_code="saml_bad_certificate",
            message="The identity provider's certificate could not be read.",
        )
    if any(cert.expired for cert in certificates):
        return OrgSsoTestResult(
            ok=False,
            error_code="saml_certificate_expired",
            message="A certificate of the identity provider has expired.",
            saml_cert_info=certificates,
        )
    return OrgSsoTestResult(
        ok=True,
        message="The SAML settings are usable.",
        saml_cert_info=certificates,
    )


@router.post("/saml/metadata-import", response_model=SamlMetadataImportResult)
async def import_saml_metadata(
    data: SamlMetadataImport, current_user: PathOrgOwnerUserDep, org: ManagedOrgDep
) -> SamlMetadataImportResult:
    """Read a pasted IdP metadata document; 422 naming what is wrong with it.

    The hardened parser of ``saml_xml`` (no DOCTYPE, entities or network);
    nothing it names is fetched and nothing is saved: the answer fills the
    form, and the owner saves it with ``PUT /sso``.
    """
    del current_user, org
    try:
        metadata = saml_xml.parse_idp_metadata(data.xml.encode("utf-8"))
    except saml_xml.SamlXmlError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None
    return SamlMetadataImportResult(
        saml_idp_entity_id=metadata.entity_id,
        saml_idp_sso_url=metadata.sso_url,
        saml_idp_certs=saml_xml.certs_pem(metadata.certs),
        saml_cert_info=[org_sso_service.cert_info_response(c) for c in metadata.certs],
    )


@router.get("/domains", response_model=list[OrgSsoDomainResponse])
async def list_domains(
    session: SessionDep, current_user: PathOrgOwnerUserDep, org: ManagedOrgDep
) -> list[OrgSsoDomainResponse]:
    del current_user
    return [
        org_sso_service.domain_response(row)
        for row in await org_sso_service.list_domains(session, org.id)
    ]


@router.post("/domains", response_model=OrgSsoDomainResponse, status_code=status.HTTP_201_CREATED)
async def add_domain(
    session: SessionDep,
    data: OrgSsoDomainCreate,
    current_user: PathOrgOwnerUserDep,
    org: ManagedOrgDep,
) -> OrgSsoDomainResponse:
    """Claim a domain, unverified; the answer names the TXT record to publish.

    409 when this organization already has it or another one verified it.
    """
    row = await org_sso_service.add_domain(session, org.id, data.domain)
    await audit_service.record(
        session,
        user=current_user,
        action="org.sso.domain_add",
        target_type="sso_domain",
        target_id=row.id,
        target_name=row.domain,
        payload={"domain": row.domain},
        organization_id=org.id,
    )
    return org_sso_service.domain_response(row)


@router.delete("/domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_domain(
    session: SessionDep,
    domain_id: uuid.UUID,
    current_user: PathOrgOwnerUserDep,
    org: ManagedOrgDep,
) -> None:
    """Remove a domain. 409 for the last verified one while SSO is enabled."""
    row = await org_sso_service.get_domain(session, org.id, domain_id)
    domain, was_verified = row.domain, row.verified_at is not None
    await org_sso_service.remove_domain(session, org.id, row)
    await audit_service.record(
        session,
        user=current_user,
        action="org.sso.domain_remove",
        target_type="sso_domain",
        target_id=domain_id,
        target_name=domain,
        payload={"domain": domain, "verified": was_verified},
        organization_id=org.id,
    )


@router.post(
    "/domains/{domain_id}/verify",
    response_model=OrgSsoDomainResponse,
    dependencies=[Depends(enforce(sso_probe_rate_limiter))],
)
async def verify_domain(
    session: SessionDep,
    domain_id: uuid.UUID,
    current_user: PathOrgOwnerUserDep,
    org: ManagedOrgDep,
) -> OrgSsoDomainResponse:
    """Look the TXT record up now.

    200 with ``verified`` true once it holds the token (audited the first
    time), ``verified`` false while it does not; 409 when another organization
    verified the domain first.
    """
    row = await org_sso_service.get_domain(session, org.id, domain_id)
    was_verified = row.verified_at is not None
    verified = await org_sso_service.verify_domain(session, org.id, row)
    if verified and not was_verified:
        await audit_service.record(
            session,
            user=current_user,
            action="org.sso.domain_verify",
            target_type="sso_domain",
            target_id=row.id,
            target_name=row.domain,
            payload={"domain": row.domain},
            organization_id=org.id,
        )
    return org_sso_service.domain_response(row)
