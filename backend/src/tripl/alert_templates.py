from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from tripl.models.alert_destination import AlertDestinationType
from tripl.models.domain_enums import AlertDriftType, AlertMessageFormat, MetricScopeType

ALERT_MESSAGE_FORMAT_PLAIN = AlertMessageFormat.plain.value
ALERT_MESSAGE_FORMAT_SLACK_MRKDWN = AlertMessageFormat.slack_mrkdwn.value
ALERT_MESSAGE_FORMAT_TELEGRAM_HTML = AlertMessageFormat.telegram_html.value
ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2 = AlertMessageFormat.telegram_markdownv2.value

ALERT_MESSAGE_FORMATS_BY_DESTINATION: dict[str, tuple[str, ...]] = {
    AlertDestinationType.slack: (
        ALERT_MESSAGE_FORMAT_PLAIN,
        ALERT_MESSAGE_FORMAT_SLACK_MRKDWN,
    ),
    AlertDestinationType.telegram: (
        ALERT_MESSAGE_FORMAT_PLAIN,
        ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
        ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
    ),
}

DEFAULT_ALERT_MESSAGE_TEMPLATES: dict[str, str] = {
    ALERT_MESSAGE_FORMAT_PLAIN: (
        "[tripl] ${matched_count} alerts\n"
        "Project delivery via ${channel}: ${destination_name}\n"
        "Rule: ${rule_name}\n"
        "Scan: ${scan_name}\n\n"
        "${items_text}"
    ),
    ALERT_MESSAGE_FORMAT_SLACK_MRKDWN: (
        "*[tripl] ${matched_count} alerts*\n"
        "Project delivery via ${channel}: ${destination_name}\n"
        "Rule: *${rule_name}*\n"
        "Scan: `${scan_name}`\n\n"
        "${items_text}"
    ),
    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML: (
        "<b>[tripl] ${matched_count} alerts</b>\n"
        "Project delivery via ${channel}: ${destination_name}\n"
        "Rule: <b>${rule_name}</b>\n"
        "Scan: <code>${scan_name}</code>\n\n"
        "${items_text}"
    ),
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2: (
        "*tripl: ${matched_count} alerts*\n"
        "Project delivery via ${channel}: ${destination_name}\n"
        "Rule: *${rule_name}*\n"
        "Scan: `${scan_name}`\n\n"
        "${items_text}"
    ),
}

# ``${percent_delta_label}`` rather than a bare ``${percent_delta}%``: the label
# carries its own unit, so it can say "no baseline" where a percentage has
# nothing to divide by. For every item that HAS a baseline it renders exactly the
# text these templates produced before (see ``format_percent_delta``).
#
# ``${expected_basis}`` sits ON the expected number rather than a line below it.
# One scope computes ``expected`` differently from every other: a release
# regression's expectation is the PREVIOUS release's share of the scope applied
# to the NEW release's own volume, so it shrinks when adoption is low and the
# comparison is share-against-share. Printed bare next to ``actual``, it reads
# as a raw count of the same thing, and the first question a reader asks is
# "so what if it's lower, not everyone updated yet?" — an objection the
# normalization has already answered. The qualifier renders empty for every
# other scope, so nothing else moves.
DEFAULT_ALERT_ITEMS_TEMPLATES: dict[str, str] = {
    ALERT_MESSAGE_FORMAT_PLAIN: (
        "- ${scope_label} ${scope_name}: ${direction_label}, "
        "actual=${actual_count}, expected=${expected_count}${expected_basis}, "
        "delta=${absolute_delta} (${percent_delta_label})"
        "${drift_line}${details_line}${monitoring_line}${attribution_line}"
        "${top_movers_line}${sparkline_line}"
    ),
    ALERT_MESSAGE_FORMAT_SLACK_MRKDWN: (
        "- ${scope_label} ${scope_name}: ${direction_label}, "
        "actual=${actual_count}, expected=${expected_count}${expected_basis}, "
        "delta=${absolute_delta} (${percent_delta_label})"
        "${drift_line}${details_line}${monitoring_line}${attribution_line}"
        "${top_movers_line}${sparkline_line}"
    ),
    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML: (
        "- ${scope_label} ${scope_name}: ${direction_label}, "
        "actual=${actual_count}, expected=${expected_count}${expected_basis}, "
        "delta=${absolute_delta} (${percent_delta_label})"
        "${drift_line}${details_line}${monitoring_line}${attribution_line}"
        "${top_movers_line}${sparkline_line}"
    ),
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2: (
        "\\- ${scope_label} ${scope_name}: ${direction_label}, "
        "actual=${actual_count}, expected=${expected_count}${expected_basis}, "
        "delta=${absolute_delta} \\(${percent_delta_label}\\)"
        "${drift_line}${details_line}${monitoring_line}${attribution_line}"
        "${top_movers_line}${sparkline_line}"
    ),
}

