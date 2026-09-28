"""What a release-regression alert links to, and what its line claims.

The trigger was a real alert:

    - Release regression spot:open:wind: down, actual=345, expected=715.7,
      delta=370.7 (51.8%)
      release: dropped in 15.7.5 (was 15.7.4)
      details: https://tripl.example.com/p/acme-ios/monitoring/event/c36b3ba...

Two defects, both the product's fault:

(a) The link opened the event's monitoring page, which charts the event across
    ALL versions over the chart's own range against the seasonal baseline. The
    alert's numbers come from one release's cohort over the activation-anchored
    rollout-overlap window. Different numerator, denominator, window and
    estimator — the page could not corroborate the alert even in principle.

(b) The line never said what it measured, so ``expected=715.7`` read as a raw
    count and the obvious reply was "so what if it's lower, the release only
    just rolled out". That objection is exactly what the normalization already
    removes.

(c) Fixing (a) over-reached: the audit link was handed to variable value drift
    as well, which cost that scope a working event link and gained it a page
    showing strictly less than the message. Section (c) below re-derives the
    membership rule scope by scope and pins it.

These tests are pure: no DB, no network. The URL builders are HANDED their base
URL: ``_build_item_paths`` takes a required ``app_base_url`` and reads nothing
back out of ``app_settings``, so ``BASE`` is passed to each call here the way
``dispatch._create_deliveries`` passes the value it resolved once for the whole
delivery. Nothing in this file stubs a settings read, and nothing needs to.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime

import pytest

from tripl.alert_templates import (
    get_default_items_template,
    get_default_message_template,
    render_alert_template,
)
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.domain_enums import MetricScopeType
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.worker.tasks.alerts_messages import (
    _build_item_template_context,
    telegram_message_length,
)
from tripl.worker.tasks.metrics.urls import (
    _SCOPES_LINKED_TO_ALERT_AUDIT,
    _build_item_paths,
)

BASE = "https://tripl.example"
SLUG = "acme-ios"
# F20 PR8: every deep link names the organization; the suite runs in default.
ORG = "default"

# The window the real alert measured over: 2026-07-24T09:00Z -> 2026-07-26T12:00Z.
WINDOW_FROM = datetime(2026, 7, 24, 9, tzinfo=UTC)
WINDOW_TO = datetime(2026, 7, 26, 12, tzinfo=UTC)


def _release_regression_item(
    *,
    event_id: uuid.UUID | None = None,
    event_type_id: uuid.UUID | None = None,
    window_from: datetime | None = WINDOW_FROM,
    actual: float = 345.0,
    expected: float = 715.7,
    kind: str = "volume_drop",
) -> AlertDeliveryItem:
    """The owner's alert, as an unsaved delivery item."""
    absolute_delta = abs(actual - expected)
    return AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
        scope_type="release_regression",
        scope_ref=str(event_id or event_type_id or uuid.uuid4()),
        scope_name="spot:open:wind:",
        event_id=event_id,
        event_type_id=event_type_id,
        bucket=WINDOW_TO,
        direction="drop",
        actual_count=actual,
        expected_count=expected,
        absolute_delta=absolute_delta,
        percent_delta=(absolute_delta / expected * 100) if expected > 0 else 0.0,
        drift_type=kind,
        drift_field="15.7.5",
        sample_value="15.7.4",
        window_from=window_from,
    )


def _full_item(item: AlertDeliveryItem) -> AlertDeliveryItem:
    """The same item with the links it really ships with.

    ``_release_regression_item`` leaves both paths NULL, so a length measured
    over it silently excludes the longest line the item has. Anything asserting
    against the 4096 ceiling has to pay for the URL.
    """
    item.details_path, item.monitoring_path = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type=item.scope_type,
        scope_ref=item.scope_ref,
        event_id=item.event_id,
        delivery_id=item.delivery_id,
    )
    return item


def _render(item: AlertDeliveryItem, message_format: str = "plain") -> str:
    return render_alert_template(
        get_default_items_template(message_format),
        _build_item_template_context(item, message_format=message_format),
    )


# --------------------------------------------------------------------------
# (a) the link
# --------------------------------------------------------------------------


