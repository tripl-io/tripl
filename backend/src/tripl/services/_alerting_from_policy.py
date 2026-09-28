"""Save-time policy for an email destination's From: override (critique #16).

On a hosted instance an organization's alert mail normally leaves through the
OPERATOR's relay, and the operator's server must not send under a sender the
organization merely typed in. Send time ignores the override there
(``app_settings_service.email_sender_for``); this refuses to store one in the
first place unless the organization runs its own relay, so save and send apply
the same rule (``app_settings_service.relay_is_scope_owned``) and a stored
override is never silently dropped. A self-hosted instance is unaffected: its
default organization IS the operator scope.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import DEPLOYMENT_HOSTED, settings
from tripl.services import app_settings_service

FROM_NEEDS_OWN_RELAY = (
    "A custom From address is used only when this organization sends through its "
    "own SMTP relay. Configure your own SMTP relay, or leave the From address empty "
    "to use the default sender."
)


async def assert_from_override_allowed(
    session: AsyncSession, org_id: uuid.UUID | None, from_address: str | None
) -> None:
    """Refuse (422) a From: override that send time would ignore."""
    if not from_address or settings.deployment_mode != DEPLOYMENT_HOSTED:
        return
    if org_id is not None:
        resolved = await app_settings_service.resolve_for_org(session, org_id)
        if resolved.org_scope is not None and app_settings_service.relay_is_scope_owned(resolved):
            return
    raise HTTPException(status_code=422, detail=FROM_NEEDS_OWN_RELAY)