ALERT_TEMPLATE_VARIABLES: dict[str, str] = {
    "project_name": "Project display name",
    "project_slug": "Project slug (unique only inside its organization)",
    "org_slug": "Organization slug of the project",
    "channel": "Destination channel",
    "destination_name": "Destination name",
    "rule_name": "Rule name",
    "scan_name": "Scan config name",
    "matched_count": "Number of matched alert items",
    "items_count": "Alias for matched_count",
    "items_text": "Preformatted list of all matched alert items",
    "headline": 'Digest summary line, e.g. "24 alerts - 7 down, 17 up" (digests only)',
    "window_label": (
        'The period a digest covers, in the project timezone, plus a "2/3" marker '
        "on the rare digest that needs more than one message (digests only)"
    ),
    "ai_explanation_block": "The AI note with its trailing blank line, or empty (digests only)",
}

ALERT_ITEM_TEMPLATE_VARIABLES: dict[str, str] = {
    "scope_name": "Matched scope name",
    "scope_type": "Matched scope type",
    "scope_label": "Matched scope label",
    "direction": "Direction: spike or drop",
    "direction_label": "Direction: up or down",
    "actual_count": "Actual count",
    "expected_count": "Expected count",
    "expected_basis": (
        "How the expected count was derived, when it is not a plain baseline "
        '("(adoption-adjusted)" for release regressions; empty otherwise)'
    ),
    "absolute_delta": "Absolute delta",
    "percent_delta": (
        "Percent delta as a bare number. Prints 0 when there was no baseline, so "
        "prefer percent_delta_label unless you need the raw number"
    ),
    "percent_delta_label": 'Percent delta with its "%" sign, or "no baseline" when expected is 0',
    "bucket": "Anomaly bucket timestamp",
    "details_url": "Event details URL",
    "monitoring_url": "Monitoring URL",
    "details_line": "Rendered details line with leading newline when URL exists",
    "monitoring_line": "Rendered monitoring line with leading newline when URL exists",
    "drift_field": (
        "Drift field name; the property name for value and property drift "
        "(empty for metric anomalies)"
    ),
    "drift_type": (
        "Drift type (empty for metric anomalies); for property drift one of new_property, "
        "missing_required, type_change"
    ),
    "sample_value": "Drift sample value (empty when unavailable)",
    "drift_line": "Rendered drift line with leading newline when drift context exists",
    "sparkline": "ASCII sparkline of recent bucket counts (empty if no history)",
    "top_movers": "Inline summary of top-3 breakdown movers (empty if none)",
    "sparkline_line": "Rendered trend line with leading newline when sparkline exists",
    "top_movers_line": "Rendered movers line with leading newline when movers exist",
    "attribution": (
        "Why it moved, as stored at detection time: the breakdown value that explains "
        'the delta, e.g. "92% of the drop comes from platform = ios (−3,120 of −3,390)", '
        "plus a release that crossed the activation gate in the window (empty if none)"
    ),
    "attribution_line": "Rendered attribution line with leading newline when one exists",
    "direction_arrow": "A single up/down arrow for the direction",
    "scope_link": (
        "Scope name linked to its incident on formats that support links; the bare name on plain"
    ),
}

# ── Digest rendering ──────────────────────────────────────────────────────
#
# A scheduled digest is a different reading task from an immediate alert. An
# immediate alert is ONE thing that just happened and the reader is being
# interrupted; a digest is twenty-four things that happened since yesterday and
# the reader is triaging over coffee. The verbose default answers the first
# question well and the second one badly: measured on a real production
# delivery it spends 317 characters per item, 186 of them on a raw details URL
# printed on its own line, so 24 items is ~8,150 characters of mostly URL.
#
# These templates are used ONLY when the delivery is a digest AND the operator
# has not saved a template of their own. A custom template always wins, and the
# immediate path never sees these at all.
DIGEST_ALERT_MESSAGE_TEMPLATES: dict[str, str] = {
    ALERT_MESSAGE_FORMAT_PLAIN: (
        "[tripl] ${headline}\n${window_label}\n${ai_explanation_block}${items_text}"
    ),
    ALERT_MESSAGE_FORMAT_SLACK_MRKDWN: (
        "*[tripl] ${headline}*\n${window_label}\n${ai_explanation_block}${items_text}"
    ),
    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML: (
        "<b>[tripl] ${headline}</b>\n${window_label}\n${ai_explanation_block}${items_text}"
    ),
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2: (
        "*tripl: ${headline}*\n${window_label}\n${ai_explanation_block}${items_text}"
    ),
}