def test_a_release_regression_never_links_to_the_event_monitoring_page() -> None:
    """The page that cannot corroborate the alert must not be the destination.

    This is the exact link the owner clicked. ``_build_event_details_url`` took
    no scope_type at all and fired for anything with a non-null ``event_id``,
    so an event-scoped release regression got the event's own chart.
    """
    event_id = uuid.uuid4()
    delivery_id = uuid.uuid4()

    details_path, monitoring_path = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type="release_regression",
        scope_ref=str(event_id),
        event_id=event_id,
        delivery_id=delivery_id,
    )

    assert f"{BASE}/o/{ORG}/p/{SLUG}/monitoring/event/{event_id}" not in (
        details_path,
        monitoring_path,
    )
    # Section (d) pins the ``?item=`` anchor; here only the destination matters.
    assert (details_path or "").startswith(f"{BASE}/o/{ORG}/p/{SLUG}/alerting/{delivery_id}")
    # One link, not two labels on the same URL: there is no monitoring page.
    assert monitoring_path is None


def test_an_event_type_scoped_regression_does_not_get_an_event_url() -> None:
    """The second, quieter defect: an event-TYPE id inside the event route.

    For these rows ``event_id`` is NULL and ``scope_ref`` is the event_type_id,
    which the old fallthrough dropped into /monitoring/event/{...} — a
    valid-looking URL for a page that does not exist.
    """
    event_type_id = uuid.uuid4()
    delivery_id = uuid.uuid4()

    details_path, monitoring_path = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type="release_regression",
        scope_ref=str(event_type_id),
        event_id=None,
        delivery_id=delivery_id,
    )

    assert f"/monitoring/event/{event_type_id}" not in (details_path or "")
    assert f"/monitoring/event/{event_type_id}" not in (monitoring_path or "")
    assert (details_path or "").startswith(f"{BASE}/o/{ORG}/p/{SLUG}/alerting/{delivery_id}")


def test_a_regression_without_a_delivery_id_gets_no_link_rather_than_a_wrong_one() -> None:
    details_path, monitoring_path = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type="release_regression",
        scope_ref=str(uuid.uuid4()),
        event_id=uuid.uuid4(),
        delivery_id=None,
    )
    assert details_path is None
    assert monitoring_path is None


@pytest.mark.parametrize(
    ("scope_type", "expected_details", "expected_monitoring"),
    [
        ("event", "/monitoring/event/{event_id}", "/monitoring/event/{scope_ref}"),
        ("event_type", "/monitoring/event/{event_id}", "/monitoring/event-type/{scope_ref}"),
        (
            "project_total",
            "/monitoring/event/{event_id}",
            "/monitoring/project-total/{scope_ref}",
        ),
        ("schema", "/monitoring/event/{event_id}", None),
        ("distribution", "/monitoring/event/{event_id}", None),
    ],
)
def test_every_other_scope_keeps_the_links_it_had(
    scope_type: str,
    expected_details: str,
    expected_monitoring: str | None,
) -> None:
    """The fix is surgical: nothing but the named scopes may move.

    Asserted rather than eyeballed, because the change replaced a fallthrough
    that every one of these scopes passed through.

    This is the NO-INCIDENT path — pre-tripl-jfm3.91 rows, whose items carry no
    ``correlation_group_id``. When there is an incident to point at, every scope
    links to it instead; see the test below.
    """
    event_id = uuid.uuid4()
    scope_ref = str(uuid.uuid4())

    details_path, monitoring_path = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type=scope_type,
        scope_ref=scope_ref,
        event_id=event_id,
        delivery_id=uuid.uuid4(),
    )

    prefix = f"{BASE}/o/{ORG}/p/{SLUG}"
    assert details_path == prefix + expected_details.format(event_id=event_id, scope_ref=scope_ref)
    if expected_monitoring is None:
        assert monitoring_path is None
    else:
        assert monitoring_path == prefix + expected_monitoring.format(
            event_id=event_id, scope_ref=scope_ref
        )


@pytest.mark.parametrize(
    "scope_type",
    ["event", "event_type", "project_total", "schema", "distribution", "release_regression"],
)
def test_every_scope_links_to_the_incident_when_there_is_one(scope_type: str) -> None:
    """One destination per alert, whatever fired it: the incident.

    Only release regressions used to reach the alerting page; an anomaly alert
    linked to the event/monitoring page, which shows neither what was sent nor
    Ack / Resolve / Mute. Acting on a telegram alert therefore meant leaving the
    page it opened and finding the matching incident by hand (tripl-pq97).

    The delivery id and item anchor stay in the URL — they still select the exact
    row the message quoted, out of up to 8 packed into one delivery — and
    ``monitoring_path`` stays empty so the message keeps carrying one link.
    """
    delivery_id = uuid.uuid4()
    group_id = uuid.uuid4()

    details_path, monitoring_path = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type=scope_type,
        scope_ref=str(uuid.uuid4()),
        event_id=uuid.uuid4(),
        delivery_id=delivery_id,
        correlation_group_id=group_id,
    )

    assert details_path is not None
    assert details_path.startswith(f"{BASE}/o/{ORG}/p/{SLUG}/alerting/{delivery_id}?")
    assert f"incident={group_id}" in details_path
    assert "item=" in details_path
    assert monitoring_path is None


