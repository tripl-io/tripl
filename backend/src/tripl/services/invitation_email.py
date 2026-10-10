"""Mail an invitation link through the OPERATOR's SMTP (F20 PR6, owner decision 3).

Account mail (:mod:`tripl.services.account_mail`): the invitee receives it
whatever the inviting organization has configured.

Optional by design: the create response still carries ``accept_path`` for the
inviter to hand over, which is the path that works on an instance without SMTP.
The send runs as a FastAPI ``BackgroundTask`` after the response, so a slow
relay neither blocks the request nor fails it; failures are logged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.organization import Organization
from tripl.services import account_mail, app_settings_service, invitation_service


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

    Never the inviting organization's relay: the invitee is not yet a member.
    """
    mail = await account_mail.operator_mail(session)
    if not mail.can_send:
        return None
    organization_name: str = (
        await session.scalar(select(Organization.name).where(Organization.id == organization_id))
    ) or "an organization"
    return InvitationMail(
        recipient=recipient,
        organization_name=organization_name,
        link=mail.link(accept_path),
        email_config=mail.config,
    )


def send(mail: InvitationMail) -> None:
    """Send ``mail``. Best-effort: the inviter still has the link."""
    account_mail.send(
        mail.email_config,
        recipient=mail.recipient,
        subject=f"You're invited to {mail.organization_name} on tripl",
        body=(
            f"You have been invited to join {mail.organization_name} on tripl.\n\n"
            f"Accept the invitation here (valid for {invitation_service.INVITATION_TTL_HOURS} "
            f"hours):\n{mail.link}\n\n"
            "If you already have a tripl account with this address, sign in first and the "
            "organization is added to it. If you did not expect this, ignore this email.\n"
        ),
        what="invitation",
    )
