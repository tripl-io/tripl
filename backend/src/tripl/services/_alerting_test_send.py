"""Test send — prove a destination actually reaches its channel.

"bot token set" and "chat -100..." say a value is STORED, not that anything
arrives: a revoked token, a webhook whose channel was archived and a healthy
destination all render identically in the form. This module
sends one fixed, clearly-marked message through the destination's real channel
so the two can be told apart.

No ``AlertDelivery`` row is written for a test. That is a deliberate choice
rather than a shortcut: ``AlertDelivery.rule_id`` and ``.scan_config_id`` are
both NOT NULL, so a test row could only exist by borrowing a real rule and a
real scan and claiming they fired — which is precisely the lie the Delivery log
must not tell, and it would also stamp ``AlertRuleState.last_notified_at`` and
silence the next genuine alert through that rule's cooldown. The operator action
is recorded where operator actions belong, in the audit log, by the route.

The actual sending is ``worker.tasks.alerts_plain.send_plain_message``, which
goes through ``worker.tasks.alerts``' channel wrappers — the same functions
``send_alert_delivery`` calls — so a destination that passes here passes for the
same reasons a real delivery would, and there is only one place where a
channel's request shape is defined. This module owns what is the test's own:
the draft's settings merged with the stored secrets, and the message itself.
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
from tripl.worker.tasks.alerts_plain import ChannelTarget, PlainMessage, send_plain_message

logger = logging.getLogger(__name__)

TEST_MESSAGE_SUBJECT = "tripl test message"


def _test_message(*, project_name: str, destination_name: str) -> PlainMessage:
    """The one message a test send emits. Fixed, and unmistakably a test.

    Whoever reads the channel did not ask for this, so the text has to say on its
    own line that nothing is wrong — an operator paging on a message that merely
    LOOKS like an alert is a worse outcome than never testing.
    """
    from tripl.worker.tasks.alerts_pagerduty import (
        PAGERDUTY_SOURCE,
        build_test_trigger_and_resolve,
    )
    from tripl.worker.tasks.alerts_teams import build_teams_test_message

    text = (
        f"{TEST_MESSAGE_SUBJECT}\n"
        f"Project: {project_name}\n"
        f"Destination: {destination_name}\n"
        "Someone pressed Test in tripl to check that this channel is reachable. "
        "No alert fired and nothing is wrong."
    )
    return PlainMessage(
        subject=TEST_MESSAGE_SUBJECT,
        text=text,
        webhook_payload={
            # A receiver that switches on `event` must be able to drop this
            # without parsing prose, so the test is typed, not just worded.
            "event": "tripl.destination_test",
            "destination": destination_name,
            "message": text,
        },
        teams_message=build_teams_test_message(destination_name=destination_name, message=text),
        # A test must not leave someone paged: the trigger is resolved straight
        # away under the same, test-only dedup key. It still reaches the on-call
        # phone for the moment it is open — that is the only way to prove the key
        # routes somewhere — and its summary says it is a test.
        pagerduty_events=build_test_trigger_and_resolve(
            summary=f"{TEST_MESSAGE_SUBJECT}: {destination_name} (no alert fired)",
            component=PAGERDUTY_SOURCE,
            message=text,
        ),
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


def _decrypt(encrypted: str | None) -> str | None:
    if not encrypted:
        return None
    return decrypt_value(encrypted)


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
    if draft.type == AlertDestinationType.teams:
        if draft.teams_webhook_url is not None:
            return _url_origin(draft.teams_webhook_url)
        if stored is not None:
            return _url_origin(_decrypt(stored.teams_webhook_url_encrypted))
    return None


def _build_draft_target(
    draft: AlertDestinationDraftTestRequest,
    stored: AlertDestination | None,
    *,
    destination_name: str,
    organization_id: uuid.UUID | None,
) -> ChannelTarget:
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
    return ChannelTarget(
        destination_type=draft.type,
        destination_name=destination_name,
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
        pagerduty_routing_key=secret(
            draft.pagerduty_routing_key,
            stored.pagerduty_routing_key_encrypted if stored is not None else None,
        ),
        pagerduty_severity=draft.pagerduty_severity,
        teams_webhook_url=secret(
            draft.teams_webhook_url,
            stored.teams_webhook_url_encrypted if stored is not None else None,
        ),
        organization_id=organization_id,
    )


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
    """A test send's failure as a kind (+ HTTP status), for the dialog.

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
        build_target=lambda: ChannelTarget.from_destination(
            destination, organization_id=project.organization_id
        ),
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
    """Test the settings a destination dialog holds, before they are saved.

    The same send, the same checks and the same answer as a saved destination's
    Test: the demo zero-egress predicate, the channel validators and, for the
    free-form URLs (webhook, Jira, Teams), the private-host refusal the plain
    send runs immediately before the request. Nothing is written — the point is
    to learn a webhook is wrong BEFORE it is a stored destination.
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
            organization_id=project.organization_id,
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
    build_target: Callable[[], ChannelTarget],
    log_ref: object,
) -> AlertDestinationTestResponse:
    """The send both Test buttons share, from the channel check to the answer."""
    now = datetime.now(UTC)

    # A demo_sink has no outside to reach: it renders and records locally, which
    # is exactly what a real delivery through it does. Reporting ok here is the
    # truthful answer — "this destination works as configured" — and it keeps the
    # button from looking broken on the one destination a demo project may own.
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
    message = _test_message(project_name=project.name, destination_name=target.destination_name)
    try:
        # Every channel client blocks (urllib, smtplib), so it must not run on the
        # request's event loop.
        await asyncio.to_thread(send_plain_message, target, message)
    except Exception as exc:  # noqa: BLE001
        # Deliberately broad, and deliberately not a 5xx. What comes back from a
        # channel is a ValueError from our own validators, a urllib/socket error,
        # an smtplib error or whatever a third-party client raises — enumerating
        # that set would mean a NEW channel's failure becomes a 500 the day it is
        # added, and a 500 tells the operator "Tripl is broken" when the correct
        # reading is "your token is". The message is already secret-safe:
        # _safe_url_for_error strips everything but scheme+host.
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
