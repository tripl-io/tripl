"""The plain send (``worker.tasks.alerts_plain``): one message, no alert behind it.

The destination's Test button and an extension's notices (Enterprise
escalations) share it, so a channel added to tripl reaches both, and the checks
a delivery makes before the request (validators, the private-host re-check)
hold for both. Nothing here opens a socket: the channel wrappers on
``worker.tasks.alerts`` are replaced, which is the seam this send uses too.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tripl.crypto import encrypt_value
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.worker.tasks import alerts, alerts_plain
from tripl.worker.tasks.alerts_plain import ChannelTarget, PlainMessage, send_plain_message
from tripl.worker.tasks.alerts_teams import build_teams_plain_message

ROUTING_KEY = "R0uT1nGkEy0123456789abcdefABCDEF"


def _message(**overrides: Any) -> PlainMessage:
    fields: dict[str, Any] = {
        "subject": "Subject",
        "text": "Body",
        "webhook_payload": {"event": "test.plain", "message": "Body"},
        "teams_message": build_teams_plain_message(title="Subject", text="Body"),
        "pagerduty_events": [],
    }
    return PlainMessage(**{**fields, **overrides})


def test_every_outside_channel_has_a_sender() -> None:
    # A new destination type must be wired here, or the Test button and every
    # extension notice refuse it; only the local demo sink has nothing to reach.
    assert set(alerts_plain._SENDERS) == set(AlertDestinationType) - {
        AlertDestinationType.demo_sink
    }


def test_a_demo_sink_is_refused() -> None:
    target = ChannelTarget(destination_type="demo_sink", destination_name="Demo")

    with pytest.raises(ValueError, match="Unsupported destination type demo_sink"):
        send_plain_message(target, _message())


def test_from_destination_decrypts_the_stored_secrets() -> None:
    org_id = uuid.uuid4()
    destination = AlertDestination(
        type=AlertDestinationType.webhook,
        name="Hook",
        target_url_encrypted=encrypt_value("https://hooks.example.com/t?token=abc"),
        webhook_header_name="X-Secret",
        webhook_header_value_encrypted=encrypt_value("s3cret"),
        pagerduty_routing_key_encrypted=None,
    )

    target = ChannelTarget.from_destination(destination, organization_id=org_id)

    assert target.destination_type == "webhook"
    assert target.destination_name == "Hook"
    assert target.target_url == "https://hooks.example.com/t?token=abc"
    assert target.webhook_header_name == "X-Secret"
    assert target.webhook_header_value == "s3cret"
    assert target.pagerduty_routing_key is None
    assert target.organization_id == org_id


def test_pagerduty_events_go_out_in_order_with_the_destinations_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[dict[str, object], str]] = []
    monkeypatch.setattr(
        alerts,
        "_send_pagerduty_event",
        lambda body, *, routing_key: sent.append((body, routing_key)),
    )
    target = ChannelTarget(
        destination_type="pagerduty", destination_name="On call", pagerduty_routing_key=ROUTING_KEY
    )
    # A body cannot aim the event at another service: the key is the destination's.
    events = [
        {"event_action": "trigger", "dedup_key": "k-1", "routing_key": "someone-elses"},
        {"event_action": "resolve", "dedup_key": "k-1"},
    ]

    send_plain_message(target, _message(pagerduty_events=events))

    assert [(body["event_action"], body["routing_key"], key) for body, key in sent] == [
        ("trigger", ROUTING_KEY, ROUTING_KEY),
        ("resolve", ROUTING_KEY, ROUTING_KEY),
    ]


def test_each_channel_gets_its_own_shape_of_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        alerts,
        "_send_slack_message",
        lambda url, text, *, message_format: calls.append(("slack", text)),
    )
    monkeypatch.setattr(
        alerts,
        "_send_linear_issue",
        lambda **kwargs: calls.append(("linear", (kwargs["title"], kwargs["label_ids"]))),
    )
    message = _message()

    send_plain_message(
        ChannelTarget(
            destination_type="slack",
            destination_name="Ops",
            webhook_url="https://hooks.slack.com/services/T000/B000/XXXX",
        ),
        message,
    )
    send_plain_message(
        ChannelTarget(
            destination_type="linear",
            destination_name="Ops",
            linear_api_key="lin_api_0123456789abcdef",
            linear_team_id="b7a5f3c1-0000-4000-8000-000000000000",
            linear_label_ids="l-1,,l-2",
        ),
        message,
    )

    assert calls == [("slack", "Body"), ("linear", ("Subject", ["l-1", "l-2"]))]


def test_a_validators_own_message_comes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    # The Test dialog shows it as it is, so it is not wrapped.
    monkeypatch.setattr(alerts, "_send_slack_message", lambda *a, **k: pytest.fail("sent"))
    target = ChannelTarget(
        destination_type="slack", destination_name="Ops", webhook_url="http://example.com/hook"
    )

    with pytest.raises(ValueError, match="Slack webhook_url"):
        send_plain_message(target, _message())


def test_an_email_with_no_organization_borrows_no_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts, "_send_email_message", lambda **_k: pytest.fail("sent"))
    target = ChannelTarget(
        destination_type="email", destination_name="Ops", email_recipients="ops@example.com"
    )

    with pytest.raises(ValueError, match="SMTP is not"):
        send_plain_message(target, _message())


def test_the_teams_plain_card_carries_the_link_only_when_there_is_one() -> None:
    with_link = build_teams_plain_message(title="T", text="Body", link="https://app.example/i/1")
    without = build_teams_plain_message(title="T", text="Body")

    card = with_link["attachments"][0]["content"]  # type: ignore[index]
    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open in tripl", "url": "https://app.example/i/1"}
    ]
    assert "actions" not in without["attachments"][0]["content"]  # type: ignore[index]
