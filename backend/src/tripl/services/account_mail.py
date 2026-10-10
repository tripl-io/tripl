"""Account mail: what tripl sends a person about their own account.

Password reset and email verification links (sent from ``api/v1/auth.py``) and
invitation links (:mod:`tripl.services.invitation_email`). All of it goes
through the OPERATOR's relay (F20 PR9, owner decision 3), never an
organization's: the recipient may not be a member of that organization yet, and
whether someone can get into their account must not depend on what an
organization configured.

Every caller checks :attr:`OperatorMail.can_send` before it issues a token or
queues a message, so nothing here checks the relay configuration again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.services import app_settings_service, auth_service, email_verification_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperatorMail:
    """The operator's relay, and the public URL the links in its mail point at."""

    config: app_settings_service.EmailConfig
    app_base_url: str

    @property
    def can_send(self) -> bool:
        """Whether a message would actually leave: see ``email_can_send``."""
        return app_settings_service.email_can_send(self.config)

    def link(self, path: str) -> str:
        """``path`` (it starts with ``/``) on the app's public URL.

        ``app_base_url`` should be set whenever email is configured; when it is
        blank the link is a relative path.
        """
        return f"{self.app_base_url.rstrip('/')}{path}"


async def operator_mail(session: AsyncSession) -> OperatorMail:
    """The operator's relay and ``app_base_url``, from one read of the operator settings."""
    overrides = await app_settings_service.get_service_overrides(session)
    return OperatorMail(
        config=app_settings_service.build_email_config(overrides),
        app_base_url=app_settings_service.build_runtime_config(overrides).app_base_url,
    )


def send(
    email_config: app_settings_service.EmailConfig,
    *,
    recipient: str,
    subject: str,
    body: str,
    what: str,
) -> None:
    """Send one account mail. Best-effort: a failure is logged and swallowed.

    The routes queue account mail as a FastAPI ``BackgroundTask``, so this runs
    after the response: a slow or failing relay neither blocks nor fails the
    request, and the response time does not show whether an account exists.
    Each kind of mail has its own way to ask again. ``what`` names the mail in
    the log.
    """
    # Lazy: keeps the worker's email module off the API's import path.
    from tripl.worker.tasks.alerts_channels import send_with_config

    try:
        send_with_config(email_config, recipients=[recipient], subject=subject, body=body)
    except Exception:  # noqa: BLE001 - best-effort; the caller has already answered.
        logger.exception("Failed to send %s email", what)


def password_reset_link(mail: OperatorMail, raw_token: str) -> str:
    """The reset link a mail carries: ``auth_service.password_reset_path`` on the app's URL."""
    return mail.link(auth_service.password_reset_path(raw_token))


def send_password_reset(
    *, recipient: str, reset_link: str, email_config: app_settings_service.EmailConfig
) -> None:
    """The password reset mail. Nothing tells the requester whether it went out."""
    send(
        email_config,
        recipient=recipient,
        subject="Reset your tripl password",
        body=(
            "We received a request to reset the password for your tripl account.\n\n"
            f"Use this link to choose a new password (valid for "
            f"{auth_service.PASSWORD_RESET_TTL_HOURS} hour):\n"
            f"{reset_link}\n\n"
            "If you did not request this, you can safely ignore this email — your "
            "password will not change.\n"
        ),
        what="password reset",
    )


def send_verification(
    *, recipient: str, verify_link: str, email_config: app_settings_service.EmailConfig
) -> None:
    """The email verification mail. The account can ask for a new link
    (``POST /auth/verify-email/request``)."""
    send(
        email_config,
        recipient=recipient,
        subject="Verify your tripl email address",
        body=(
            "Confirm the email address of your tripl account.\n\n"
            f"Open this link to verify it (valid for "
            f"{email_verification_service.EMAIL_VERIFICATION_TTL_HOURS} hours):\n"
            f"{verify_link}\n\n"
            "If you did not create a tripl account, you can ignore this email.\n"
        ),
        what="verification",
    )