# One line per item, and it must STAY one line: no variable here may carry a
# newline. ``${top_movers_line}``, ``${attribution_line}`` and ``${sparkline_line}``
# are deliberately absent because all three are built with a leading "\n  " —
# three breakdown movers
# per line across 24 items is not a scanning surface, it is what the page
# behind the link is for. ``${sparkline}`` (newline-free) carries the trend.
#
# ``${direction_arrow}`` leads the line rather than trailing it: with the items
# grouped, direction is otherwise carried only by the group heading, and a
# reader who scrolls past the heading loses it entirely.
DIGEST_ALERT_ITEMS_TEMPLATES: dict[str, str] = {
    ALERT_MESSAGE_FORMAT_PLAIN: (
        "${direction_arrow} ${scope_name} ${actual_count} vs ${expected_count} "
        "(${percent_delta_label})${details_line}"
    ),
    ALERT_MESSAGE_FORMAT_SLACK_MRKDWN: (
        "${direction_arrow} ${scope_link} ${actual_count} vs ${expected_count} "
        "(${percent_delta_label})"
    ),
    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML: (
        "${direction_arrow} ${scope_link} ${actual_count} vs ${expected_count} "
        "(${percent_delta_label})"
    ),
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2: (
        "${direction_arrow} ${scope_link} ${actual_count} vs ${expected_count} "
        "(${percent_delta_label})"
    ),
}

# Formats whose syntax can hide a URL behind a label. ``plain`` cannot, which is
# why its digest template above keeps ``${details_line}`` — dropping the link
# there would lose the way to reach the incident entirely.
LINK_CAPABLE_ALERT_FORMATS = frozenset(
    {
        ALERT_MESSAGE_FORMAT_SLACK_MRKDWN,
        ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
        ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
    }
)


def escape_alert_link_url(url: str, message_format: str) -> str:
    """Escape a URL for the href/target slot, which is NOT the label slot.

    The two slots have different rules and using ``escape_alert_value`` for both
    corrupts one of them. In MarkdownV2 the LABEL escapes ``_ * [ ] ( )`` and
    the URL must NOT — a backslash inside the parentheses is sent literally and
    the link 404s. In HTML the href needs the same entity escaping as text
    (``&`` in a query string above all), so it shares the escaper. Slack wraps
    ``<url|label>`` and needs the url's ``<`` ``>`` ``&`` escaped but nothing
    else.
    """
    if message_format == ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2:
        # Only the two characters that would close the link early.
        return url.replace("\\", "%5C").replace(")", "%29")
    if message_format in (
        ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
        ALERT_MESSAGE_FORMAT_SLACK_MRKDWN,
    ):
        return escape_alert_value(url, message_format)
    return url


def format_alert_link(label: str, url: str, message_format: str) -> str:
    """A label linked to a URL, or the bare escaped label when the format cannot."""
    safe_label = escape_alert_value(label, message_format)
    if not url or message_format not in LINK_CAPABLE_ALERT_FORMATS:
        return safe_label
    safe_url = escape_alert_link_url(url, message_format)
    if message_format == ALERT_MESSAGE_FORMAT_TELEGRAM_HTML:
        return f'<a href="{safe_url}">{safe_label}</a>'
    if message_format == ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2:
        return f"[{safe_label}]({safe_url})"
    return f"<{safe_url}|{safe_label}>"


def format_alert_bold(text: str, message_format: str) -> str:
    """Bold an ALREADY-ESCAPED string.

    Escape first, then wrap — never the other way round. Wrapping first and
    escaping the result turns the markup itself into literal text; escaping a
    string that already contains the wrapper does the same. A group heading is
    the one string in a digest body that is built at runtime rather than pulled
    from an item field, and it is exactly where that has been got wrong before:
    ``variable_value_drift drops`` carries two underscores, which MarkdownV2
    reads as a nested italic entity.
    """
    if message_format == ALERT_MESSAGE_FORMAT_TELEGRAM_HTML:
        return f"<b>{text}</b>"
    if message_format in (
        ALERT_MESSAGE_FORMAT_SLACK_MRKDWN,
        ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
    ):
        return f"*{text}*"
    return text


def get_digest_message_template(message_format: str | None) -> str:
    fmt = message_format or ALERT_MESSAGE_FORMAT_PLAIN
    return DIGEST_ALERT_MESSAGE_TEMPLATES.get(
        fmt, DIGEST_ALERT_MESSAGE_TEMPLATES[ALERT_MESSAGE_FORMAT_PLAIN]
    )