def test_a_catalog_metric_links_to_the_metric_drilldown_not_the_event_route() -> None:
    """Same fallthrough, quieter victim: scope_ref is a metric_definition_id."""
    metric_id = uuid.uuid4()
    _, monitoring_path = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type="metric",
        scope_ref=str(metric_id),
        event_id=None,
        delivery_id=uuid.uuid4(),
    )
    assert monitoring_path == f"{BASE}/o/{ORG}/p/{SLUG}/monitoring/metric/{metric_id}"


def test_the_base_url_is_handed_in_and_has_no_default() -> None:
    """Where every ``BASE`` above comes from, pinned as a contract.

    These builders used to open by reading the base URL back out of
    ``app_settings`` themselves, and this file used to stub that read with an
    autouse fixture. ``_build_item_paths`` is now the only writer of
    ``AlertDeliveryItem.details_path`` / ``monitoring_path`` and of the matching
    pair in ``payload_snapshot``, so it mints every link an alert carries, and
    it takes the value as a REQUIRED keyword-only argument precisely so a new
    call site cannot ship link-less alerts in silence (see the ``urls`` module
    docstring). Giving the parameter a default would leave every call above
    passing while quietly reopening that hole, so requiredness is asserted
    rather than assumed.
    """
    parameter = inspect.signature(_build_item_paths).parameters["app_base_url"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


# --------------------------------------------------------------------------
# (b) the message
# --------------------------------------------------------------------------


def test_the_expected_count_is_labelled_where_a_reader_would_misread_it() -> None:
    """The qualifier has to sit ON the number, not a line below it."""
    rendered = _render(_release_regression_item(event_id=uuid.uuid4()))
    assert "expected=715.7 (adoption-adjusted)" in rendered


def test_the_release_line_says_the_expectation_came_from_the_new_release() -> None:
    """The single fact that kills the "not everyone updated yet" misreading.

    Once the line says 715.7 was built FROM 15.7.5's own volume, low adoption
    is already priced in: a smaller cohort shrinks total_new and shrinks
    expected with it. Everything else in the line is corroboration.
    """
    rendered = _render(_release_regression_item(event_id=uuid.uuid4()))
    assert "15.7.4's share of this event at 15.7.5's own volume" in rendered
    assert "so 51.8% is share-for-share" in rendered


def test_the_release_line_names_the_window_it_measured() -> None:
    """Not the chart's 7 days: the activation-anchored rollout overlap."""
    rendered = _render(_release_regression_item(event_id=uuid.uuid4()))
    assert "over the 51h rollout overlap" in rendered


def test_the_window_clause_is_dropped_when_the_window_is_unknown() -> None:
    """Items delivered before window_from existed must still render."""
    rendered = _render(_release_regression_item(event_id=uuid.uuid4(), window_from=None))
    assert "rollout overlap" not in rendered
    # The clause that does the actual work survives.
    assert "at 15.7.5's own volume" in rendered


def test_an_event_type_scoped_regression_calls_its_scope_an_event_type() -> None:
    rendered = _render(_release_regression_item(event_type_id=uuid.uuid4()))
    assert "share of this event type at" in rendered


def test_a_zero_baseline_regression_quotes_no_ratio() -> None:
    """``expected`` 0 means there is nothing to normalize and no ratio exists.

    ``format_percent_delta`` already says "no baseline" there; printing the
    formula would present a zero as an expectation.
    """
    item = _release_regression_item(event_id=uuid.uuid4(), actual=0.0, expected=0.0, kind="missing")
    rendered = _render(item)
    assert "adoption-adjusted" not in rendered
    assert "share-for-share" not in rendered
    assert "release: disappeared in 15.7.5 vs 15.7.4" in rendered


def test_other_scopes_get_no_expected_basis_qualifier() -> None:
    """``${expected_basis}`` must be invisible everywhere else."""
    item = AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
        scope_type="event",
        scope_ref=str(uuid.uuid4()),
        scope_name="Login",
        event_id=uuid.uuid4(),
        event_type_id=None,
        bucket=WINDOW_TO,
        direction="drop",
        actual_count=345,
        expected_count=715.7,
        absolute_delta=370.7,
        percent_delta=51.8,
        drift_type=None,
        drift_field=None,
        sample_value=None,
        window_from=None,
    )
    rendered = _render(item)
    assert "expected=715.7," in rendered
    assert "adoption-adjusted" not in rendered
    # And no unresolved placeholder leaked through.
    assert "${" not in rendered


