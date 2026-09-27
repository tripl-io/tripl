"""Test send — prove a destination actually reaches its channel.

"bot token set" and "chat -100..." say a value is STORED, not that anything
arrives: a revoked token, a webhook whose channel was archived and a healthy
destination all render identically in the form (tripl-oxkt.17). This module
sends one fixed, clearly-marked message through the destination's real channel
so the two can be told apart.

No ``AlertDelivery`` row is written for a test. That is a deliberate choice
rather than a shortcut: ``AlertDelivery.rule_id`` and ``.scan_config_id`` are
both NOT NULL, so a test row could only exist by borrowing a real rule and a
real scan and claiming they fired — which is precisely the lie the Delivery log
must not tell, and it would also stamp ``AlertRuleState.last_notified_at`` and
silence the next genuine alert through that rule's cooldown. The operator action
is recorded where operator actions belong, in the audit log, by the route.

The actual sending goes through ``worker.tasks.alerts``' channel wrappers — the
same functions ``send_alert_delivery`` calls — so a destination that passes here
passes for the same reasons a real delivery would, and there is only one place
where a channel's request shape is defined.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import socket
import ssl
import urllib.error
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

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
    validate_sender_address,
    validate_slack_webhook_url,
    validate_telegram_bot_token,
    validate_telegram_chat_id,
    validate_webhook_target_url,
)
from tripl.crypto import decrypt_value
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.project import Project
from tripl.schemas.alerting import (
    AlertDestinationDraftTestRequest,
    AlertDestinationTestResponse,
    DestinationTestErrorKind,
)
from tripl.services._alerting_destinations import get_destination
from tripl.services.project_lookup import resolve_project as _get_project

logger = logging.getLogger(__name__)

TEST_MESSAGE_SUBJECT = "Tripl test message"


def _test_message(*, project_name: str, destination_name: str) -> str:
    """The one message a test send emits. Fixed, and unmistakably a test.

    Whoever reads the channel did not ask for this, so the text has to say on its
    own line that nothing is wrong — an operator paging on a message that merely
    LOOKS like an alert is a worse outcome than never testing.
    """
    return (
        f"{TEST_MESSAGE_SUBJECT}\n"
        f"Project: {project_name}\n"
        f"Destination: {destination_name}\n"
        "Someone pressed Test in Tripl to check that this channel is reachable. "
        "No alert fired and nothing is wrong."
    )


@dataclass(frozen=True)
class DestinationTestOutcome:
    """The test result plus the destination's name, from the one load that ran.

    The route has to name the destination in the audit entry, and used to get
    that name by calling the service's ``get_destination`` first — which is
    ``get_destination_response``, so naming a destination cost the four
    delete-impact aggregates of ``load_destination_health`` and a second load of
    the row this function already holds. Nine queries to send one message, on a
    path that then blocks on a 10s network call.

    A wrapper rather than a field on ``AlertDestinationTestResponse``: the name
    is something the caller already knows and no client asked for it, so it stays
    out of the public contract.
    """

    response: AlertDestinationTestResponse
    destination_name: str
    # Scheme and host of a free-form target (webhook, Jira) the test was aimed
    # at, for the audit entry. A draft's URL is caller-chosen and nothing of it
    # is persisted, so without this the audit log could not say where a test —
    # and whatever stored secret it carried — was sent.
    target_origin: str | None = None


@dataclass(frozen=True)
class _TestTarget:
    """Everything the send needs, decrypted, with no ORM object attached.

    The send runs in a worker thread (``asyncio.to_thread``) because every
    channel client is blocking — urllib and smtplib. Handing a thread a
    SQLAlchemy instance bound to the request's AsyncSession would be a lazy-load
    from the wrong thread, so the snapshot is taken on the event loop first.
    """

    # None for a draft that was never saved (AL-30).
    destination_id: uuid.UUID | None
    destination_type: str
    destination_name: str
    message: str
    webhook_url: str | None
    bot_token: str | None
    chat_id: str | None
    target_url: str | None
    webhook_header_name: str | None
    webhook_header_value: str | None
    email_recipients: str | None
    email_from_address: str | None
    jira_base_url: str | None
    jira_auth_email: str | None
    jira_api_token: str | None
    jira_project_key: str | None
    jira_issue_type: str | None
    linear_api_key: str | None
    linear_team_id: str | None
    linear_state_id: str | None
    linear_label_ids: str | None


def _decrypt(encrypted: str | None) -> str | None:
    if not encrypted:
        return None
    return decrypt_value(encrypted)


def _build_target(destination: AlertDestination, *, project_name: str) -> _TestTarget:
    return _TestTarget(
        destination_id=destination.id,
        destination_type=destination.type,
        destination_name=destination.name,
        message=_test_message(
            project_name=project_name,
            destination_name=destination.name,
        ),
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
    )


def _url_origin(url: str | None) -> str | None:
    """``scheme://host[:port]`` of ``url``, lowercased; None when it has neither."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    try:
        port_number = parsed.port
    except ValueError:
        # An out-of-range port: the netloc as typed still names the target.
        return f"{parsed.scheme.lower()}://{parsed.netloc.rsplit('@', 1)[-1].lower()}"
    host = (parsed.hostname or "").lower()
    port = f":{port_number}" if port_number is not None else ""
    return f"{parsed.scheme.lower()}://{host}{port}"


