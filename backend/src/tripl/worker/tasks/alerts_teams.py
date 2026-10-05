"""Microsoft Teams: one Adaptive Card per delivery, posted to a webhook URL.

Teams has two ways in, and both take this body. The retired-but-still-common
Office 365 connector ("incoming webhook", ``*.webhook.office.com``) and its
replacement, a Power Automate / Workflows "When a Teams webhook request is
received" flow, each accept a ``message`` whose attachment is an Adaptive Card.
The older ``MessageCard`` format reaches only the first, which is why it is not
used here.

The card carries the PLAIN rendered message — the same text an email gets —
rather than a markup dialect: the rule's template is written for one of the
formats ``alert_templates`` knows, and Teams speaks none of them reliably (an
Adaptive Card ``TextBlock`` renders a Markdown subset, so ``*`` and ``_`` in a
scope name would turn into formatting). The facts above it are the at-a-glance
part a phone notification shows.

Card schema 1.4 because it is the highest version both entry points render on
desktop and mobile today; nothing below needs anything newer.
"""

from __future__ import annotations

from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.project import Project
from tripl.worker.tasks.alerts_pagerduty import first_app_link

ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
ADAPTIVE_CARD_VERSION = "1.4"
# Teams refuses a whole message above ~28 KB. The rendered message is the only
# part of the card that grows with the delivery, so it alone is capped; the
# full text stays in the delivery's ``payload_snapshot`` in tripl.
TEAMS_MESSAGE_MAX_CHARS = 20_000


def build_teams_card_message(
    delivery: AlertDelivery,
    *,
    destination: AlertDestination,
    rule: AlertRule,
    scan_name: str,
    project: Project | None,
    message: str,
    title: str | None = None,
) -> dict[str, object]:
    """The ``{"type": "message", "attachments": [card]}`` body Teams accepts."""
    prefix = project.name if project else "tripl"
    heading = title or f"[{prefix}] {rule.name} — {delivery.matched_count} alert(s)"
    body_text = (
        message
        if len(message) <= TEAMS_MESSAGE_MAX_CHARS
        else message[: TEAMS_MESSAGE_MAX_CHARS - 1] + "…"
    )
    facts = [
        {"title": "Project", "value": prefix},
        {"title": "Rule", "value": rule.name},
        {"title": "Scan", "value": scan_name},
        {"title": "Destination", "value": destination.name},
        {"title": "Alerts", "value": str(delivery.matched_count)},
    ]
    card: dict[str, object] = {
        "$schema": ADAPTIVE_CARD_SCHEMA,
        "type": "AdaptiveCard",
        "version": ADAPTIVE_CARD_VERSION,
        "body": [
            {
                "type": "TextBlock",
                "text": heading,
                "weight": "Bolder",
                "size": "Medium",
                "wrap": True,
            },
            {"type": "FactSet", "facts": facts},
            # ``wrap`` or Teams shows the first line and an ellipsis. Not
            # ``isSubtle``: this is the alert itself, not a footnote.
            {"type": "TextBlock", "text": body_text, "wrap": True, "fontType": "Monospace"},
        ],
    }
    link = first_app_link(delivery.items)
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in tripl", "url": link}]
    return _wrap(card)


def build_teams_test_message(*, destination_name: str, message: str) -> dict[str, object]:
    """The Test button's card: same envelope, nothing that looks like an alert."""
    card: dict[str, object] = {
        "$schema": ADAPTIVE_CARD_SCHEMA,
        "type": "AdaptiveCard",
        "version": ADAPTIVE_CARD_VERSION,
        "body": [
            {
                "type": "TextBlock",
                "text": f"Tripl test message — {destination_name}",
                "weight": "Bolder",
                "wrap": True,
            },
            {"type": "TextBlock", "text": message, "wrap": True},
        ],
    }
    return _wrap(card)


def _wrap(card: dict[str, object]) -> dict[str, object]:
    return {
        "type": "message",
        # ``contentUrl: null`` is what the Office 365 connector's own samples
        # send; the Workflows trigger ignores it, the connector wants the key.
        "attachments": [
            {"contentType": ADAPTIVE_CARD_CONTENT_TYPE, "contentUrl": None, "content": card}
        ],
    }