@pytest.mark.parametrize(
    "message_format",
    ["plain", "slack_mrkdwn", "telegram_html", "telegram_markdownv2"],
)
def test_no_message_format_leaks_an_unrendered_placeholder(message_format: str) -> None:
    """A new template variable missing from one context builder prints raw."""
    rendered = _render(_release_regression_item(event_id=uuid.uuid4()), message_format)
    assert "${" not in rendered


def test_markdownv2_escapes_the_new_clauses() -> None:
    """Telegram rejects an unescaped '.', '-' or '(' in MarkdownV2."""
    rendered = _render(_release_regression_item(event_id=uuid.uuid4()), "telegram_markdownv2")
    assert "\\(adoption\\-adjusted\\)" in rendered
    assert "15\\.7\\.5" in rendered
    # Nothing double-escaped: the basis clause is escaped once, by its caller.
    assert "\\\\" not in rendered


@pytest.mark.parametrize(
    "message_format",
    ["plain", "telegram_html", "telegram_markdownv2"],
)
def test_a_full_eight_item_delivery_still_fits_one_telegram_message(
    message_format: str,
) -> None:
    """The added text is measured, not assumed.

    Telegram counts the 4096 ceiling in UTF-16 code units and dispatch chunks
    at 8 items per delivery, so the worst case that must survive is a full
    chunk of the longest item type — carrying the links it really ships with,
    in the format that escapes the most characters.

    Overshooting is survivable (``split_telegram_messages`` splits, never
    truncates), so this is a headroom guard rather than a correctness one; it
    exists because the per-item anchor is the second thing to lengthen this
    line and the next one should have to look at the number.
    """
    items = [_full_item(_release_regression_item(event_id=uuid.uuid4())) for _ in range(8)]
    items_text = "\n".join(_render(item, message_format).rstrip() for item in items)
    message = render_alert_template(
        get_default_message_template(message_format),
        _build_item_template_context(items[0], message_format=message_format),
    )
    body = message + items_text
    assert telegram_message_length(body) < 4096


# --------------------------------------------------------------------------
# (d) WHICH item the link identifies
# --------------------------------------------------------------------------
#
# ``_build_alert_audit_url`` keyed only on ``delivery_id``, but a delivery is
# not an item: ``_delivery_chunks`` packs up to 8 items into one telegram
# delivery (``_MAX_ITEMS_PER_DELIVERY``). A rule matching 8 regressed scopes
# therefore printed 8 byte-identical "details:" lines, and clicking the sixth
# opened a row expanded over 8 siblings with nothing marking the one the
# message quoted. The alert that prompted this work carried a single item,
# which is the only reason it looked right.


def test_two_items_in_one_delivery_do_not_share_a_link() -> None:
    """The defect, stated at its smallest: one delivery, two scopes.

    Both lines are printed in the same message, so identical URLs are not a
    near-miss — they are the message claiming a precision it does not have.
    """
    delivery_id = uuid.uuid4()
    first_event = uuid.uuid4()
    second_event = uuid.uuid4()

    first, _ = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type="release_regression",
        scope_ref=str(first_event),
        event_id=first_event,
        delivery_id=delivery_id,
    )
    second, _ = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type="release_regression",
        scope_ref=str(second_event),
        event_id=second_event,
        delivery_id=delivery_id,
    )

    assert first != second


def test_the_audit_link_names_the_item_it_was_printed_for() -> None:
    """The anchor is a query param, the state convention this frontend uses.

    Every other cross-page focus handle here rides ``useSearchParams``
    (branch-context, saved views, the events route state, the metrics
    catalog); nothing in the app reads ``location.hash``. So ``?item=`` is
    what the landing page already knows how to pick up.
    """
    event_id = uuid.uuid4()
    delivery_id = uuid.uuid4()

    details_path, _ = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type="release_regression",
        scope_ref=str(event_id),
        event_id=event_id,
        delivery_id=delivery_id,
    )

    assert details_path == (
        f"{BASE}/o/{ORG}/p/{SLUG}/alerting/{delivery_id}?item=release_regression:{event_id}"
    )