class _SecretBorrowRefused(ValueError):
    """A draft would carry a stored secret to a host it was never saved for."""


def _check_secret_stays_home(
    draft: AlertDestinationDraftTestRequest, stored: AlertDestination
) -> None:
    """Refuse to lend a stored secret to a draft aimed at a different host.

    A blank secret in an edit dialog means "the one on file", but that secret
    was entrusted to one Jira site or one webhook host. A draft that changes the
    host and leaves the secret blank would send the stored credential to
    wherever the caller typed — and a test persists nothing, so the audit trail
    would be all that is left of it. The operator re-types the secret to test a
    new host, exactly as they would have to know it to point a real destination
    there.
    """
    if (
        draft.type == AlertDestinationType.jira
        and draft.jira_api_token is None
        and stored.jira_api_token_encrypted
        and _url_origin(draft.jira_base_url) != _url_origin(stored.jira_base_url)
    ):
        raise _SecretBorrowRefused(
            "The stored Jira API token is only sent to the Jira site it was saved "
            "for. Enter the API token again to test a different Jira site."
        )
    if (
        draft.type == AlertDestinationType.webhook
        and draft.webhook_header_name is not None
        and draft.webhook_header_value is None
        and draft.target_url is not None
        and stored.webhook_header_value_encrypted
        and _url_origin(draft.target_url) != _url_origin(_decrypt(stored.target_url_encrypted))
    ):
        # The stored target URL is itself a secret, so the message does not
        # name the host it is compared with.
        raise _SecretBorrowRefused(
            "The stored header value is only sent to the webhook host it was saved "
            "for. Enter the header value again to test a different host."
        )


def _draft_target_origin(
    draft: AlertDestinationDraftTestRequest, stored: AlertDestination | None
) -> str | None:
    """Where a draft's free-form URL points, scheme and host only (for audit)."""
    if draft.type == AlertDestinationType.jira:
        return _url_origin(draft.jira_base_url)
    if draft.type == AlertDestinationType.webhook:
        if draft.target_url is not None:
            return _url_origin(draft.target_url)
        if stored is not None:
            return _url_origin(_decrypt(stored.target_url_encrypted))
    return None


