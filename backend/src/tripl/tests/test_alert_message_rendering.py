"""Rendering-side alert defects: the release-regression trend, message length
and the zero-baseline percentage.

All three are about what the reader is shown, so all three are exercised through
the renderer rather than through a send.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
    get_default_items_template,
)
from tripl.alerting_matching import SCOPE_RELEASE_REGRESSION
from tripl.anomaly_context import load_recent_metric_points
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.schemas.alerting import AlertDeliveryItemResponse, SimulatedRuleFiring
from tripl.services.alerting_rendering import render_firing_item
from tripl.worker.tasks.alerts_messages import (
    TELEGRAM_MESSAGE_MAX_CHARS,
    _append_ai_explanation,
    _build_items_text,
    _is_telegram_markdown_parse_error,
    _is_telegram_message_too_long_error,
    _render_delivery_message,
    _webhook_item_payload,
    split_telegram_messages,
    telegram_message_length,
)
from tripl.worker.tasks.metrics.alert_payload import _build_delivery_snapshot


@pytest.fixture
def sync_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_event_with_metrics(session: Session) -> tuple[uuid.UUID, uuid.UUID, datetime]:
    """One event carrying 24 hourly EventMetric buckets. Returns (scan, event, last)."""
    project = Project(id=uuid.uuid4(), name="P", slug="rr-trend", description="")
    session.add(project)
    session.flush()
    data_source = DataSource(
        id=uuid.uuid4(),
        name="wh",
        db_type="clickhouse",
        host="localhost",
        port=9000,
        database_name="db",
        username="u",
        password_encrypted="x",
    )
    session.add(data_source)
    session.flush()
    config = ScanConfig(
        id=uuid.uuid4(),
        project_id=project.id,
        data_source_id=data_source.id,
        name="scan",
        base_query="SELECT * FROM events",
        app_version_column="app_version",
    )
    event_type = EventType(
        id=uuid.uuid4(), project_id=project.id, name="pv", display_name="PV", description=""
    )
    session.add_all([config, event_type])
    session.flush()
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name="main:tap:snippet",
        description="",
        order=0,
    )
    session.add(event)
    session.flush()

    last_bucket = datetime(2026, 8, 4, 19, tzinfo=UTC)
    for hours_ago in range(24):
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=event.id,
                event_type_id=event_type.id,
                bucket=last_bucket - timedelta(hours=hours_ago),
                count=100 + hours_ago,
            )
        )
    session.flush()
    return config.id, event.id, last_bucket


def test_release_regression_scope_gets_no_metric_series(sync_session: Session) -> None:
    """A release-regression scope_ref IS an event id, so the old fall-through
    resolved it and plotted the event's all-versions volume under a
    release-cohort headline (delivery d4d8cf9e). The event scope still plots."""
    scan_config_id, event_id, last_bucket = _seed_event_with_metrics(sync_session)

    assert (
        load_recent_metric_points(
            sync_session,
            scan_config_id=scan_config_id,
            scope_type=SCOPE_RELEASE_REGRESSION,
            scope_ref=str(event_id),
            until=last_bucket,
        )
        == []
    )

    event_points = load_recent_metric_points(
        sync_session,
        scan_config_id=scan_config_id,
        scope_type="event",
        scope_ref=str(event_id),
        until=last_bucket,
    )
    assert len(event_points) == 24


def _item(index: int, *, scope_type: str = "event") -> AlertDeliveryItem:
    return AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
        scope_type=scope_type,
        scope_ref=str(uuid.uuid4()),
        # Zero-padded so no scope name is a prefix of another: ":1" would
        # otherwise match inside ":10" when checking which items a message got.
        scope_name=f"main:tap:snippet:{index:03d}",
        bucket=datetime(2026, 8, 4, 19, tzinfo=UTC),
        direction="drop",
        actual_count=15403,
        expected_count=32048,
        absolute_delta=16645,
        percent_delta=51.9,
        # The optional lines are what make a real item ~350 characters; the
        # production URLs are this long.
        details_path=f"https://tripl.example.com/p/acme-ios/monitoring/event/{uuid.uuid4()}",
        monitoring_path=f"https://tripl.example.com/p/acme-ios/events/detail/{uuid.uuid4()}",
    )


def _render(items: list[AlertDeliveryItem]) -> str:
    return _build_items_text(
        items,
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
        items_template=get_default_items_template(ALERT_MESSAGE_FORMAT_PLAIN),
    )


def _shown_indices(text: str, total: int) -> list[int]:
    """Which items survived. An item spans several lines, so count scope names."""
    return [i for i in range(total) if f"main:tap:snippet:{i:03d}:" in text]


def _telegram_delivery(
    items: list[AlertDeliveryItem],
    *,
    ai_explanation_enabled: bool = True,
) -> tuple[AlertDelivery, AlertDestination, AlertRule, Project]:
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        type=AlertDestinationType.telegram,
        name="Test",
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="TG dev",
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
        ai_explanation_enabled=ai_explanation_enabled,
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
    return delivery, destination, rule, project


def _split(
    items: list[AlertDeliveryItem],
    *,
    ai_explanation: str | None = None,
    max_chars: int = TELEGRAM_MESSAGE_MAX_CHARS,
) -> list[tuple[str, list[AlertDeliveryItem]]]:
    delivery, destination, rule, project = _telegram_delivery(items)
    text, message_format = _render_delivery_message(
        delivery,
        destination=destination,
        rule=rule,
        scan_name="Snowplow Events (iOS)",
        project=project,
    )
    if ai_explanation:
        text = _append_ai_explanation(text, ai_explanation, message_format)
    return split_telegram_messages(
        delivery,
        destination=destination,
        rule=rule,
        scan_name="Snowplow Events (iOS)",
        project=project,
        message=text,
        message_format=message_format,
        ai_explanation=ai_explanation,
        max_chars=max_chars,
    )


def test_items_text_renders_every_item_it_is_given() -> None:
    """No truncation anywhere — email, Slack and webhook renders carry the lot."""
    items = [_item(i) for i in range(14)]
    text = _render(items)
    assert _shown_indices(text, 14) == list(range(14))
    assert "more of" not in text


def test_a_delivery_that_fits_is_still_one_message() -> None:
    """The common case pays nothing: one length check, no re-render, one send."""
    items = [_item(i) for i in range(4)]
    parts = _split(items)
    assert len(parts) == 1
    assert parts[0][1] == items
    assert telegram_message_length(parts[0][0]) <= TELEGRAM_MESSAGE_MAX_CHARS


def test_every_item_survives_the_split_into_several_messages() -> None:
    """The promise alerting.md makes: as many messages as it takes, none dropped.

    Driven at 14 items of the measured ~330-character size — the replay that
    unblocked volume scopes rendered exactly that, 4154 characters, which is
    when the ceiling starts to matter.
    """
    items = [_item(i) for i in range(14)]
    parts = _split(items, ai_explanation="Checkout volume fell across four screens." * 8)

    assert len(parts) > 1
    for text, _part_items in parts:
        assert telegram_message_length(text) <= TELEGRAM_MESSAGE_MAX_CHARS
        assert "not shown" not in text
    # Every item, exactly once, in the delivery's own order.
    assert [item for _text, part_items in parts for item in part_items] == items
    # And every item is actually rendered into the message it was assigned to.
    for text, part_items in parts:
        assert _shown_indices(text, 14) == [items.index(item) for item in part_items]


def test_the_ai_note_rides_on_the_first_message_only() -> None:
    """It summarises the whole delivery; repeating it just costs length."""
    note = "Checkout volume fell across four screens after the 8.2 rollout."
    parts = _split([_item(i) for i in range(14)], ai_explanation=note * 4)
    assert len(parts) > 1
    assert note in parts[0][0]
    assert [note in text for text, _ in parts[1:]] == [False] * (len(parts) - 1)


def test_the_ceiling_is_counted_in_utf16_code_units_not_code_points() -> None:
    """Telegram counts its 4096 in UTF-16 units, so an emoji costs two.

    Budgeting in ``len`` is how a message of 4000 code points gets refused at
    4400 units — the exact way the previous attempt at this ceiling was wrong.
    Every scope name here is astral, so a code-point split packs messages that
    Telegram refuses.
    """
    assert telegram_message_length("😀") == 2
    assert telegram_message_length("a") == 1

    items = [_item(i) for i in range(14)]
    for index, item in enumerate(items):
        item.scope_name = f"{'😀' * 40}:{index:03d}"
    parts = _split(items)

    assert len(parts) > 1
    for text, _part_items in parts:
        assert telegram_message_length(text) <= TELEGRAM_MESSAGE_MAX_CHARS
    # A code-point count would have said these messages had room to spare, and
    # packed more items into each of them.
    assert max(len(text) for text, _ in parts) < TELEGRAM_MESSAGE_MAX_CHARS * 0.9
    assert [item for _text, part_items in parts for item in part_items] == items


def test_a_single_item_over_the_ceiling_gets_its_own_message() -> None:
    """It cannot be made to fit, and dropping it is what this fix exists to stop.

    Telegram refuses it and the delivery fails visibly, which is the honest
    outcome: no budget re-render can shorten a message of one item.
    """
    items = [_item(0), _item(1)]
    items[0].scope_name = "x" * 5000
    parts = _split(items)

    assert [part_items for _text, part_items in parts] == [[items[0]], [items[1]]]
    assert telegram_message_length(parts[0][0]) > TELEGRAM_MESSAGE_MAX_CHARS


def test_each_message_counts_only_the_items_it_carries() -> None:
    """ "${matched_count} alerts" has to be true of the message it heads.

    Dispatch already gives every 8-item chunk its own count; a message that is
    one of several must do the same or the reader is told to look for 14 alerts
    in a message holding 6.
    """
    items = [_item(i) for i in range(14)]
    parts = _split(items)
    assert len(parts) > 1
    for text, part_items in parts:
        assert f"{len(part_items)} alerts" in text


def test_too_long_error_is_recognised_and_is_not_a_parse_error() -> None:
    """The message _post_json actually raises for an over-4096 body."""
    too_long = ValueError(
        "HTTP 400 from https://api.telegram.org/bot***/sendMessage: "
        "Bad Request: message is too long"
    )
    assert _is_telegram_message_too_long_error(too_long) is True
    # Re-rendering as plain text does not shorten a too-long message, so this
    # must NOT route into the MarkdownV2 fallback.
    assert _is_telegram_markdown_parse_error(too_long) is False

    parse_error = ValueError(
        "HTTP 400 from https://api.telegram.org/bot***/sendMessage: "
        "Bad Request: can't parse entities: Character '-' is reserved"
    )
    assert _is_telegram_message_too_long_error(parse_error) is False


# --- the zero-baseline percentage (tripl-l429.24) ---------------------------
#
# The percent gate deliberately admits anomalies with no baseline at all
# (tripl-l429.12): a scope resuming after an outage, or an event firing for the
# first time. ``percent_delta`` is stored 0.0 for those because the ratio is
# undefined and the column is NOT NULL — so the message printed the largest
# possible relative move as the smallest one.


def _zero_baseline_item() -> AlertDeliveryItem:
    """137 events against a baseline of zero — the class the gate lets through."""
    item = _item(0)
    item.scope_name = "checkout:completed"
    item.direction = "spike"
    item.actual_count = 137
    item.expected_count = 0
    item.absolute_delta = 137
    item.percent_delta = 0.0
    item.details_path = None
    item.monitoring_path = None
    return item


def _render_in(item: AlertDeliveryItem, message_format: str) -> str:
    return _build_items_text(
        [item],
        message_format=message_format,
        items_template=get_default_items_template(message_format),
    )


def test_a_zero_baseline_item_does_not_report_a_zero_percent_change() -> None:
    text = _render_in(_zero_baseline_item(), ALERT_MESSAGE_FORMAT_PLAIN)

    assert "0.0%" not in text
    assert "no baseline" in text
    # The absolute move is what there is to report, and it stays.
    assert "actual=137, expected=0, delta=137" in text


def test_the_zero_baseline_label_survives_markdownv2_escaping() -> None:
    """The label rides inside the template's escaped parens like the number did."""
    text = _render_in(_zero_baseline_item(), ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2)

    assert "\\(no baseline\\)" in text
    assert "0\\.0%" not in text