def test_the_anchor_carries_the_scope_type_because_scope_ref_alone_collides() -> None:
    """``scope_ref`` is not an identity on its own, even inside one delivery.

    A release regression's ``scope_ref`` IS its event id (see
    ``_event_generator_merge``: ``scope_ref=str(target.id)``), and one rule can
    set ``include_events`` and ``include_release_regressions`` together
    (``alerting_matching``), so both items land in the same delivery carrying
    the same ``scope_ref``. Anchoring on the ref alone would highlight two rows
    and the reader would be back to guessing.
    """
    shared_event_id = uuid.uuid4()
    delivery_id = uuid.uuid4()

    regression_details, _ = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type="release_regression",
        scope_ref=str(shared_event_id),
        event_id=shared_event_id,
        delivery_id=delivery_id,
    )

    assert regression_details is not None
    anchor = regression_details.partition("?item=")[2]
    scope_type, _, scope_ref = anchor.partition(":")
    assert scope_type == "release_regression"
    assert scope_ref == str(shared_event_id)


def test_the_anchor_is_url_safe_for_a_scope_ref_that_is_not_a_uuid() -> None:
    """``_SCOPES_LINKED_TO_ALERT_AUDIT`` is a set, and it has grown before.

    Only release regression is in it today and its ``scope_ref`` is a UUID, but
    ``AlertDeliveryItem.scope_ref`` is a free-form ``String(64)`` — the drift
    scopes key theirs on a field name. Percent-encoding the value costs
    nothing for a UUID and stops a future member from emitting a URL that
    breaks at the first ``&`` or space.
    """
    audit_scope = next(iter(_SCOPES_LINKED_TO_ALERT_AUDIT))
    details_path, _ = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type=audit_scope,
        scope_ref="checkout total&spend=1",
        event_id=uuid.uuid4(),
        delivery_id=uuid.uuid4(),
    )

    assert details_path is not None
    query = details_path.partition("?")[2]
    # One parameter, whatever the ref contained.
    assert query.count("=") == 1
    assert "&" not in query
    assert " " not in query


def test_the_per_item_anchor_does_not_leak_into_scopes_that_have_a_real_page() -> None:
    """Monitoring links address an entity; they must not grow a delivery query."""
    for scope_type in ("event", "event_type", "project_total", "metric"):
        details_path, monitoring_path = _build_item_paths(
            SLUG,
            org_slug=ORG,
            app_base_url=BASE,
            scope_type=scope_type,
            scope_ref=str(uuid.uuid4()),
            event_id=uuid.uuid4(),
            delivery_id=uuid.uuid4(),
        )
        assert "?item=" not in (details_path or "")
        assert "?item=" not in (monitoring_path or "")


# --------------------------------------------------------------------------
# (c) who the audit link is for
# --------------------------------------------------------------------------
#
# The audit row is a downgrade for every scope that has a real page, because it
# renders a fixed 8 columns — Grp / Scope / Direction / Actual / Expected /
# Abs delta / % delta / Link (frontend AlertDeliveryRow.tsx) — and none of them
# is ``drift_field`` or ``sample_value``. For a drift scope those two fields
# ARE the finding, so sending a drift item there shows less than the message
# the reader is already holding.
#
# It is an upgrade for exactly one scope: release regression, whose numbers no
# page can reproduce. So the rule is a last resort, not a default:
#
#     link an item to the audit row only when NO page shows more than
#     the message does.
#
# Scope by scope, as of this test:
#
#   project_total / event_type / event / metric
#       Have their own monitoring pages, computed the same way the alert was.
#       Never audit-linked.
#   variable_value_drift
#       ``VariableValueDrift.event_id`` is NOT NULL, and
#       ``/p/{slug}/monitoring/event/{event_id}`` mounts EventValueDriftPanel
#       (MonitoringDetailPage.tsx), which lists the variable name, EVERY
#       observed value — not the 500-char truncation the message sends — and
#       the Accept / Accept for event / Snooze / False positive actions.
#       Strictly more than both the audit row and the message. Never
#       audit-linked.
#   schema / distribution
#       Carry ``event_type_id`` and a NULL ``event_id``, so no event link
#       exists to build, and their scope_ref is a drift row / field key that
#       no route accepts. They get no link at all, which is honest; the audit
#       row would not fix that, because it drops their ``drift_field`` and
#       ``sample_value`` too. Not audit-linked.
#   release_regression
#       The one scope whose actual/expected/% delta exist nowhere else: the
#       monitoring rows are recomputed per scan and only ever describe the
#       CURRENT latest release. Audit-linked.