def _build_draft_target(
    draft: AlertDestinationDraftTestRequest,
    stored: AlertDestination | None,
    *,
    destination_name: str,
    project_name: str,
) -> _TestTarget:
    """A test target from the dialog's settings, secrets filled from ``stored``.

    Only the write-only fields fall back, because they are the only ones the
    dialog cannot show: a blank token in an edit dialog means "keep the one on
    file", which is what the PATCH reads it as, so the test must send with it.
    Every other field is the form's — it was loaded from the row and is what
    Save would write, so testing the stored value instead would test something
    the operator is about to replace.

    The header secret follows its name, as on update: a header the form removed
    goes out with neither half, and a kept name with a blank value sends the
    stored value — but only to the host it was stored for, and the Jira token
    only to its own site (``_check_secret_stays_home``).
    """
    if stored is not None:
        _check_secret_stays_home(draft, stored)

    def secret(value: str | None, encrypted: str | None) -> str | None:
        if value is not None:
            return value
        return _decrypt(encrypted) if stored is not None else None

    header_value = (
        secret(
            draft.webhook_header_value,
            stored.webhook_header_value_encrypted if stored is not None else None,
        )
        if draft.webhook_header_name is not None
        else None
    )
    return _TestTarget(
        destination_id=stored.id if stored is not None else None,
        destination_type=draft.type,
        destination_name=destination_name,
        message=_test_message(project_name=project_name, destination_name=destination_name),
        webhook_url=secret(
            draft.webhook_url, stored.webhook_url_encrypted if stored is not None else None
        ),
        bot_token=secret(
            draft.bot_token, stored.bot_token_encrypted if stored is not None else None
        ),
        chat_id=draft.chat_id,
        target_url=secret(
            draft.target_url, stored.target_url_encrypted if stored is not None else None
        ),
        webhook_header_name=draft.webhook_header_name,
        webhook_header_value=header_value,
        email_recipients=draft.email_recipients,
        email_from_address=draft.email_from_address,
        jira_base_url=draft.jira_base_url,
        jira_auth_email=draft.jira_auth_email,
        jira_api_token=secret(
            draft.jira_api_token, stored.jira_api_token_encrypted if stored is not None else None
        ),
        jira_project_key=draft.jira_project_key,
        jira_issue_type=draft.jira_issue_type,
        linear_api_key=secret(
            draft.linear_api_key, stored.linear_api_key_encrypted if stored is not None else None
        ),
        linear_team_id=draft.linear_team_id,
        linear_state_id=draft.linear_state_id,
        linear_label_ids=draft.linear_label_ids,
    )


def _send_slack(target: _TestTarget) -> None:
    from tripl.worker.tasks import alerts

    webhook_url = validate_slack_webhook_url(target.webhook_url or "")
    alerts._send_slack_message(
        webhook_url,
        target.message,
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
    )


def _send_telegram(target: _TestTarget) -> None:
    from tripl.worker.tasks import alerts

    bot_token = validate_telegram_bot_token(target.bot_token or "")
    chat_id = validate_telegram_chat_id(target.chat_id)
    alerts._send_telegram_message(
        bot_token,
        chat_id,
        target.message,
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
    )


def _send_webhook(target: _TestTarget) -> None:
    from tripl.worker.tasks import alerts
    from tripl.worker.tasks.alerts_channels import _reject_private_target

    url = validate_webhook_target_url(target.target_url or "")
    # Same DNS-rebinding re-check the real send does immediately before the
    # request; a test send is an equally good way to reach 169.254.169.254.
    _reject_private_target(url, field="Webhook target_url")
    alerts._send_webhook_message(
        url,
        {
            # A receiver that switches on `event` must be able to drop this
            # without parsing prose, so the test is typed, not just worded.
            "event": "tripl.destination_test",
            "destination": target.destination_name,
            "message": target.message,
        },
        header_name=target.webhook_header_name,
        header_value=target.webhook_header_value,
    )


def _send_email(target: _TestTarget) -> None:
    from tripl.services import app_settings_service
    from tripl.worker.tasks import alerts
    from tripl.worker.tasks.alerts_channels import _parse_email_recipients

    # No session argument: this runs off the event loop, so it opens its own
    # short-lived sync session exactly as the worker does.
    email_config = app_settings_service.get_email_config_sync()
    if not email_config.smtp_host:
        raise ValueError(
            "Email destination is configured but SMTP is not — set SMTP_HOST "
            "(and SMTP_USERNAME/SMTP_PASSWORD if your relay requires auth)."
        )
    recipients = _parse_email_recipients(validate_email_recipients(target.email_recipients))
    from_address = target.email_from_address or email_config.smtp_from_address
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
        subject=TEST_MESSAGE_SUBJECT,
        body=target.message,
    )


def _send_jira(target: _TestTarget) -> None:
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
        summary=TEST_MESSAGE_SUBJECT,
        body_text=target.message,
    )


