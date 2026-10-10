"""One plain message through an alert destination's channel: not an alert delivery.

Two senders reach a destination's channel with no alert behind the message:
the destination's **Test** button (``services/_alerting_test_send``) and an
extension's own notices (the Enterprise escalations). Both call
:func:`send_plain_message`, so a new channel, a changed validator or a
private-host re-check is written once for both. An alert delivery does not
come through here: ``alerts.send_alert_delivery`` threads Telegram replies,
records ticket ids and keys PagerDuty incidents per alert group, none of which
a plain message has.

The request itself goes out through ``worker.tasks.alerts``' channel wrappers
(``alerts._send_*``), the functions a delivery calls, looked up on that module
at send time, so a test that replaces one replaces it here too.

Blocking (urllib, smtplib): from a request, call it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tripl.alert_templates import ALERT_MESSAGE_FORMAT_PLAIN
from tripl.alerting_validation import (
    validate_email_recipients,
    validate_jira_api_token,
    validate_jira_auth_email,
    validate_jira_base_url,
    validate_jira_issue_type,
    validate_jira_project_key,
    validate_linear_api_key,
    validate_linear_team_id,
    validate_pagerduty_routing_key,
    validate_sender_address,
    validate_slack_webhook_url,
    validate_teams_webhook_url,
    validate_telegram_bot_token,
    validate_telegram_chat_id,
    validate_webhook_target_url,
)
from tripl.crypto import decrypt_value
from tripl.models.alert_destination import AlertDestination, AlertDestinationType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# The senders import ``worker.tasks.alerts`` and what it uses when they run: a
# service (``_alerting_test_send``) imports this module at load time, and
# ``alerts`` is in an import cycle with the Celery app that resolves only when
# the Celery app loads first.


def _decrypt(encrypted: str | None) -> str | None:
    return decrypt_value(encrypted) if encrypted else None


@dataclass(frozen=True)
class ChannelTarget:
    """A destination's channel settings, secrets decrypted, no ORM object attached.

    A snapshot rather than the row: the send runs in a worker thread, where an
    instance bound to the request's AsyncSession would lazy-load from the wrong
    thread, and the Test button sends settings that were never saved.
    """

    destination_type: str
    destination_name: str
    webhook_url: str | None = None
    bot_token: str | None = None
    chat_id: str | None = None
    target_url: str | None = None
    webhook_header_name: str | None = None
    webhook_header_value: str | None = None
    email_recipients: str | None = None
    email_from_address: str | None = None
    jira_base_url: str | None = None
    jira_auth_email: str | None = None
    jira_api_token: str | None = None
    jira_project_key: str | None = None
    jira_issue_type: str | None = None
    linear_api_key: str | None = None
    linear_team_id: str | None = None
    linear_state_id: str | None = None
    linear_label_ids: str | None = None
    pagerduty_routing_key: str | None = None
    pagerduty_severity: str | None = None
    teams_webhook_url: str | None = None
    # The organization whose SMTP relay an email goes through (F20 PR9). None
    # sends no email: the send refuses rather than borrow the operator's relay.
    organization_id: uuid.UUID | None = None

    @classmethod
    def from_destination(
        cls, destination: AlertDestination, *, organization_id: uuid.UUID | None
    ) -> ChannelTarget:
        """``destination``'s stored settings, decrypted.

        ``organization_id`` is the organization of the destination's project:
        its relay sends an email.
        """
        return cls(
            destination_type=destination.type,
            destination_name=destination.name,
            webhook_url=_decrypt(destination.webhook_url_encrypted),
            bot_token=_decrypt(destination.bot_token_encrypted),
            chat_id=destination.chat_id,
            target_url=_decrypt(destination.target_url_encrypted),
            webhook_header_name=destination.webhook_header_name,
            webhook_header_value=_decrypt(destination.webhook_header_value_encrypted),
            email_recipients=destination.email_recipients,
            email_from_address=destination.email_from_address,
            jira_base_url=destination.jira_base_url,
            jira_auth_email=destination.jira_auth_email,
            jira_api_token=_decrypt(destination.jira_api_token_encrypted),
            jira_project_key=destination.jira_project_key,
            jira_issue_type=destination.jira_issue_type,
            linear_api_key=_decrypt(destination.linear_api_key_encrypted),
            linear_team_id=destination.linear_team_id,
            linear_state_id=destination.linear_state_id,
            linear_label_ids=destination.linear_label_ids,
            pagerduty_routing_key=_decrypt(destination.pagerduty_routing_key_encrypted),
            pagerduty_severity=destination.pagerduty_severity,
            teams_webhook_url=_decrypt(destination.teams_webhook_url_encrypted),
            organization_id=organization_id,
        )


@dataclass(frozen=True)
class PlainMessage:
    """What one plain send says, in each channel's shape.

    Slack and Telegram send ``text``; email, Jira and Linear send ``subject``
    and ``text``. The other three take a body the caller builds, because each
    caller tells a receiver in its own terms what the message is.
    """

    subject: str
    text: str
    #: The JSON body a generic webhook receives.
    webhook_payload: Mapping[str, object]
    #: The Teams body, as ``alerts_teams.build_teams_plain_message`` builds it.
    teams_message: Mapping[str, object]
    #: PagerDuty Events v2 bodies, sent in order, WITHOUT ``routing_key``: the
    #: send adds the destination's, so a caller never handles the credential.
    pagerduty_events: Sequence[Mapping[str, object]]


def _send_slack(target: ChannelTarget, message: PlainMessage, _session: Session | None) -> None:
    from tripl.worker.tasks import alerts

    alerts._send_slack_message(
        validate_slack_webhook_url(target.webhook_url or ""),
        message.text,
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
    )


def _send_telegram(target: ChannelTarget, message: PlainMessage, _session: Session | None) -> None:
    from tripl.worker.tasks import alerts

    bot_token = validate_telegram_bot_token(target.bot_token or "")
    chat_id = validate_telegram_chat_id(target.chat_id)
    alerts._send_telegram_message(
        bot_token, chat_id, message.text, message_format=ALERT_MESSAGE_FORMAT_PLAIN
    )


def _send_webhook(target: ChannelTarget, message: PlainMessage, _session: Session | None) -> None:
    from tripl.worker.tasks import alerts
    from tripl.worker.tasks.alerts_channels import _reject_private_target

    url = validate_webhook_target_url(target.target_url or "")
    # The DNS-rebinding re-check a delivery makes immediately before the
    # request; a plain send is an equally good way to reach 169.254.169.254.
    _reject_private_target(url, field="Webhook target_url")
    alerts._send_webhook_message(
        url,
        dict(message.webhook_payload),
        header_name=target.webhook_header_name,
        header_value=target.webhook_header_value,
    )


def _send_email(target: ChannelTarget, message: PlainMessage, session: Session | None) -> None:
    from tripl.services import app_settings_service
    from tripl.worker.tasks import alerts
    from tripl.worker.tasks.alerts_channels import _parse_email_recipients

    # The organization's relay (F20 PR9); with no organization, no relay at
    # all. With no session the lookup opens its own short-lived one.
    email_config = (
        app_settings_service.get_email_config_sync(session, org_id=target.organization_id)
        if target.organization_id is not None
        else app_settings_service.disabled_email_config()
    )
    if not email_config.smtp_host:
        raise ValueError(
            "Email destination is configured but SMTP is not — set SMTP_HOST "
            "(and SMTP_USERNAME/SMTP_PASSWORD if your relay requires auth)."
        )
    recipients = _parse_email_recipients(validate_email_recipients(target.email_recipients))
    # The rule a delivery follows (critique #16): the override only on an own relay.
    from_address = app_settings_service.email_sender_for(target.email_from_address, email_config)
    if not from_address:
        raise ValueError("Email destination has no From: address and SMTP_FROM_ADDRESS is unset.")
    alerts._send_email_message(
        smtp_host=email_config.smtp_host,
        smtp_port=email_config.smtp_port,
        smtp_username=email_config.smtp_username,
        smtp_password=email_config.smtp_password,
        smtp_security=email_config.smtp_security,
        from_address=validate_sender_address(from_address),
        recipients=recipients,
        subject=message.subject,
        body=message.text,
    )


def _send_jira(target: ChannelTarget, message: PlainMessage, _session: Session | None) -> None:
    from tripl.worker.tasks import alerts
    from tripl.worker.tasks.alerts_channels import _reject_private_target

    base_url = validate_jira_base_url(target.jira_base_url)
    _reject_private_target(base_url, field="Jira base_url")
    alerts._send_jira_issue(
        base_url=base_url,
        auth_email=validate_jira_auth_email(target.jira_auth_email),
        api_token=validate_jira_api_token(target.jira_api_token or ""),
        project_key=validate_jira_project_key(target.jira_project_key),
        issue_type=validate_jira_issue_type(target.jira_issue_type or "Task"),
        summary=message.subject,
        body_text=message.text,
    )


def _send_linear(target: ChannelTarget, message: PlainMessage, _session: Session | None) -> None:
    from tripl.worker.tasks import alerts

    alerts._send_linear_issue(
        api_key=validate_linear_api_key(target.linear_api_key or ""),
        team_id=validate_linear_team_id(target.linear_team_id),
        title=message.subject,
        body_text=message.text,
        state_id=target.linear_state_id,
        label_ids=(
            [label for label in target.linear_label_ids.split(",") if label]
            if target.linear_label_ids
            else None
        ),
    )


def _send_pagerduty(target: ChannelTarget, message: PlainMessage, _session: Session | None) -> None:
    from tripl.worker.tasks import alerts

    routing_key = validate_pagerduty_routing_key(target.pagerduty_routing_key or "")
    for event in message.pagerduty_events:
        # The key last, so a body cannot aim the event at another service.
        alerts._send_pagerduty_event({**event, "routing_key": routing_key}, routing_key=routing_key)


def _send_teams(target: ChannelTarget, message: PlainMessage, _session: Session | None) -> None:
    from tripl.worker.tasks import alerts
    from tripl.worker.tasks.alerts_channels import _reject_private_target

    url = validate_teams_webhook_url(target.teams_webhook_url or "")
    # The same DNS-rebinding re-check a delivery makes, as for the webhook.
    _reject_private_target(url, field="Teams webhook_url")
    alerts._send_teams_message(url, dict(message.teams_message))


_SENDERS: dict[
    AlertDestinationType, Callable[[ChannelTarget, PlainMessage, Session | None], None]
] = {
    AlertDestinationType.slack: _send_slack,
    AlertDestinationType.telegram: _send_telegram,
    AlertDestinationType.webhook: _send_webhook,
    AlertDestinationType.email: _send_email,
    AlertDestinationType.jira: _send_jira,
    AlertDestinationType.linear: _send_linear,
    AlertDestinationType.pagerduty: _send_pagerduty,
    AlertDestinationType.teams: _send_teams,
}


def send_plain_message(
    target: ChannelTarget, message: PlainMessage, *, session: Session | None = None
) -> None:
    """Validate ``target``, re-check a free-form host, send ``message``; raise on any failure.

    The validators' messages come through as they are: the Test dialog shows
    them. ``session`` serves only an email's relay lookup. A ``demo_sink`` has
    no outside channel and is refused like an unknown type.

    Whether the message may leave at all is the caller's to decide first: the
    demo zero-egress rule (``alerts._assert_egress_allowed``) and, where it
    applies, the destination's ``enabled`` switch, which the Test button
    deliberately ignores.
    """
    sender = _SENDERS.get(AlertDestinationType(target.destination_type))
    if sender is None:
        raise ValueError(f"Unsupported destination type {target.destination_type}")
    sender(target, message, session)