def get_digest_items_template(message_format: str | None) -> str:
    fmt = message_format or ALERT_MESSAGE_FORMAT_PLAIN
    return DIGEST_ALERT_ITEMS_TEMPLATES.get(
        fmt, DIGEST_ALERT_ITEMS_TEMPLATES[ALERT_MESSAGE_FORMAT_PLAIN]
    )


_ALERT_TEMPLATE_VAR_RE = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_TELEGRAM_MARKDOWNV2_SPECIAL_CHARS = set("_*[]()~`>#+-=|{}.!\\")

# Catalog metric unit that marks stored-fraction values (0.08 == 8%).
METRIC_UNIT_PERCENT = "%"

# What the percent parenthetical says when there is nothing to divide by.
NO_BASELINE_LABEL = "no baseline"

# The ``local_notice`` a ``demo_sink`` delivery carries in its payload snapshot.
# One constant for both writers — the demo seeder that records the initial
# delivery and the worker's demo_sink dispatch branch (send, retry, simulate) —
# so a freshly seeded delivery and a retried one always describe themselves the
# same way.
DEMO_SINK_LOCAL_NOTICE = (
    "Simulated local delivery (demo_sink) — rendered and recorded locally with "
    "no external message sent."
)


@dataclass(frozen=True)
class AlertTemplateContext:
    variables: dict[str, str]
    message_format: str


def normalize_message_template(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def normalize_message_format(value: str | None) -> str:
    return value or ALERT_MESSAGE_FORMAT_PLAIN


def get_default_message_template(message_format: str | None) -> str:
    normalized_format = normalize_message_format(message_format)
    return DEFAULT_ALERT_MESSAGE_TEMPLATES.get(
        normalized_format,
        DEFAULT_ALERT_MESSAGE_TEMPLATES[ALERT_MESSAGE_FORMAT_PLAIN],
    )


def get_default_items_template(message_format: str | None) -> str:
    normalized_format = normalize_message_format(message_format)
    return DEFAULT_ALERT_ITEMS_TEMPLATES.get(
        normalized_format,
        DEFAULT_ALERT_ITEMS_TEMPLATES[ALERT_MESSAGE_FORMAT_PLAIN],
    )


def get_supported_message_formats(destination_type: str) -> tuple[str, ...]:
    return ALERT_MESSAGE_FORMATS_BY_DESTINATION.get(destination_type, (ALERT_MESSAGE_FORMAT_PLAIN,))


def validate_template_configuration(
    *,
    destination_type: str,
    message_format: str | None,
    message_template: str | None,
    items_template: str | None,
) -> tuple[str, str | None, str | None]:
    normalized_format = normalize_message_format(message_format)
    supported_formats = get_supported_message_formats(destination_type)
    if normalized_format not in supported_formats:
        raise ValueError(
            f"Message format {normalized_format!r} is not supported for {destination_type}"
        )

    normalized_template = normalize_message_template(message_template)
    if normalized_template is not None:
        unknown_variables = sorted(
            {
                match.group(1)
                for match in _ALERT_TEMPLATE_VAR_RE.finditer(normalized_template)
                if match.group(1) not in ALERT_TEMPLATE_VARIABLES
            }
        )
        if unknown_variables:
            raise ValueError("Unknown alert template variables: " + ", ".join(unknown_variables))

    normalized_items_template = normalize_message_template(items_template)
    if normalized_items_template is not None:
        unknown_item_variables = sorted(
            {
                match.group(1)
                for match in _ALERT_TEMPLATE_VAR_RE.finditer(normalized_items_template)
                if match.group(1) not in ALERT_ITEM_TEMPLATE_VARIABLES
            }
        )
        if unknown_item_variables:
            raise ValueError(
                "Unknown alert item template variables: " + ", ".join(unknown_item_variables)
            )

    return normalized_format, normalized_template, normalized_items_template


def escape_alert_value(value: object, message_format: str) -> str:
    text = _stringify_alert_value(value)
    if message_format == ALERT_MESSAGE_FORMAT_TELEGRAM_HTML:
        return html.escape(text, quote=True)
    if message_format == ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2:
        return "".join(
            f"\\{char}" if char in _TELEGRAM_MARKDOWNV2_SPECIAL_CHARS else char for char in text
        )
    if message_format == ALERT_MESSAGE_FORMAT_SLACK_MRKDWN:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text


def render_alert_template(template: str, context: AlertTemplateContext) -> str:
    return _ALERT_TEMPLATE_VAR_RE.sub(
        lambda match: context.variables.get(match.group(1), match.group(0)),
        template,
    )


def _stringify_alert_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:.1f}" if not value.is_integer() else str(int(value))
    return str(value)