def _send_linear(target: _TestTarget) -> None:
    from tripl.worker.tasks import alerts

    alerts._send_linear_issue(
        api_key=validate_linear_api_key(target.linear_api_key or ""),
        team_id=validate_linear_team_id(target.linear_team_id),
        title=TEST_MESSAGE_SUBJECT,
        body_text=target.message,
        state_id=target.linear_state_id,
        label_ids=(
            [label for label in target.linear_label_ids.split(",") if label]
            if target.linear_label_ids
            else None
        ),
    )


_SENDERS: dict[AlertDestinationType, Callable[[_TestTarget], None]] = {
    AlertDestinationType.slack: _send_slack,
    AlertDestinationType.telegram: _send_telegram,
    AlertDestinationType.webhook: _send_webhook,
    AlertDestinationType.email: _send_email,
    AlertDestinationType.jira: _send_jira,
    AlertDestinationType.linear: _send_linear,
}


def _send_test_message(target: _TestTarget) -> None:
    sender = _SENDERS.get(AlertDestinationType(target.destination_type))
    if sender is None:
        raise ValueError(f"Unsupported destination type {target.destination_type}")
    sender(target)


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and every exception it was raised from, outermost first.

    The channel clients wrap transport errors in a readable ``ValueError``
    (``raise ValueError(...) from exc``), so the type that says what went wrong
    sits on ``__cause__``. A URLError's ``reason`` is walked too: it is where
    urllib keeps the socket or TLS error.
    """
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        reason = (
            getattr(current, "reason", None) if isinstance(current, urllib.error.URLError) else None
        )
        current = (
            reason
            if isinstance(reason, BaseException)
            else current.__cause__ or current.__context__
        )
    return chain


def classify_test_send_error(exc: BaseException) -> tuple[DestinationTestErrorKind, int | None]:
    """A test send's failure as a kind (+ HTTP status), for the dialog (AL-30).

    Most specific first: an HTTP answer beats the socket it came over, a TLS or
    DNS failure beats the generic OSError both subclass. A bare ``ValueError``
    with nothing behind it is one of our own validators refusing a stored value.
    """
    chain = _exception_chain(exc)
    for item in chain:
        if isinstance(item, urllib.error.HTTPError):
            return "http_status", item.code
    for item in chain:
        if isinstance(item, (ssl.SSLError, ssl.CertificateError)):
            return "tls", None
        if isinstance(item, socket.gaierror):
            return "dns", None
        if isinstance(item, TimeoutError):
            return "timeout", None
        if isinstance(item, smtplib.SMTPException):
            return "smtp", None
    for item in chain:
        if isinstance(item, (urllib.error.URLError, OSError)):
            return "network", None
    if len(chain) == 1 and isinstance(exc, ValueError):
        return "config", None
    return "other", None


async def send_destination_test(
    session: AsyncSession,
    slug: str,
    destination_id: uuid.UUID,
) -> DestinationTestOutcome:
    """Send one test message and report whether the channel took it."""
    project = await _get_project(session, slug)
    destination = await get_destination(
        session,
        project_id=project.id,
        destination_id=destination_id,
    )
    destination_name = destination.name
    # A DISABLED destination is still tested. Disabled means "route no alerts
    # here", and the commonest reason to press Test is to check credentials
    # before switching a destination back on — refusing would make the button
    # useless exactly when it is most wanted. The send is an explicit,
    # editor-only, one-message action, not routing.
    response = await _run_test_send(
        project=project,
        policy_subject=destination,
        build_target=lambda: _build_target(destination, project_name=project.name),
        log_ref=destination_id,
    )
    return DestinationTestOutcome(response=response, destination_name=destination_name)


#: What a draft with no name is called in the test message and the audit log.
UNSAVED_DESTINATION_NAME = "Unsaved destination"


async def send_draft_destination_test(
    session: AsyncSession,
    slug: str,
    draft: AlertDestinationDraftTestRequest,
) -> DestinationTestOutcome:
    """Test the settings a destination dialog holds, before they are saved (AL-30).

    The same send, the same checks and the same answer as a saved destination's
    Test: the demo zero-egress predicate, the channel validators and, for the
    two free-form URLs, the private-host refusal ``_send_webhook`` and
    ``_send_jira`` run immediately before the request. Nothing is written — the
    point is to learn a webhook is wrong BEFORE it is a stored destination.
    """
    project = await _get_project(session, slug)
    stored: AlertDestination | None = None
    if draft.destination_id is not None:
        # Project-scoped like every other destination read: an id from another
        # project is a 404 here, never a way to borrow that project's secrets.
        stored = await get_destination(
            session,
            project_id=project.id,
            destination_id=draft.destination_id,
        )
        if stored.type != draft.type:
            # A saved destination's channel is fixed; mixing one channel's form
            # with another's stored secrets would test something that cannot
            # exist.
            raise HTTPException(
                status_code=422,
                detail=(
                    f"This destination is a {stored.type} destination; its channel "
                    "cannot change, so it cannot be tested as another one."
                ),
            )
    destination_name = draft.name or (
        stored.name if stored is not None else UNSAVED_DESTINATION_NAME
    )
    # The egress predicate reads the destination's type and name only. A draft
    # has no row, so it is judged as the transient row it would become — never
    # added to the session, so nothing can flush it.
    policy_subject = stored or AlertDestination(
        project_id=project.id, type=draft.type, name=destination_name
    )
    response = await _run_test_send(
        project=project,
        policy_subject=policy_subject,
        build_target=lambda: _build_draft_target(
            draft,
            stored,
            destination_name=destination_name,
            project_name=project.name,
        ),
        log_ref=draft.destination_id or "draft",
    )
    return DestinationTestOutcome(
        response=response,
        destination_name=destination_name,
        target_origin=_draft_target_origin(draft, stored),
    )


async def _run_test_send(
    *,
    project: Project,
    policy_subject: AlertDestination,
    build_target: Callable[[], _TestTarget],
    log_ref: object,
) -> AlertDestinationTestResponse:
    """The send both Test buttons share, from the channel check to the answer."""
    now = datetime.now(UTC)

    # A demo_sink has no outside to reach: it renders and records locally, which
    # is exactly what a real delivery through it does. Reporting ok here is the
    # truthful answer — "this destination works as configured" — and it keeps the
    # button from looking broken on the one destination a demo project may own
    # (tripl-2su6.6).
    if policy_subject.type == AlertDestinationType.demo_sink:
        return AlertDestinationTestResponse(ok=True, error=None, sent_at=now)

    # A test send is still egress. Derive its readable refusal from the same
    # predicate used by actual delivery tasks, keeping this answer aligned when
    # the zero-egress policy changes. Import here to avoid worker/service cycles.
    from tripl.worker.tasks.alerts import _assert_egress_allowed

    try:
        _assert_egress_allowed(policy_subject, project)
    except ValueError as exc:
        return AlertDestinationTestResponse(
            ok=False,
            error=f"Demo projects cannot send external alerts. {exc}",
            # Nothing was sent, so there is no instant to report — but the
            # key is still present, because the response type says it is.
            sent_at=None,
            error_kind="policy",
        )

    # Built after the policy check, so a refused send never decrypts anything.
    try:
        target = build_target()
    except _SecretBorrowRefused as exc:
        return AlertDestinationTestResponse(
            ok=False, error=str(exc), sent_at=None, error_kind="config"
        )
    try:
        # Every channel client blocks (urllib, smtplib), so it must not run on the
        # request's event loop.
        await asyncio.to_thread(_send_test_message, target)
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad, and deliberately not a 5xx. What comes back from a
        # channel is a ValueError from our own validators, a urllib/socket error,
        # an smtplib error or whatever a third-party client raises — enumerating
        # that set would mean a NEW channel's failure becomes a 500 the day it is
        # added, and a 500 tells the operator "Tripl is broken" when the correct
        # reading is "your token is". The message is already secret-safe:
        # _safe_url_for_error strips everything but scheme+host (tripl-jfm3.94).
        logger.warning(
            "Destination test send failed for %s (%s)",
            log_ref,
            policy_subject.type,
            exc_info=True,
        )
        error_kind, http_status = classify_test_send_error(exc)
        return AlertDestinationTestResponse(
            ok=False,
            error=str(exc),
            sent_at=None,
            error_kind=error_kind,
            http_status=http_status,
        )

    return AlertDestinationTestResponse(ok=True, error=None, sent_at=datetime.now(UTC))
