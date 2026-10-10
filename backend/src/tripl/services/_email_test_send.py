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
    setting that is missing. The blank From: address is the interesting case:
    the relay may be reachable, yet ``email_can_send`` is false without it, so
    no password reset, verification or invitation mail is sent at all and the
    sign-in page says self-service password reset is not available. Naming it
    is half the reason this endpoint exists.
    """
    if not email_config.smtp_host:
        raise ValueError("No SMTP host is configured, so nothing can be sent.")
    if not email_config.smtp_from_address:
        raise ValueError(
            "No default From: address is configured. Password reset, verification and "
            "invitation mail is not sent without one, even when the relay itself is "
            "reachable."
        )
    # Shared with the alert-destination test, so the two diagnostics cannot
    # disagree about which senders are usable — they did, and one of them was
    # failing configurations that deliver.
    validate_sender_address(email_config.smtp_from_address)
    address = validate_email_address(recipient)

    # Lazy: keeps the worker's email module off the API's import path. The same
    # transport account mail uses (``account_mail.send``), but a failure raises
    # here: reporting it is the point of the test.
    from tripl.worker.tasks.alerts_channels import send_with_config

    send_with_config(email_config, recipients=[address], subject=TEST_SUBJECT, body=TEST_BODY)