def test_the_typed_api_item_reports_no_percentage_rather_than_zero() -> None:
    """One delivery must not answer the same question two ways.

    ``AlertDeliveryDetailResponse`` carries BOTH ``payload_snapshot`` (where the
    percent has been null at a zero baseline since tripl-l429.27) and the typed
    ``items[]`` array, which used to be a bare float served straight off the
    NOT NULL column — so a single JSON body said ``null`` and ``0.0`` about the
    same number, and the typed half is the one an external consumer reads off
    the OpenAPI spec.

    The stored column is untouched: a delivery is frozen history. Only the
    outbound encoding changes, and it is enforced on the model so no future call
    site can construct the response without it.
    """
    serialized = AlertDeliveryItemResponse.model_validate(_zero_baseline_item())

    assert serialized.percent_delta is None, (
        "a zero baseline has no ratio to report; 0.0 is indistinguishable from "
        "'no change' for a consumer testing percent_delta > threshold"
    )
    assert serialized.expected_count == 0
    assert serialized.absolute_delta == 137, "the number that does mean something stays"


def test_the_typed_api_item_keeps_a_real_percentage() -> None:
    """The guard must not swallow the ordinary case."""
    serialized = AlertDeliveryItemResponse.model_validate(_item(0))

    assert serialized.percent_delta == 51.9