# Every MetricScopeType member, with the decision and why. Keyed by the enum so
# a newly added scope cannot slip through undecided — see the coverage test.
_AUDIT_LINK_DECISION: dict[MetricScopeType, bool] = {
    MetricScopeType.project_total: False,
    MetricScopeType.event_type: False,
    MetricScopeType.event: False,
    MetricScopeType.metric: False,
    MetricScopeType.schema: False,
    MetricScopeType.distribution: False,
    MetricScopeType.variable_value_drift: False,
    MetricScopeType.release_regression: True,
    # A whole scan config; no page shows more than the message, and every
    # delivered item reaches its incident through the correlation link anyway.
    MetricScopeType.source_freshness: False,
    # An event lifecycle finding (#258): the event page shows the finding and
    # the migration numbers; every item reaches its incident anyway.
    MetricScopeType.lifecycle: False,
}


def test_value_drift_links_to_the_event_its_variable_is_anchored_to() -> None:
    """The regression: value drift had a working link and lost it.

    ``/p/{slug}/monitoring/event/{event_id}`` mounts EventValueDriftPanel,
    which shows the variable and every observed value and can resolve the
    drift. The audit row shows ``actual = len(observed_values)`` against a
    hardcoded ``expected = 0`` and names neither the variable nor a value.
    """
    event_id = uuid.uuid4()
    delivery_id = uuid.uuid4()

    details_path, monitoring_path = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type="variable_value_drift",
        # scope_ref is the VariableValueDrift row id, which no route accepts.
        scope_ref=str(uuid.uuid4()),
        event_id=event_id,
        delivery_id=delivery_id,
    )

    assert details_path == f"{BASE}/o/{ORG}/p/{SLUG}/monitoring/event/{event_id}"
    # The scope still has no monitoring DETAIL route of its own; the round that
    # made ``_build_monitoring_url`` exhaustive got that half right, and the
    # row id must never reappear inside /monitoring/event/.
    assert monitoring_path is None


def test_value_drift_is_not_sent_to_the_audit_row() -> None:
    """Stated as its own claim, because it is the exact line that regressed."""
    delivery_id = uuid.uuid4()
    details_path, monitoring_path = _build_item_paths(
        SLUG,
        org_slug=ORG,
        app_base_url=BASE,
        scope_type="variable_value_drift",
        scope_ref=str(uuid.uuid4()),
        event_id=uuid.uuid4(),
        delivery_id=delivery_id,
    )
    # Prefix, not equality: the audit URL now carries a ``?item=`` anchor, and
    # an equality check would pass against any anchored variant of it.
    audit_prefix = f"{BASE}/o/{ORG}/p/{SLUG}/alerting/{delivery_id}"
    assert not (details_path or "").startswith(audit_prefix)
    assert not (monitoring_path or "").startswith(audit_prefix)


def test_every_value_drift_has_an_event_to_link_to() -> None:
    """Not luck: the column is NOT NULL, so the link can never be missing.

    If this ever becomes nullable, ``details_path`` starts coming back None for
    some value drifts and the scope needs a fallback destination decided.
    """
    assert VariableValueDrift.__table__.c.event_id.nullable is False


@pytest.mark.parametrize(
    ("scope_type", "is_audit_linked"),
    [(scope.value, decision) for scope, decision in _AUDIT_LINK_DECISION.items()],
)
def test_only_release_regression_is_linked_to_the_alert_audit_row(
    scope_type: str,
    is_audit_linked: bool,
) -> None:
    """Pins the membership rule so the set cannot quietly grow again.

    It grew once already: adding ``variable_value_drift`` traded that scope's
    working event link for a page showing strictly less.
    """
    assert (scope_type in _SCOPES_LINKED_TO_ALERT_AUDIT) is is_audit_linked


def test_the_audit_link_decision_covers_every_scope() -> None:
    """A new MetricScopeType member must be decided, not defaulted.

    The parametrized test above only asks about the scopes this table names, so
    on its own it would never notice a ninth scope, nor a typo'd string sitting
    in the set matching nothing. Both are checked here.
    """
    assert set(_AUDIT_LINK_DECISION) == set(MetricScopeType)
    known_scopes = {scope.value for scope in MetricScopeType}
    unknown_scopes = _SCOPES_LINKED_TO_ALERT_AUDIT - known_scopes
    assert unknown_scopes == set()
