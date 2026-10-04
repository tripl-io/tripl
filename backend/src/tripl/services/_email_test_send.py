"""Send one probe message using the instance's own SMTP settings.

There was already a test send, but it hangs off an alert DESTINATION
(``_alerting_test_send``) and can only exercise one that has been saved. An
operator configuring SMTP so password-reset links go out has no destination and
never will — so the one path whose failure is invisible was also the one with no
way to check itself: ``request_password_reset`` returns the same neutral message
either way, and the background send swallows whatever goes wrong.

Deliberately blocking, like every other channel client here. The caller hands it
to ``asyncio.to_thread``.
"""

from __future__ import annotations

import smtplib

from tripl.alerting_validation import validate_email_address, validate_sender_address
from tripl.services.app_settings_service import EmailConfig

TEST_SUBJECT = "tripl SMTP test"
TEST_BODY = (
    "This is a test message from tripl.\n\n"
    "If you are reading it, the instance can reach your SMTP relay, and password "
    "reset links and alert email will be delivered.\n"
)


def send_test_email(*, email_config: EmailConfig, recipient: str) -> None:
    """Deliver one probe message, or raise saying why it could not.

    Refuses on missing configuration BEFORE opening a socket, and names the
    setting that is missing. The blank From: address is the interesting case: it
    does not fail at the transport at all — ``_send_password_reset_email``
    returns early on it, so a reset link is minted, never sent, and nothing but a
    log line records that. Reporting it is half the reason this endpoint exists.
    """
    if not email_config.smtp_host:
        raise ValueError("No SMTP host is configured, so nothing can be sent.")
    if not email_config.smtp_from_address:
        raise ValueError(
            "No default From: address is configured. Password reset mail is dropped "
            "without one, even when the relay itself is reachable."
        )
    # Shared with the alert-destination test, so the two diagnostics cannot
    # disagree about which senders are usable — they did, and one of them was
    # failing configurations that deliver.
    validate_sender_address(email_config.smtp_from_address)
    address = validate_email_address(recipient)

    # Lazy, for the reason ``api/v1/auth.py`` gives at its own copy of this
    # import: it keeps the worker's email module off the API's import path.
    from tripl.worker.tasks.alerts_channels import _send_email_message

    _send_email_message(
        smtp_module=smtplib,
        smtp_host=email_config.smtp_host,
        smtp_port=email_config.smtp_port,
        smtp_username=email_config.smtp_username,
        smtp_password=email_config.smtp_password,
        smtp_security=email_config.smtp_security,
        from_address=email_config.smtp_from_address,
        recipients=[address],
        subject=TEST_SUBJECT,
        body=TEST_BODY,
    )