def test_an_ordinary_item_still_renders_its_percentage_unchanged() -> None:
    """Nothing moves for the case that has a baseline — byte for byte."""
    plain = _render_in(_item(0), ALERT_MESSAGE_FORMAT_PLAIN)
    markdown = _render_in(_item(0), ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2)

    assert "actual=15403, expected=32048, delta=16645 (51.9%)" in plain
    assert "delta=16645 \\(51\\.9%\\)" in markdown
    assert "no baseline" not in plain


def test_the_preview_says_the_same_thing_as_the_send() -> None:
    """The in-UI simulator renders through its own copy of this context.

    A rule tested in the simulator and then sent for real must not describe the
    same firing two different ways.
    """
    item = _zero_baseline_item()
    firing = SimulatedRuleFiring(
        anomaly_id=uuid.uuid4(),
        scope_type="event",
        scope_ref=item.scope_ref,
        scope_name=item.scope_name,
        event_type_id=None,
        event_id=None,
        bucket=item.bucket,
        direction=item.direction,
        actual_count=item.actual_count,
        expected_count=item.expected_count,
        absolute_delta=item.absolute_delta,
        percent_delta=item.percent_delta,
    )

    previewed = render_firing_item(
        firing,
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
        items_template=get_default_items_template(ALERT_MESSAGE_FORMAT_PLAIN),
    )

    assert previewed == _render_in(item, ALERT_MESSAGE_FORMAT_PLAIN)
    assert "no baseline" in previewed