def format_metric_alert_value(value: float, unit: str | None) -> str | float:
    """Percent-aware rendering for metric-scope alert values.

    Catalog metrics with unit ``%`` store fractions (0.08 == 8%), so scale by
    100 and suffix ``%`` using the same integral/fractional rules as the shared
    stringifier ("8%" integral, "8.3%" fractional). Any other unit returns the
    value unchanged so downstream stringifying/escaping behaves exactly as
    before. Rounding absorbs binary float noise (0.08 * 100 == 8.000...002).
    """
    if unit != METRIC_UNIT_PERCENT:
        return value
    scaled = round(float(value) * 100, 10)
    return f"{_stringify_alert_value(scaled)}%"


def has_baseline(expected_count: float) -> bool:
    """Was there an expectation to divide by?

    ZERO is the no-baseline condition, and it is the ONLY one. The percent gate
    deliberately admits anomalies with no baseline at all — a
    scope resuming after an outage, an event firing for the first time, a schema
    drift — and every one of those arrives with ``expected_count`` exactly 0. The
    stored ``percent_delta`` is a 0.0 placeholder for them, because the ratio is
    undefined and the column is NOT NULL; emitting it reported the largest
    possible relative move as the smallest (.27).

    A NEGATIVE expectation is a REAL baseline. A ``fact`` sum/avg/min/max over a
    signed column, or a ``sql`` level that legitimately sits below zero, has a
    level of -100 that is exactly as substantial as one of +100; the detector
    scores that series on magnitude
    (``anomaly_detector._clears_volume_gate``) and the matcher fires on it the
    same way (``alerting_matching.rule_matches_anomaly``: ``abs(expected)``
    against ``min_expected_count``, ``absolute_delta / abs(expected)`` against
    ``min_percent_delta``). A reader still asking
    ``expected_count > 0`` therefore prints "no baseline" over the very number
    that made the rule fire — the renderer contradicting the matcher.

    It is one function rather than the same expression repeated per reader
    because the repetition is exactly how the signed fix reached the matcher and
    the payload builder and left the renderers behind. Every backend surface that
    decides "was there a baseline" must route through here.
    """
    return expected_count != 0


def percent_delta_of(actual_count: float, expected_count: float) -> float:
    """The ``percent_delta`` stored for one signal: the SIZE of the move.

    THE definition, and the only one. ``dispatch._create_deliveries`` writes
    ``AlertDeliveryItem.percent_delta`` from it, ``alert_payload`` freezes the
    same number into ``AlertDelivery.payload_snapshot``, the simulator's
    ``SimulatedRuleFiring`` (``alerting_service.simulate_rule``) replays it, and
    the demo builder seeds it — so the simulator cannot disagree with the thing
    it simulates and one delivery cannot disagree with itself. Each of those was
    once a separate copy of this expression, which is how the signed fix
     reached some of them and not others; add a writer, call
    this, do not re-derive the ratio.

    Both numerator and divisor are MAGNITUDES, so the ratio stays a size instead
    of flipping sign with the level: -3 -> -9 is a 200% move, the same as
    3 -> 9. Direction is carried by ``direction``/``actual_count`` and never by
    this field. With no baseline (:func:`has_baseline`) the ratio is undefined
    and the frozen 0.0 placeholder is returned; every OUTBOUND encoding of it
    goes through :func:`format_percent_delta` or :func:`percent_delta_or_none`,
    which name the placeholder rather than print it.
    """
    if not has_baseline(expected_count):
        return 0.0
    return abs(actual_count - expected_count) / abs(expected_count) * 100


def format_percent_delta(percent_delta: float, expected_count: float, *, spec: str = ".1f") -> str:
    """The percent parenthetical for one alert item, unit included.

    :func:`has_baseline` is the exact condition under which the stored number was
    computed (:func:`percent_delta_of`, which is what ``dispatch`` and
    ``alert_payload`` call), so the label and the number can never disagree
    about whether there was a baseline. Without a baseline the absolute
    delta stands on its own for that class, and the default item templates
    already print it.

    ``spec`` is the float format the caller wants around it: the item templates
    use ".1f", the AI prompt "+.0f".
    """
    if has_baseline(expected_count):
        return f"{percent_delta:{spec}}%"
    return NO_BASELINE_LABEL


def percent_delta_or_none(percent_delta: float, expected_count: float) -> float | None:
    """The machine-readable twin of :func:`format_percent_delta`.

    Same gate, same fact, different encoding: a human reading an alert is told
    the words ``no baseline``; a program parsing JSON is handed ``null``. What
    neither may be handed is the stored ``0.0`` placeholder, because a consumer
    cannot tell it apart from a real "no change" — and the class it hides is
    exactly the loudest one, a scope firing from nothing or resuming after an
    outage. ``expected_count`` travels beside it in every payload
    and corroborates the null, but a consumer that only reads this field must
    still not be misled by it.

    Callers keep the stored column as it is: ``AlertDeliveryItem.percent_delta``
    is NOT NULL and holds frozen history, so the placeholder stays there and only
    the outbound encodings change.
    """
    if has_baseline(expected_count):
        return percent_delta
    return None


