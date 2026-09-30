"""Mail an invitation link through the OPERATOR's SMTP (F20 PR6, owner decision 3).

Account mail — sign-up, password reset, invitations — always goes through the
instance operator's relay, never an organization's own, so the person invited
receives it whatever the inviting organization has configured.

Optional by design: the create response still carries ``accept_path`` for the
inviter to hand over, which is the path that works on an instance without SMTP.
The send runs as a FastAPI ``BackgroundTask`` after the response, so a slow
relay neither blocks the request nor fails it; failures are logged.
"""

from __future__ import annotations

import logging
import smtplib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.organization import Organization
from tripl.services import app_settings_service, invitation_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvitationMail:
    """One invitation email, ready to send."""

    recipient: str
    organization_name: str
    link: str
    email_config: app_settings_service.EmailConfig


async def prepare(
    session: AsyncSession, *, recipient: str, organization_id: uuid.UUID, accept_path: str
) -> InvitationMail | None:
    """The mail to send, or ``None`` when the operator has no working SMTP.

    Account mail: always the OPERATOR's relay (F20 PR9, owner decision 3), never
    the inviting organization's — the invitee is not yet a member of it.
    """
    overrides = await app_settings_service.get_service_overrides(session)
    email_config = app_settings_service.build_email_config(overrides)
    if not app_settings_service.email_can_send(email_config):
        return None
    organization_name: str = (
        await session.scalar(select(Organization.name).where(Organization.id == organization_id))
    ) or "an organization"
    base_url = app_settings_service.build_runtime_config(overrides).app_base_url
    return InvitationMail(
        recipient=recipient,
        organization_name=organization_name,
        link=f"{base_url.rstrip('/')}{accept_path}",
        email_config=email_config,
    )


def send(mail: InvitationMail) -> None:
    """Send ``mail``. Best-effort: every failure is logged and swallowed."""
    # Lazy import keeps the worker email module off the API's import path.
    from tripl.worker.tasks.alerts_channels import _send_email_message

    body = (
        f"You have been invited to join {mail.organization_name} on tripl.\n\n"
        f"Accept the invitation here (valid for {invitation_service.INVITATION_TTL_HOURS} "
        f"hours):\n{mail.link}\n\n"
        "If you already have a tripl account with this address, sign in first and the "
        "organization is added to it. If you did not expect this, ignore this email.\n"
    )
    config = mail.email_config
    try:
        _send_email_message(
            smtp_module=smtplib,
            smtp_host=config.smtp_host,
            smtp_port=config.smtp_port,
            smtp_username=config.smtp_username,
            smtp_password=config.smtp_password,
            smtp_security=config.smtp_security,
            from_address=config.smtp_from_address,
            recipients=[mail.recipient],
            subject=f"You're invited to {mail.organization_name} on tripl",
            body=body,
        )
    except Exception:  # noqa: BLE001 - best-effort; the inviter still has the link
        logger.exception("Failed to send invitation email")