def test_a_saved_custom_template_still_prints_the_bare_number() -> None:
    """``${percent_delta}`` is documented as a raw number and stays one.

    An operator's saved ``items_template`` is their content: no code rewrites it,
    so a rule that wrote ``${percent_delta}%`` before ``${percent_delta_label}``
    existed goes on printing ``0.0%`` at a zero baseline. Pinned here so the
    behaviour is a deliberate contract rather than an oversight — the way out is
    the operator swapping the variable, which
    ``website/docs/use/alerting.md`` ("Message templates") tells them to do.
    """
    item = _zero_baseline_item()

    raw = _build_items_text(
        [item],
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
        items_template="${scope_name}: ${percent_delta}%",
    )
    labelled = _build_items_text(
        [item],
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
        items_template="${scope_name}: ${percent_delta_label}",
    )

    assert raw == "checkout:completed: 0.0%"
    assert labelled == "checkout:completed: no baseline"


# --- the machine encodings of the same fact (tripl-l429.27) -----------------
#
# Humans are told "no baseline"; programs are handed JSON null. What neither may
# be handed is the stored 0.0 placeholder, which a consumer cannot tell apart
# from a real "no change".


def test_the_webhook_body_says_null_rather_than_zero_at_a_zero_baseline() -> None:
    payload = _webhook_item_payload(_zero_baseline_item())

    assert payload["percent_delta"] is None, (
        "0.0 in the webhook body reads as 'no change' on the anomaly that "
        "moved the most; expected_count sits beside it but a consumer testing "
        "percent_delta alone has no way to know that"
    )
    # The numbers that do mean something are untouched.
    assert payload["expected_count"] == 0
    assert payload["actual_count"] == 137
    assert payload["absolute_delta"] == 137


