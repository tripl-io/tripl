"""The SAML 2.0 side of signing in through an organization (F20, GH #273).

``sso_login_service.start`` sends the browser to the IdP with an AuthnRequest
(its ID kept on the single-use login state, the state itself as
``RelayState``). This module takes the provider's answer at the ACS:

1. the organization has SAML sign-in enabled;
2. the login state named by ``RelayState`` is consumed (single use, 10
   minutes, this organization's, started as a SAML sign-in); the route has
   already compared it with the browser's ``tripl_saml_state`` cookie;
3. the response is verified (:func:`saml_response.verify_response`) against
   the stored AuthnRequest ID — an IdP-initiated (unsolicited) response has no
   state and never gets this far;
4. the assertion ID is remembered until it expires; a second use is refused;
5. :func:`sso_login_service.complete_sign_in` — the same account resolution as
   OIDC (linked identity, JIT, confirmed link, unverified reclaim, membership
   block, an ``sso`` session).

Also tripl's SP metadata (:func:`sp_metadata`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.domain_enums import OrganizationStatus
from tripl.models.org_sso import PROTOCOL_SAML, OrgSsoConfig, SamlAssertionId
from tripl.models.organization import Organization
from tripl.services import org_sso_service, saml_response, saml_xml
from tripl.services.oidc.flow import ERR_STATE, ERR_UNAVAILABLE, SignInFlowError
from tripl.services.saml_response import SamlError
from tripl.services.sso_login_service import (
    ERR_SAML,
    ERR_SAML_REPLAY,
    NeedsLink,
    SignedIn,
    VerifiedIdentity,
    complete_sign_in,
    consume_state,
    enabled_org,
)

logger = logging.getLogger(__name__)


async def sp_metadata(session: AsyncSession, *, org_slug: str, app_base_url: str) -> bytes | None:
    """tripl's SP metadata for an active organization configured for SAML, else None.

    Served before sign-in is enabled too: the owner registers tripl at the
    provider first, then turns SSO on.
    """
    config = await session.scalar(
        select(OrgSsoConfig)
        .join(Organization, Organization.id == OrgSsoConfig.organization_id)
        .where(
            Organization.slug == org_slug,
            Organization.status == OrganizationStatus.active.value,
            OrgSsoConfig.protocol == PROTOCOL_SAML,
        )
    )
    if config is None:
        return None
    return saml_xml.sp_metadata(
        entity_id=org_sso_service.saml_sp_entity_id(app_base_url, org_slug),
        acs_url=org_sso_service.saml_acs_url(app_base_url, org_slug),
        name_id_format=config.saml_name_id_format,
    )


async def _remember_assertion(
    session: AsyncSession, org_id: uuid.UUID, identity: saml_response.SamlIdentity
) -> None:
    """Record the assertion ID; ``saml_replay`` if it was seen. Commits."""
    now = datetime.now(UTC)
    await session.execute(
        delete(SamlAssertionId)
        .where(SamlAssertionId.expires_at < now)
        .execution_options(synchronize_session=False)
    )
    seen = await session.scalar(
        select(SamlAssertionId.id).where(
            SamlAssertionId.organization_id == org_id,
            SamlAssertionId.assertion_id == identity.assertion_id,
        )
    )
    if seen is not None:
        await session.commit()
        raise SignInFlowError(ERR_SAML_REPLAY)
    session.add(
        SamlAssertionId(
            organization_id=org_id,
            assertion_id=identity.assertion_id,
            expires_at=identity.replay_until,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # The same assertion, posted twice at once.
        await session.rollback()
        raise SignInFlowError(ERR_SAML_REPLAY) from None


async def acs(
    session: AsyncSession,
    *,
    org_slug: str,
    saml_response_b64: str | None,
    relay_state: str | None,
    app_base_url: str,
) -> SignedIn | NeedsLink:
    """Finish a SAML sign-in; the outcomes are ``complete_sign_in``'s."""
    org = await enabled_org(session, org_slug)
    config = org.config
    if config.protocol != PROTOCOL_SAML:
        raise SignInFlowError(ERR_UNAVAILABLE)
    if not relay_state:
        raise SignInFlowError(ERR_STATE)
    state = await consume_state(session, org, relay_state)
    if not state.request_id:
        # An OIDC sign-in's state.
        raise SignInFlowError(ERR_STATE)
    if not saml_response_b64:
        raise SignInFlowError(ERR_SAML)
    idp_entity_id = config.saml_idp_entity_id or ""
    try:
        certs = saml_xml.load_certs(config.saml_idp_certs or "")
    except saml_xml.SamlXmlError:
        logger.warning("SAML ACS for %s: the configured certificates do not load", org.slug)
        raise SignInFlowError(ERR_UNAVAILABLE) from None
    if not idp_entity_id:
        raise SignInFlowError(ERR_UNAVAILABLE)
    try:
        identity = await asyncio.to_thread(
            saml_response.verify_response,
            saml_response_b64,
            idp_entity_id=idp_entity_id,
            certs=certs,
            sp_entity_id=org_sso_service.saml_sp_entity_id(app_base_url, org.slug),
            acs_url=org_sso_service.saml_acs_url(app_base_url, org.slug),
            request_id=state.request_id,
            email_attribute=config.saml_email_attribute,
        )
    except SamlError as exc:
        # The reason stays in the log; the browser gets the code only.
        logger.warning("SAML response for %s refused: %s", org.slug, exc)
        raise SignInFlowError(exc.code) from None
    await _remember_assertion(session, org.id, identity)
    return await complete_sign_in(
        session,
        org,
        VerifiedIdentity(
            # Prefixed: a SAML identity never matches an OIDC one.
            issuer=org_sso_service.saml_identity_issuer(identity.issuer),
            subject=identity.subject,
            email=identity.email,
            name=identity.name,
        ),
        next_path=state.next_path,
    )