# ── ${scope_label} and ${drift_line}: one wording, two renderers ───────────
#
# Two code paths render an alert item and they must word it identically. The
# worker renders a DELIVERED ``AlertDeliveryItem``
# (``alerts_messages._build_item_template_context``); the rule simulator
# renders a SIMULATED ``SimulatedRuleFiring``
# (``alerting_rendering.render_firing_item``), and the simulator exists so an
# operator can read what a rule WOULD send before pointing it at a live
# channel. Each used to build these two strings itself and the copies drifted:
# for one schema drift the worker wrote "drift: type_changed amount
# sample=9.99" where the preview wrote "drift: type_changed: amount — e.g.
# 9.99", so a rule tested in the simulator and then sent for real described the
# same firing two different ways. The label map had split the
# same way — only the worker's knew about release regressions.
#
# The WORKER's wording is the one kept in both cases. It is the text an
# operator actually receives, it is what website/docs/use/alerting.md
# ("Release-regression items") quotes, and adopting it moves only future
# previews; adopting the preview's would have rewritten production message text
# on every channel and every already-stored payload snapshot would disagree
# with the next send.
#
# The scope constants below come from ``MetricScopeType`` rather than from
# ``tripl.alerting_matching``: this module is the leaf that both the worker and
# the services layer import, and it stays below the matcher.

_SCOPE_RELEASE_REGRESSION = MetricScopeType.release_regression.value
_SCOPE_VARIABLE_VALUE_DRIFT = MetricScopeType.variable_value_drift.value
_SCOPE_SOURCE_FRESHNESS = MetricScopeType.source_freshness.value
_SCOPE_LIFECYCLE = MetricScopeType.lifecycle.value
_SCOPE_PROPERTY_DRIFT = MetricScopeType.property_drift.value

ALERT_SCOPE_LABELS: dict[str, str] = {
    MetricScopeType.project_total.value: "Project total",
    MetricScopeType.event_type.value: "Event type",
    MetricScopeType.event.value: "Event",
    MetricScopeType.metric.value: "Metric",
    MetricScopeType.schema.value: "Schema drift",
    MetricScopeType.distribution.value: "Distribution drift",
    _SCOPE_VARIABLE_VALUE_DRIFT: "Variable value drift",
    _SCOPE_RELEASE_REGRESSION: "Release regression",
    _SCOPE_SOURCE_FRESHNESS: "Source freshness",
    _SCOPE_LIFECYCLE: "Event lifecycle",
    _SCOPE_PROPERTY_DRIFT: "Property drift",
}


def alert_scope_label(scope_type: str) -> str:
    """The human name a rendered item gives a scope, for ``${scope_label}``.

    Shared by delivered messages and simulator previews so their scope labels
    stay identical. Unknown future scopes still fall back to their raw value.
    """
    return ALERT_SCOPE_LABELS.get(scope_type, str(scope_type))


@dataclass(frozen=True)
class DriftLineFacts:
    """Every fact ``${drift_line}`` is built from, named once.

    This field list IS the contract between the two renderers: a delivered
    ``AlertDeliveryItem`` and a simulated ``SimulatedRuleFiring`` each carry all
    of them, so neither side can render a line the other cannot. Adapters on
    both sides fill every field; the defaults exist so a test can state only the
    facts its case is about.

    ``window_from`` is filled by both adapters too, but only one family HAS
    one: a release regression is measured over the activation-anchored rollout
    overlap, so ``ReleaseRegression.window_from`` is NOT NULL, the send
    snapshots it onto ``AlertDeliveryItem.window_from`` and — since
    tripl-0zpq.158 taught the replay to load those rows — the preview carries
    it through ``DriftAlertCandidate`` and ``SimulatedRuleFiring``. Every other
    scope's window IS its bucket and leaves it None, as does any item delivered
    before the column existed; that is what its only consumer, the
    rollout-overlap clause, drops out on.
    """

    scope_type: str
    drift_type: str | None = None
    drift_field: str | None = None
    sample_value: str | None = None
    expected_count: float = 0.0
    percent_delta: float = 0.0
    bucket: datetime | None = None
    window_from: datetime | None = None
    event_id: uuid.UUID | None = None
    event_type_id: uuid.UUID | None = None