def test_the_webhook_body_still_carries_the_number_when_there_was_a_baseline() -> None:
    payload = _webhook_item_payload(_item(0))

    assert payload["percent_delta"] == 51.9
    assert payload["expected_count"] == 32048


def _snapshot_items(expected_count: float) -> list[dict[str, object]]:
    """``payload_snapshot["items"]`` for one anomaly with the given baseline."""
    from tripl.models.metric_anomaly import MetricAnomaly

    anomaly = MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        scope_type="event",
        scope_ref="event-1",
        event_id=None,
        event_type_id=None,
        bucket=datetime(2026, 8, 4, 19, tzinfo=UTC),
        direction="spike",
        actual_count=137,
        expected_count=expected_count,
        stddev=1.0,
        z_score=9.9,
    )
    snapshot = _build_delivery_snapshot(
        ScanConfig(
            id=uuid.uuid4(),
            data_source_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            name="Hourly scan",
            base_query="SELECT 1",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        ),
        project_slug="checkout",
        # Required, and passed EMPTY on purpose (tripl-0zpq.109). The snapshot
        # builder now takes the base URL from its caller instead of opening its
        # own session to read the runtime config, which is what kept these
        # otherwise-pure percentage tests reaching for a database. Every
        # assertion below reads ``percent_delta`` / ``expected_count``; not one
        # reads a link, so the base is deliberately the one value that cannot
        # make a link look right by accident.
        app_base_url="",
        rule=AlertRule(id=uuid.uuid4(), destination_id=uuid.uuid4(), name="Volume drops"),
        destination=AlertDestination(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            type=AlertDestinationType.webhook.value,
            name="Ops Webhook",
        ),
        anomalies=[anomaly],
        scope_names={("event", "event-1"): "checkout:completed"},
        delivery_id=uuid.uuid4(),
    )
    items = snapshot["items"]
    assert isinstance(items, list)
    return items


def test_the_frozen_delivery_snapshot_says_null_at_a_zero_baseline() -> None:
    """The audit/Inbox blob is read as JSON too, so it gets the same encoding."""
    (item,) = _snapshot_items(0.0)

    assert item["percent_delta"] is None
    assert item["expected_count"] == 0
    assert item["absolute_delta"] == 137


def test_the_frozen_delivery_snapshot_keeps_the_ratio_when_there_was_one() -> None:
    (item,) = _snapshot_items(100.0)

    assert item["percent_delta"] == pytest.approx(37.0)
    assert item["expected_count"] == 100


def test_a_fractional_baseline_survives_the_snapshot_intact() -> None:
    """A baseline below 1 is a real baseline, not a missing one.

    The snapshot used to round ``expected_count`` for output. Rounding the
    percent GATE to match it — the obvious way to stop ``expected_count: 0``
    appearing beside a non-null percent — silently reclassified every fractional
    baseline as "no baseline": 0.2 became ``expected_count: 0, percent_delta:
    null`` here, while ``AlertDeliveryItemResponse`` and the webhook kept 0.2
    and the real percentage off the same stored row. One delivery answered the
    same question two ways, which is the defect the null encoding exists to
    prevent.

    0.2 is not a contrived value: ``test_a_fractional_baseline_below_one_keeps_
    its_full_percent`` pins it as a 350% move that must clear a 300% threshold.
    Ratio-shaped catalog metrics live down here.
    """
    (item,) = _snapshot_items(0.2)

    assert item["expected_count"] == pytest.approx(0.2), (
        "a fractional baseline must survive as itself; rounding it to 0 tells "
        "every consumer there was no baseline at all"
    )
    assert item["percent_delta"] is not None, "there WAS a baseline, so there is a ratio to report"
    # |137 - 0.2| / 0.2 * 100
    assert item["percent_delta"] == pytest.approx(68400.0)
    assert item["absolute_delta"] == pytest.approx(136.8)
