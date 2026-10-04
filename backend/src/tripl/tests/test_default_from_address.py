"""Batch 4, review follow-up: the whitespace-only Default From.

``EmailSettingsUpdate._check_smtp_from_address`` guards the global Default From
with the send path's own helper, and its own comment says ``None`` and ``""``
are how the value is CLEARED. A whitespace-only string was neither: it skipped
the validator (``not value.strip()``) and was then returned unchanged, so a
``PATCH`` carrying ``smtp_from_address: "   "`` stored a TRUTHY blank — the one
state the comment said the field does not have.

Truthiness is the whole defect. Every reader of this setting asks whether it is
configured with ``not`` or ``or``, so a truthy blank passes for configured
everywhere and fails at the far end of each path with a diagnostic that names
something else: the alert reports an invalid From: address rather than an unset
one, Settings -> Send test email answers ``Not a usable From: address: '   '``
instead of the sentence it keeps for exactly this case, and password reset mints
a token, promises the mail, and hands SMTP a blank sender.

The fix folds whitespace into the empty string the clearing path already uses
rather than refusing it with a 422 — refusing would mean an operator who clears
the field by typing a space cannot reach a state the product supports, and the
value left behind would be the old sender, still sending.
"""

from __future__ import annotations

import pytest

from tripl.schemas.app_settings import EmailSettingsUpdate
from tripl.services._email_test_send import send_test_email
from tripl.services.app_settings_service import EmailConfig


def _email_config(from_address: str) -> EmailConfig:
    """A relay that is configured in every respect except the sender.

    Host, port, credentials and security are all present, so the only thing a
    probe against this config can complain about is the field under test.
    """
    return EmailConfig(
        smtp_host="relay.example.com",
        smtp_port=587,
        smtp_username="tripl",
        smtp_password="secret",
        smtp_security="starttls",
        smtp_from_address=from_address,
    )


def test_a_whitespace_only_default_from_is_stored_as_the_cleared_value() -> None:
    """Whitespace normalises to "", the value ``""`` already stores.

    Restore ``return value`` in place of ``return ""`` in
    ``EmailSettingsUpdate._check_smtp_from_address`` and both assertions go red
    with ``'   '``: the model keeps the blank, and ``exclude_unset`` hands that
    blank to ``update_service_overrides``, which writes it verbatim because
    ``smtp_from_address`` is not a secret field and only ``None`` is popped.

    Not a ``ValidationError``, deliberately. Making this a 422 would also make
    the first assertion pass in form while refusing an operator the "not
    configured" state — and would leave the previous Default From in place,
    still sending, which is the failure this whole field was validated to
    prevent.
    """
    assert EmailSettingsUpdate(smtp_from_address="   ").smtp_from_address == ""
    # What actually reaches the override map: the flattened update the endpoint
    # builds, carrying the cleared value rather than the blank one.
    assert EmailSettingsUpdate(smtp_from_address="   ").model_dump(exclude_unset=True) == {
        "smtp_from_address": ""
    }


def test_the_cleared_sender_reaches_the_probe_as_the_setting_it_actually_is(
    deny_network: None,
) -> None:
    """The consequence, not the string: which failure the operator is shown.

    ``send_test_email`` keeps one sentence for an unset Default From, because
    that configuration does not fail at the transport at all — password reset
    mail is dropped and only a log line records it. Reaching that sentence
    depends entirely on ``not email_config.smtp_from_address`` being true.

    Revert ``return ""`` to ``return value`` and this goes red: ``"   "`` is
    truthy, the probe falls through to ``validate_sender_address`` and reports
    ``Not a usable From: address: '   '`` — a message that reads as "you typed a
    bad address" to an operator who typed none, and names no setting to fix.

    ``deny_network`` pins the other half: the refusal happens before a socket is
    opened, so it is a statement about configuration and not a failed delivery.
    """
    stored = EmailSettingsUpdate(smtp_from_address="   ").smtp_from_address
    assert stored is not None

    with pytest.raises(ValueError) as excinfo:
        send_test_email(email_config=_email_config(stored), recipient="ops@example.com")

    message = str(excinfo.value)
    assert "No default From: address is configured" in message
    assert "Not a usable From: address" not in message


def test_a_usable_sender_is_still_returned_byte_for_byte() -> None:
    """The neighbouring behaviour this fix must not have taken with it.

    ``validate_sender_address`` returns the ORIGINAL string — display name and
    surrounding whitespace included — and both send paths put that string
    straight into ``msg["From"]``. The tempting over-correction here is a
    blanket ``value.strip()``, which would pass every case in this file and
    every case in ``test_alert_send_path`` while quietly rewriting what the operator
    configured; this is the assertion that catches it.

    Unlike the two above, this one also passes with the fix reverted. It guards
    the blast radius, not the fix: the padded display-name form is what the
    trimming version of the change silently alters.
    """
    padded = "  Tripl Alerts <no-reply@example.com>  "
    assert EmailSettingsUpdate(smtp_from_address=padded).smtp_from_address == padded