_RELEASE_KIND_LABELS = {"missing": "disappeared", "volume_drop": "dropped"}

# Headline per freshness ``drift_type``. ``source_late``: the scan runs but the
# newest event it sees is old — the warehouse load is delayed.
# ``source_overdue``: the scan itself has not completed a collection in time.
_FRESHNESS_STATUS_LABELS = {
    AlertDriftType.source_late.value: "Data late",
    AlertDriftType.source_overdue.value: "Scan overdue",
}


def plain_alert_number(value: float) -> str:
    """Stringify a number exactly as ``${expected_count}`` does, unescaped.

    The basis clause is escaped as a whole by its caller, so escaping here too
    would double-escape it under MarkdownV2. The plain format is the shared
    stringifier's pass-through branch, which is all this needs.
    """
    return escape_alert_value(value, ALERT_MESSAGE_FORMAT_PLAIN)


def _release_scope_noun(facts: DriftLineFacts) -> str:
    """What the regressed scope IS, so the basis sentence can name it."""
    if facts.event_id is not None:
        return "event"
    if facts.event_type_id is not None:
        return "event type"
    return "scope"


def _format_window_span(facts: DriftLineFacts) -> str | None:
    """``"51h"`` for the window this item was measured over, or None.

    ``bucket`` is the window's end and ``window_from`` its start. Every scope
    whose window IS its bucket, and any item delivered before the column
    existed, gets None and simply loses the clause. Release regressions are the
    one family that carries a window, and they carry it on BOTH sides since
    a simulated firing has it too, which is what lets the
    preview print the same "over the 51h rollout overlap" the delivered message
    prints. A span that rounds to under an hour returns None as well, rather
    than printing ``0h``.
    """
    if facts.window_from is None or facts.bucket is None:
        return None
    hours = round((facts.bucket - facts.window_from).total_seconds() / 3600)
    if hours < 1:
        return None
    return f"{hours}h"


def release_regression_basis(facts: DriftLineFacts) -> str:
    """The body of the "release:" line: which build, over what window, vs what.

    Naming the build is not enough. ``expected`` for a release regression is
    ``total_new * share_prev`` — the PREVIOUS release's share of this scope
    applied to the NEW release's own volume over the rollout-overlap window —
    so it is not a count of the same thing as ``actual`` and the ``%`` beside
    it is already ``1 - share_new/share_prev``, i.e. the share-for-share drop.
    Printed bare, the pair reads as "the count halved", and the first reply is
    "so what, the release only just rolled out" — an objection the
    normalization has already priced in, because a smaller adopting cohort
    shrinks ``total_new`` and shrinks ``expected`` with it.

    So the line states that the expectation was built FROM the new release's
    own volume. That single fact is what kills the misreading; the window is
    corroboration. Costs ~117 UTF-16 units over the old line, well inside the
    per-item budget that keeps a full 8-item Telegram delivery in one message.

    Public because the AI-explanation prompt quotes the same clause it renders
    (``alerts_messages._append_ai_explanation``): without it the model writes
    the note from the raw counts alone and re-teaches the reading this sentence
    exists to remove.
    """
    kind_label = _RELEASE_KIND_LABELS.get(facts.drift_type or "", "regressed")
    version = facts.drift_field or "the new release"
    previous = facts.sample_value or "the previous release"
    span = _format_window_span(facts)
    window_clause = f" over the {span} rollout overlap" if span else ""
    line = f"{kind_label} in {version} vs {previous}{window_clause}"
    if not has_baseline(facts.expected_count):
        # No baseline: there is no ratio to explain and ${percent_delta_label}
        # already says "no baseline". Adding the formula here would quote a
        # zero as if it were an expectation. Through ``has_baseline`` so that it
        # is the SAME question ``format_percent_delta`` answers four lines down:
        # a signed expectation renders a real percentage there, and this sentence
        # has to explain the ratio rather than deny there is one.
        return line
    return (
        f"{line}; {plain_alert_number(facts.expected_count)} is {previous}'s share "
        f"of this {_release_scope_noun(facts)} at {version}'s own volume, so "
        f"{format_percent_delta(facts.percent_delta, facts.expected_count)} "
        f"is share-for-share"
    )


def source_freshness_line(facts: DriftLineFacts) -> str:
    """``"Data late: <scan> — newest event 7h ago (expected within 3h)"``.

    Built from the shared drift columns the worker fills
    (``signals._get_source_freshness_candidates``): status -> ``drift_type``
    (``source_late`` / ``source_overdue``),
    scan name -> ``drift_field``, the lag clause -> ``sample_value``.
    """
    headline = _FRESHNESS_STATUS_LABELS.get(facts.drift_type or "", "Data late")
    scan = facts.drift_field or "scan"
    if facts.sample_value:
        return f"{headline}: {scan} \u2014 {facts.sample_value}"
    return f"{headline}: {scan}"


