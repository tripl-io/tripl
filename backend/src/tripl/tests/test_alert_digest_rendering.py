"""How a scheduled digest reads, as opposed to an immediate alert.

An immediate alert is ONE thing that just happened and the reader is being
interrupted. A digest is everything since yesterday and the reader is triaging.
Measured on a real production delivery, the verbose default spends 317
characters per item — 186 of them on a raw URL on its own line — so a 24-alert
morning is ~8,150 characters that nobody reads.

These pin the layout decisions, the escaping (a Telegram parse error rejects
the whole message), and the one number that makes it work: what Telegram counts
is the text AFTER entity parsing, so a URL hidden behind a link label is free.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from html import unescape
from types import SimpleNamespace

import pytest

from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    ALERT_MESSAGE_FORMAT_SLACK_MRKDWN,
    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
    escape_alert_value,
    format_alert_bold,
    format_alert_link,
    get_default_items_template,
    get_digest_items_template,
)
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.project import Project
from tripl.worker.tasks import alerts_messages as am

_URL = (
    "https://tripl.example.com/p/acme-ios/alerting/"
    "c33ed139-da4d-429f-8ee9-f9c61e67d02c?item=event:12e1e41c&incident=514450c0"
)


def _item(name: str, actual: float, expected: float, direction: str, hour: int = 12):
    return SimpleNamespace(
        id=uuid.uuid4(),
        scope_name=name,
        scope_type="event",
        scope_ref=str(uuid.uuid4()),
        direction=direction,
        actual_count=float(actual),
        expected_count=float(expected),
        absolute_delta=abs(actual - expected),
        percent_delta=(abs(actual - expected) / expected * 100 if expected else 0.0),
        bucket=datetime(2026, 9, 6, hour, tzinfo=UTC),
        details_path=_URL,
        monitoring_path=None,
        drift_field=None,
        drift_type=None,
        sample_value=None,
        window_from=None,
        # An event-scope item carries an event id on the real row, and the
        # renderer reads both of these now: ``_release_scope_noun`` names what
        # the regressed scope IS ("event" / "event type" / "scope") in the
        # release-regression basis clause. These items never take that branch —
        # they carry no drift_type — but a double that omits a column its
        # subject always has is a claim that the absence is normal.
        event_id=uuid.uuid4(),
        event_type_id=None,
    )


def _visible(text: str) -> str:
    """What Telegram shows after it parses the entities away."""
    return unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", text))


# ── layout ────────────────────────────────────────────────────────────────


def test_drops_lead_because_they_are_the_actionable_class() -> None:
    """A drop needs a baseline to BE a drop, so it is the smaller class — and a
    fall in checkout or login is close to always worth more than a rise in an
    impression counter. Ordered the other way round, the two revenue-shaped
    drops of a real 24-item morning sat about thirty phone-lines down."""
    items = [
        _item("promo:view:banner", 900, 300, "spike"),
        _item("checkout:complete:annual", 42, 310, "drop"),
        _item("session:start:cold", 88000, 0, "spike"),
    ]

    headings = [heading for heading, _group in am._digest_groups(items)]

    assert headings == ["1 down", "1 up", "1 new"]


def test_a_scope_with_no_baseline_gets_its_own_trailing_group() -> None:
    """Ranking by percent puts them first by construction — an undefined ratio
    has no magnitude — which is backwards. A counter that went from nothing to
    something is usually a new event shipping, not an incident."""
    items = [
        _item("session:start:cold", 88000, 0, "spike"),
        _item("community:post_seen", 276, 136, "spike"),
    ]

    groups = dict(am._digest_groups(items))

    assert [i.scope_name for i in groups["1 up"]] == ["community:post_seen"]
    assert [i.scope_name for i in groups["1 new"]] == ["session:start:cold"]


def test_within_a_group_the_biggest_mover_is_first() -> None:
    items = [
        _item("small", 90, 100, "drop"),
        _item("huge", 10, 100, "drop"),
        _item("medium", 50, 100, "drop"),
    ]

    ((_heading, group),) = am._digest_groups(items)

    assert [i.scope_name for i in group] == ["huge", "medium", "small"]


def test_every_digest_line_stays_one_line() -> None:
    """``top_movers_line`` and ``sparkline_line`` are built with a LEADING
    newline, so including either would make "one line per alert" false the
    moment a scope had breakdown movers."""
    for fmt in (
        ALERT_MESSAGE_FORMAT_PLAIN,
        ALERT_MESSAGE_FORMAT_SLACK_MRKDWN,
        ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
        ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
    ):
        template = get_digest_items_template(fmt)
        assert "${top_movers_line}" not in template, fmt
        assert "${sparkline_line}" not in template, fmt


def test_the_arrow_leads_the_line_because_the_heading_scrolls_away() -> None:
    """Direction is otherwise carried only by the group heading, and the sign
    does not carry it: format_percent_delta prints an unsigned magnitude for a
    spike too."""
    text = am._build_items_text(
        [_item("checkout:complete:annual", 42, 310, "drop")],
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
        items_template=get_digest_items_template(ALERT_MESSAGE_FORMAT_PLAIN),
        digest=True,
    )

    assert "▼ checkout:complete:annual" in text
    assert "▲" not in text


def _drift(name: str, scope_type: str, actual: float, expected: float) -> SimpleNamespace:
    """A drift as its row stores it: filed as a spike, so the Spikes toggle gates it."""
    item = _item(name, actual, expected, "spike")
    item.scope_type = scope_type
    return item


def test_a_drift_is_grouped_as_a_drift_not_as_up_or_new() -> None:
    """A drift went neither up nor down. A distribution drift compares two
    windows' row counts, so it sat under "up"; a schema drift's expected 0 put
    it under "new", as if an event had shipped."""
    distribution = _drift("screen_view.platform", "distribution", 1200, 1200)
    schema = _drift("checkout.amount", "schema", 1, 0)
    items = [
        _item("checkout:complete:annual", 42, 310, "drop"),
        distribution,
        schema,
        _item("session:start:cold", 88000, 0, "spike"),
    ]

    groups = am._digest_groups(items)

    assert [heading for heading, _group in groups] == ["1 down", "2 drifts", "1 new"]
    assert dict(groups)["2 drifts"] == [distribution, schema]
    assert "1 down, 2 drifts, 1 new" in am._digest_headline(items, len(items))
    # Nothing ranks a drift, so it is never the worst mover.
    assert am._digest_headline([distribution], 1) == "1 alerts · 1 drift"


def test_a_digest_names_a_drift_s_kind_and_quotes_no_counts() -> None:
    text = am._build_items_text(
        [_drift("screen_view.platform", "distribution", 1200, 1200)],
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
        items_template=None,
        digest=True,
    )

    assert text.startswith("◆ Distribution drift screen_view.platform\n  details: ")
    assert "1200" not in text
    assert "▲" not in text


# ── the headline and the window ───────────────────────────────────────────


def test_the_headline_is_deterministic_and_names_the_worst_drop() -> None:
    """This is what lands in the phone's notification preview, so it has to be
    true on the delivery where the LLM is off, times out, or spends its first
    230 characters clearing its throat."""
    items = [
        _item("checkout:complete:annual", 42, 310, "drop"),
        _item("community:post_seen", 276, 136, "spike"),
        _item("session:start:cold", 88000, 0, "spike"),
    ]

    headline = am._digest_headline(items, len(items))

    assert headline.startswith("3 alerts")
    assert "1 down, 1 up, 1 new" in headline
    assert "worst checkout:complete:annual down 86%" in headline


def test_the_window_is_stated_in_the_project_clock() -> None:
    """The reader set the schedule as a wall-clock time in their own zone;
    telling them the window in UTC makes them redo the arithmetic."""
    items = [_item("a", 1, 2, "drop", hour=6), _item("b", 1, 2, "drop", hour=16)]

    label = am._digest_window_label(items, "Europe/Moscow")

    assert "Europe/Moscow" in label
    assert "09:00" in label and "19:00" in label


def test_an_unusable_project_zone_degrades_rather_than_raising() -> None:
    assert "UTC" in am._digest_window_label([_item("a", 1, 2, "drop")], "Mars/Olympus_Mons")


# ── escaping: a parse error rejects the WHOLE message ─────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "promo:tap:acme_to_partner_banner",  # MarkdownV2 italic trap
        "a<b>&c",  # HTML injection through a scope name
        "weird (paren) name",  # closes a MarkdownV2 link early
    ],
)
def test_a_scope_name_can_never_break_out_of_its_link(name: str) -> None:
    html_link = format_alert_link(name, _URL, ALERT_MESSAGE_FORMAT_TELEGRAM_HTML)
    assert html_link.startswith('<a href="') and html_link.endswith("</a>")
    # Exactly one anchor: the label cannot have opened a tag of its own.
    assert html_link.count("<a ") == 1 and html_link.count("<") == 2

    md_link = format_alert_link(name, _URL, ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2)
    label = md_link[1 : md_link.index("](")]
    assert "\\" in label or not set("_*()[]").intersection(name)


def test_a_markdownv2_url_is_not_backslash_escaped() -> None:
    """The label and the href have DIFFERENT rules. A backslash inside the
    parentheses is sent literally and the link 404s."""
    link = format_alert_link("x", _URL, ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2)

    assert _URL in link


def test_a_group_heading_is_escaped_before_it_is_bolded() -> None:
    """A heading is the one body string built at runtime rather than read off an
    item. ``variable_value_drift drops`` carries two underscores, which
    MarkdownV2 reads as a nested italic entity."""
    heading = "3 variable_value_drift drops"
    fmt = ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2

    rendered = format_alert_bold(escape_alert_value(heading, fmt), fmt)

    assert rendered == "*3 variable\\_value\\_drift drops*"


def test_plain_keeps_the_url_because_it_has_no_link_syntax() -> None:
    """Dropping the details line on plain would remove the only way to reach
    the incident."""
    assert "${details_line}" in get_digest_items_template(ALERT_MESSAGE_FORMAT_PLAIN)
    assert "${scope_link}" in get_digest_items_template(ALERT_MESSAGE_FORMAT_TELEGRAM_HTML)


# ── the number that makes it work ─────────────────────────────────────────


def test_telegram_counts_the_text_after_entities_not_the_wire() -> None:
    """The 4096 ceiling applies to what the reader sees. Counting the raw body
    is what made hyperlinks buy nothing — the URL simply moved from a visible
    line into an href and kept splitting the message."""
    items = [_item(f"event:number:{n}", 100 + n, 50, "spike") for n in range(24)]
    text = am._build_items_text(
        items,
        message_format=ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
        items_template=get_digest_items_template(ALERT_MESSAGE_FORMAT_TELEGRAM_HTML),
        digest=True,
    )

    raw = am.telegram_message_length(text)
    visible = am.telegram_visible_length(text, ALERT_MESSAGE_FORMAT_TELEGRAM_HTML)

    assert raw > am.TELEGRAM_MESSAGE_MAX_CHARS, "the wire text alone would split"
    assert visible < am.TELEGRAM_MESSAGE_MAX_CHARS, "but the reader's text fits in one message"
    assert visible == am.telegram_message_length(_visible(text))


def test_the_visible_length_of_a_plain_body_is_its_whole_length() -> None:
    """Plain has no markup to parse away, so visible length IS wire length.

    The ASCII case alone stated that rule in ``len``, which is code points —
    the one unit that cannot express it. Telegram counts UTF-16 code units, so
    the two rulers agree only while the body stays in the BMP, and an assertion
    that holds only there pins the implementation rather than the contract. The
    emoji line is the case that carries the claim.
    """
    ascii_body = "just text, no markup"
    assert am.telegram_visible_length(ascii_body, ALERT_MESSAGE_FORMAT_PLAIN) == len(ascii_body)

    emoji_body = "checkout 🚀 dropped"
    visible = am.telegram_visible_length(emoji_body, ALERT_MESSAGE_FORMAT_PLAIN)
    assert visible == am.telegram_message_length(emoji_body)
    assert visible == len(emoji_body) + 1, "the surrogate pair costs two, not one"


def test_an_escaped_angle_bracket_still_counts_as_text() -> None:
    """Strip tags FIRST, then unescape. The other order turns an escaped
    ``&lt;b&gt;`` from a scope name into a tag and deletes visible text."""
    text = format_alert_link("a<b>c", _URL, ALERT_MESSAGE_FORMAT_TELEGRAM_HTML)

    assert am.telegram_visible_length(text, ALERT_MESSAGE_FORMAT_TELEGRAM_HTML) == len("a<b>c")


# ── the immediate path is untouched ───────────────────────────────────────


def test_without_the_digest_flag_nothing_changes() -> None:
    items = [_item("community:post_seen", 276, 136, "spike")]
    fmt = ALERT_MESSAGE_FORMAT_PLAIN

    immediate = am._build_items_text(
        items, message_format=fmt, items_template=get_default_items_template(fmt)
    )

    assert immediate.startswith("- Event community:post_seen: up, actual=276")
    assert "▲" not in immediate
    # No group headings at all on the immediate path.
    assert "1 up" not in immediate


# ── a split digest is still ONE digest ────────────────────────────────────


def _split_digest(
    count: int, *, max_chars: int, message_format: str = ALERT_MESSAGE_FORMAT_TELEGRAM_HTML
) -> list[tuple[str, list[AlertDeliveryItem]]]:
    """Force a digest across several messages and return them in send order."""
    items = [
        AlertDeliveryItem(
            id=uuid.uuid4(),
            delivery_id=uuid.uuid4(),
            scope_type="event",
            scope_ref=str(uuid.uuid4()),
            scope_name=f"main:tap:snippet:{index:03d}",
            # Spread across the morning so the WHOLE window is wider than any
            # single part's — the property the per-part label got wrong.
            bucket=datetime(2026, 9, 6, 8 + index, tzinfo=UTC),
            direction="drop",
            actual_count=15403,
            expected_count=32048,
            absolute_delta=16645,
            percent_delta=51.9,
            details_path=_URL,
            monitoring_path=None,
        )
        for index in range(count)
    ]
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        type=AlertDestinationType.telegram,
        name="All",
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="TG",
        message_format=message_format,
    )
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=destination.project_id,
        scan_config_id=uuid.uuid4(),
        destination_id=destination.id,
        rule_id=rule.id,
        channel=AlertDestinationType.telegram,
        matched_count=len(items),
    )
    delivery.items = items
    project = Project(id=delivery.project_id, name="acme-ios", slug="acme-ios", description="")
    text, fmt = am._render_delivery_message(
        delivery,
        destination=destination,
        rule=rule,
        scan_name="Snowplow Events (iOS)",
        project=project,
        digest=True,
        project_timezone="Europe/Moscow",
    )
    return am.split_telegram_messages(
        delivery,
        destination=destination,
        rule=rule,
        scan_name="Snowplow Events (iOS)",
        project=project,
        message=text,
        message_format=fmt,
        digest=True,
        project_timezone="Europe/Moscow",
        max_chars=max_chars,
    )


def test_every_part_of_a_split_digest_summarises_the_whole_digest() -> None:
    """The reader is holding one digest, not three, and the header must say so.

    Computed per part, message 2 of 3 announced its own share and named a window
    that closed hours before the digest's did — three disagreeing summaries of
    one morning, which is the reading a digest exists to prevent.
    """
    parts = _split_digest(12, max_chars=600)

    assert len(parts) > 1
    headlines = {_visible(text).splitlines()[0] for text, _items in parts}
    assert len(headlines) == 1
    assert "12 alerts" in headlines.pop()


def test_every_part_names_the_whole_window_not_its_own_slice() -> None:
    parts = _split_digest(12, max_chars=600)

    # 08:00–19:00 UTC is 11:00–22:00 in Moscow; no single part spans it.
    windows = {_visible(text).splitlines()[1] for text, _items in parts}
    assert len(windows) == len(parts), "the part marker must differ per part"
    assert all(window.startswith("Sep 06, 11:00–22:00 Europe/Moscow") for window in windows)


def test_a_split_digest_says_which_part_the_reader_is_holding() -> None:
    """Without it, "24 alerts" over nine visible lines reads as lost items."""
    parts = _split_digest(12, max_chars=600)

    markers = [_visible(text).splitlines()[1].rsplit(" · ", 1)[-1] for text, _items in parts]
    assert markers == [f"{index + 1}/{len(parts)}" for index in range(len(parts))]


def test_the_part_marker_never_pushes_a_part_over_the_ceiling() -> None:
    """The marker is reserved during packing precisely so this cannot happen.

    336 rather than a round number: the marker is stamped AFTER packing, so it
    can only overflow a part that packing left within its own width of the
    ceiling. Sweeping every ceiling from 300 to 1200 over this delivery, 42 of
    them do exactly that without the reserve — this is one, overflowing by 6 —
    and none of them do with it. A ceiling picked for roundness misses the
    hazard entirely and pins nothing.
    """
    parts = _split_digest(12, max_chars=336)

    assert len(parts) > 1
    for text, _items in parts:
        assert am.telegram_visible_length(text, ALERT_MESSAGE_FORMAT_TELEGRAM_HTML) <= 336


def test_a_split_digest_still_loses_no_item() -> None:
    parts = _split_digest(12, max_chars=600)

    # Without this the test passes on a single part, so it would go on being
    # green the day the split stopped happening at all — proving nothing about
    # the packing it exists to check.
    assert len(parts) > 1
    delivered = [item.scope_name for _text, part_items in parts for item in part_items]
    assert sorted(delivered) == sorted(f"main:tap:snippet:{index:03d}" for index in range(12))


def test_one_message_digests_carry_no_part_marker() -> None:
    """The overwhelming majority of digests, and they must not grow a "1/1"."""
    ((text, _items),) = _split_digest(4, max_chars=4096)

    window_line = _visible(text).splitlines()[1]
    assert window_line == "Sep 06, 11:00–14:00 Europe/Moscow"


# ── the MarkdownV2 length measurement survives a bracket in a scope name ──


def test_an_escaped_bracket_in_a_label_does_not_hide_the_link_from_the_ruler() -> None:
    """ "]" reaches the label as "\\]", which the old label class could not cross.

    The link then matched nothing and stayed in the count with its whole URL —
    over-counting by ~140 units per item and splitting messages that fitted.
    """
    fmt = ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2
    plain = format_alert_link("checkout", _URL, fmt)
    bracketed = format_alert_link("checkout[a]", _URL, fmt)

    assert am.telegram_visible_length(plain, fmt) == len("checkout")
    assert am.telegram_visible_length(bracketed, fmt) == len("checkout[a]")


def test_a_closing_paren_in_a_label_is_measured_too() -> None:
    fmt = ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2

    text = format_alert_link("checkout (v2)", _URL, fmt)

    assert am.telegram_visible_length(text, fmt) == len("checkout (v2)")