# Lifecycle finding kinds (GH #258) -> headline. The kind rides ``drift_type``
# (``AlertDriftType.sunset_overdue`` / ``successor_silent``, the same values as
# ``LifecycleFindingKind``).
LIFECYCLE_KIND_SUNSET_OVERDUE = AlertDriftType.sunset_overdue.value
LIFECYCLE_KIND_SUCCESSOR_SILENT = AlertDriftType.successor_silent.value
_LIFECYCLE_KIND_LABELS = {
    LIFECYCLE_KIND_SUNSET_OVERDUE: "Sunset overdue",
    LIFECYCLE_KIND_SUCCESSOR_SILENT: "Successor silent",
}


def lifecycle_line(facts: DriftLineFacts) -> str:
    """``"Sunset overdue: signup still receives 1,240/day, sunset 2026-09-01"``.

    Built from the shared drift columns ``lifecycle_alerts`` fills: kind ->
    ``drift_type``, the event the message names -> ``drift_field``, the rendered
    "<event> <what is wrong>" clause -> ``sample_value``.
    """
    headline = _LIFECYCLE_KIND_LABELS.get(facts.drift_type or "", "Event lifecycle")
    if facts.sample_value:
        return f"{headline}: {facts.sample_value}"
    if facts.drift_field:
        return f"{headline}: {facts.drift_field}"
    return headline


# Property drift kinds (F23, #306) -> headline, most serious first. The kind
# rides ``drift_type`` (``AlertDriftType.missing_required`` / ``type_change`` /
# ``new_property``, the same values as ``PropertyDriftKind``). A required
# property going missing and a type change break consumers of the event; a new
# property is the plan falling behind the data.
_PROPERTY_DRIFT_KIND_LABELS = {
    AlertDriftType.missing_required.value: "Missing required property",
    AlertDriftType.type_change.value: "Property type changed",
    AlertDriftType.new_property.value: "New property",
}


def property_drift_line(facts: DriftLineFacts) -> str:
    """``"Missing required property ${plan}: on 40% of rows, required on 95%"``.

    Built from the shared drift columns ``alerting_property_drift`` fills:
    kind -> ``drift_type``, the property -> ``drift_field``, what the scan saw
    -> ``sample_value``.
    """
    headline = _PROPERTY_DRIFT_KIND_LABELS.get(facts.drift_type or "", "Property drift")
    subject = f"{headline} ${{{facts.drift_field}}}" if facts.drift_field else headline
    return f"{subject}: {facts.sample_value}" if facts.sample_value else subject


def build_drift_line(facts: DriftLineFacts) -> str:
    """``${drift_line}`` for one item — leading ``"\\n  "`` included, or ``""``.

    Three scopes reuse the same three drift columns to say three different
    things, so the placeholder is one variable with three wordings:

    * a release regression reads version -> ``drift_field``, kind ->
      ``drift_type``, previous release -> ``sample_value`` and renders the
      ``release:`` basis sentence;
    * a value drift reads variable -> ``drift_field`` and the sampled novel
      values -> ``sample_value``;
    * everything that has drift columns at all — schema and distribution drift
      — space-joins whichever of the three it has.

    Returns the empty string when there is nothing to say, which is the normal
    case: a count anomaly carries no drift columns, and the default item
    templates place ``${drift_line}`` so that an empty one leaves no blank line.
    The caller escapes the result as a whole, once, for its message format.
    """
    if facts.scope_type == _SCOPE_RELEASE_REGRESSION:
        return f"\n  release: {release_regression_basis(facts)}"
    if facts.scope_type == _SCOPE_SOURCE_FRESHNESS:
        return f"\n  {source_freshness_line(facts)}"
    if facts.scope_type == _SCOPE_LIFECYCLE:
        return f"\n  {lifecycle_line(facts)}"
    if facts.scope_type == _SCOPE_PROPERTY_DRIFT:
        return f"\n  {property_drift_line(facts)}"
    if facts.scope_type == _SCOPE_VARIABLE_VALUE_DRIFT:
        observed_clause = f" observed {facts.sample_value}" if facts.sample_value else ""
        return f"\n  value drift: ${{{facts.drift_field}}}{observed_clause}"
    drift_parts = [
        facts.drift_type or "",
        facts.drift_field or "",
        f"sample={facts.sample_value}" if facts.sample_value else "",
    ]
    drift_text = " ".join(part for part in drift_parts if part)
    return f"\n  drift: {drift_text}" if drift_text else ""
