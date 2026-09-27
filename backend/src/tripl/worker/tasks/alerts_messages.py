"""Message-building helpers for alert deliveries.

Pure rendering/formatting logic extracted from alerts.py so that module stays
under a manageable size.  Nothing here touches Celery or outbound HTTP — all
I/O lives in alerts.py.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from html import unescape

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    ALERT_MESSAGE_FORMAT_TELEGRAM_HTML,
    ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2,
    AlertTemplateContext,
    DriftLineFacts,
    alert_scope_label,
    build_drift_line,
    escape_alert_value,
    format_alert_bold,
    format_alert_link,
    format_metric_alert_value,
    format_percent_delta,
    get_default_items_template,
    get_default_message_template,
    get_digest_items_template,
    get_digest_message_template,
    has_baseline,
    normalize_message_template,
    percent_delta_or_none,
    plain_alert_number,
    release_regression_basis,
    render_alert_template,
    source_freshness_line,
)
from tripl.alerting_matching import (
    SCOPE_METRIC,
    SCOPE_RELEASE_REGRESSION,
    SCOPE_SOURCE_FRESHNESS,
)
from tripl.anomaly_context import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_PROJECT_TOTAL,
    build_alert_item_context,
)
from tripl.core.alert_schedule import resolve_timezone
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import DistributionDriftBand
from tripl.models.event import Event, EventStatus
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SchemaDrift
from tripl.services import app_settings_service, llm_service
from tripl.services.attribution_text import attribution_line_for_scope

logger = logging.getLogger(__name__)

DIGEST_WINDOW_DAYS = 7
DEAD_EVENT_DAYS = 30

# How many overdue events the daily sunset alert NAMES. Its "Count:" line is the
# true total either way, resolved by its own COUNT(*) — the same split
# _build_plan_digest_message makes between the counters it reports and the
# ".limit(5)" its "Top anomalies" list is drawn from.
#
# Capped because that message is an outbound payload on a timer: beat's
# "check-deprecated-sunset-events" entry sends it once a day to every enabled
# Slack and email destination, and the list only grows — nothing takes an event
# off it but retiring the event or clearing its sunset_at, since last_seen_at is
# monotonic (metrics.collect._bump_event_last_seen only ever moves it forward).
# Uncapped, the size of that daily payload is bounded by nothing except how many
# deprecated events the project has left running.
#
# 50 and not the digest's 5, because naming the events is the whole reason this
# message exists beside the count. 50 and not more, because ``events.name`` is
# String(500): at 50 lines even all-maximum-width names render under 28k
# characters and stay inside the 40,000 Slack accepts in a "text" field. Past
# that ceiling the POST is REJECTED rather than truncated, and
# check_deprecated_sunset_events turns the raise into a logger.warning and a
# "failed" tally — so the alert would stop arriving and say so nowhere its
# reader looks.
_SUNSET_ALERT_MAX_EVENTS = 50

_AI_EXPLANATION_MAX_ITEMS = 10

# A digest is one delivery covering a whole window, so the immediate path's cap
# would write the note from the first ten of twenty-four items and say nothing
# about the rest — the note would confidently describe less than half the
# morning. Bounded rather than unbounded because the prompt is one LLM
# round-trip inside the send task.
DIGEST_AI_EXPLANATION_MAX_ITEMS = 40
_AI_EXPLANATION_MAX_TOKENS = 250

# How much of what this rule already said about these same scopes goes into the
# prompt. The explanation used to be a pure function of the current bucket, so
# an event drifting for the third day running produced the same paragraph three
# times and the reader learned nothing from the repeat (tripl-ikee).
_AI_HISTORY_MAX_DELIVERIES = 3
_AI_HISTORY_WINDOW = timedelta(days=7)
# Prior explanations are 2-4 sentences; keep a readable head of each so three of
# them cannot crowd out the current items.
_AI_HISTORY_EXPLANATION_CHARS = 320

# Telegram's sendMessage rejects a body over 4096 characters with HTTP 400
# "Bad Request: message is too long".
#
# The ceiling is on rendered LENGTH, not item count. Across the 29 deliveries
# this instance has sent (48 rendered items), a single item runs 97-389
# characters depending on how many of the optional
# drift/details/monitoring/movers/trend lines it carries — a 4x spread, so any
# item count safe for a schema-drift rule overshoots for an event rule carrying
# URLs. At the measured 355-character mean the items alone pass 4096 at 12
# items, and the whole message passes it at 10.
TELEGRAM_MESSAGE_MAX_CHARS = 4096


def telegram_message_length(text: str) -> int:
    """Length of ``text`` the way Telegram counts it.

    The 4096 ceiling is counted in UTF-16 code units — the same units the API
    uses for entity offsets — while ``len`` counts code points. Every character
    outside the BMP (emoji, and the symbols an event name can carry) therefore
    costs two where ``len`` charges one, so a body of 4000 code points can be
    refused at 4400 units. Budgeting in ``len`` is how the previous attempt at
    this ceiling let an oversized message through.
    """
    return len(text.encode("utf-16-le")) // 2


# Markup Telegram parses away before it counts: a tag pair, and the HTML
# entities the escaper produces. Anchored deliberately at a tag-shaped token
# rather than anything greedier, so a literal "<" that survived escaping (it
# cannot, but the length budget must not depend on that) is still counted.
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
# Both slots step over an escaped pair before testing for their terminator,
# because MarkdownV2 escaping puts the terminator itself INSIDE them: a scope
# name holding "]" reaches the label as "\]", and a naive "[^\]]*" cannot cross
# it — the link then matches nothing and stays in the count, URL and all. The
# measurement over-counts and splits a message that would have fitted, silently
# undoing the saving links exist for.
_MARKDOWNV2_LINK_RE = re.compile(
    r"\[((?:\\.|[^\]\\])*)\]\((?:\\.|[^)\\])*\)",
    re.DOTALL,
)
_MARKDOWNV2_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def telegram_visible_length(text: str, message_format: str) -> int:
    """Length of ``text`` as Telegram counts it AFTER parsing entities.

    The 4096 ceiling applies to what the reader sees, not to the wire bytes: a
    200-character URL hidden behind a 20-character link label costs 20. Counting
    the raw body instead is what made hyperlinks buy nothing — the URL simply
    moved from a visible line into an ``href`` and kept splitting the message.

    Measured on a real 24-item digest: 5,323 raw units against 1,061 visible.
    The difference is the whole feature.
    """
    if message_format == ALERT_MESSAGE_FORMAT_TELEGRAM_HTML:
        # Strip tags FIRST, then unescape: doing it the other way round would
        # turn an escaped "&lt;b&gt;" from a scope name into a tag and delete
        # text the reader can actually see.
        return telegram_message_length(unescape(_HTML_TAG_RE.sub("", text)))
    if message_format == ALERT_MESSAGE_FORMAT_TELEGRAM_MARKDOWNV2:
        without_links = _MARKDOWNV2_LINK_RE.sub(lambda m: m.group(1), text)
        return telegram_message_length(_MARKDOWNV2_ESCAPE_RE.sub(r"\1", without_links))
    return telegram_message_length(text)


def _resolve_metric_units(
    session: Session | None,
    items: list[AlertDeliveryItem],
    cache: dict[str, str | None] | None,
) -> dict[str, str | None]:
    """Unit per metric-definition id for the delivery's metric-scope items.

    One batched query for all unresolved scope_refs; results land in ``cache``
    (when provided) so a re-render without a session — e.g. the MarkdownV2 →
    plain fallback — reuses them instead of losing the percent formatting.
    """
    units = cache if cache is not None else {}
    if session is None:
        return units
    missing = {
        item.scope_ref
        for item in items
        if item.scope_type == SCOPE_METRIC and item.scope_ref not in units
    }
    if not missing:
        return units
    metric_ids: list[uuid.UUID] = []
    for scope_ref in missing:
        try:
            metric_ids.append(uuid.UUID(scope_ref))
        except ValueError:
            units[scope_ref] = None
    if metric_ids:
        for metric_id, unit in session.execute(
            select(MetricDefinition.id, MetricDefinition.unit).where(
                MetricDefinition.id.in_(metric_ids)
            )
        ).all():
            units[str(metric_id)] = unit
    for scope_ref in missing:
        units.setdefault(scope_ref, None)
    return units


# The parenthetical that rides on ${expected_count} for the one scope whose
# expectation is not a plain baseline. See DEFAULT_ALERT_ITEMS_TEMPLATES.
_ADOPTION_ADJUSTED_LABEL = " (adoption-adjusted)"


def _drift_facts(item: AlertDeliveryItem) -> DriftLineFacts:
    """The delivered item's half of the ``${drift_line}`` contract.

    The wording itself lives in ``alert_templates.build_drift_line``, because
    the rule simulator renders the same firing from a ``SimulatedRuleFiring``
    and the two builders had drifted apart (tripl-0zpq.165). This adapter and
    its twin in ``services.alerting_rendering`` are the only production
    constructors of the facts, which is what keeps the wording from splitting
    again: a field one side forgets is a field neither side can render.

    Used by both readers of the line — the rendered message and the AI-note
    prompt — so the note cannot quote a different basis from the line it
    annotates.
    """
    return DriftLineFacts(
        scope_type=item.scope_type,
        drift_type=item.drift_type,
        drift_field=item.drift_field,
        sample_value=item.sample_value,
        expected_count=item.expected_count,
        percent_delta=item.percent_delta,
        bucket=item.bucket,
        window_from=item.window_from,
        event_id=item.event_id,
        event_type_id=item.event_type_id,
    )


# Only the volume scopes get a stored attribution (GH #255): the worker computes
# it for project_total / event_type / event anomalies and nothing else.
_ATTRIBUTION_SCOPES = frozenset({SCOPE_PROJECT_TOTAL, SCOPE_EVENT_TYPE, SCOPE_EVENT})


def _unpack_item_context(cached: tuple[str, ...]) -> tuple[str, str, str]:
    """``(sparkline, top_movers, attribution)`` from a cache entry.

    Entries were ``(sparkline, top_movers)`` before the attribution line joined
    them; a two-tuple still unpacks, with no attribution.
    """
    padded = (*cached, "", "", "")
    return padded[0], padded[1], padded[2]


def _load_item_attribution(
    session: Session,
    scan_config_id: uuid.UUID,
    item: AlertDeliveryItem,
) -> str:
    """The stored attribution one-liner for a volume item, or ``""``.

    Read from ``metric_anomaly_attributions`` — the row the metrics worker wrote
    at detection time — so the alert quotes the same numbers as the Why panel.
    Best-effort like the sparkline: a failed read costs the line, not the alert.
    """
    if item.scope_type not in _ATTRIBUTION_SCOPES:
        return ""
    try:
        with session.no_autoflush:
            return attribution_line_for_scope(
                session,
                scan_config_id=scan_config_id,
                scope_type=item.scope_type,
                scope_ref=item.scope_ref,
                bucket=item.bucket,
                direction=item.direction,
            )
    except Exception:  # noqa: BLE001
        logger.warning("Failed to load the alert item attribution", exc_info=True)
        return ""


def _build_item_template_context(
    item: AlertDeliveryItem,
    *,
    message_format: str,
    session: Session | None = None,
    scan_config_id: uuid.UUID | None = None,
    item_context_cache: dict[uuid.UUID, tuple[str, ...]] | None = None,
    metric_unit: str | None = None,
) -> AlertTemplateContext:
    scope_label = alert_scope_label(item.scope_type)
    # An event's catalog entry and its charts are one page: `/events/detail/<id>`
    # redirects to `/monitoring/event/<id>`, and that view carries the field
    # values, meta fields AND the charts. So for event-scoped rows both builders
    # produce the same URL, and printing it twice under two labels made every
    # item look like it offered a choice it did not. The two still differ for
    # event-type and project-total scopes — monitoring points at the scope's own
    # page, details at the underlying event — so the lines collapse only when
    # they genuinely coincide. Both template variables stay populated either way,
    # so custom templates referencing ${monitoring_url} are unaffected.
    details_line = f"\n  details: {item.details_path}" if item.details_path else ""
    monitoring_line = (
        f"\n  monitoring: {item.monitoring_path}"
        if item.monitoring_path and item.monitoring_path != item.details_path
        else ""
    )
    # Release regressions and value drifts reuse the same three drift columns to
    # say different things (version/kind/previous release, variable/observed
    # values), so the per-scope wording lives in one shared builder that the
    # rule simulator's preview calls too.
    drift_line = build_drift_line(_drift_facts(item))

    # Explainability context — sparkline + top movers. The (sparkline,
    # top_movers) pair is format-independent and the only DB-touching part of
    # the render, so we cache it per item: a re-render in a different format
    # (e.g. the MarkdownV2→plain fallback) reuses it instead of re-querying.
    sparkline = ""
    top_movers = ""
    attribution = ""
    cached = item_context_cache.get(item.id) if item_context_cache is not None else None
    if cached is not None:
        sparkline, top_movers, attribution = _unpack_item_context(cached)
    elif session is not None and scan_config_id is not None:
        try:
            sparkline, top_movers = build_alert_item_context(
                session,
                scan_config_id=scan_config_id,
                scope_type=item.scope_type,
                scope_ref=item.scope_ref,
                bucket=item.bucket,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Failed to build alert item context", exc_info=True)
        attribution = _load_item_attribution(session, scan_config_id, item)
        if item_context_cache is not None:
            item_context_cache[item.id] = (sparkline, top_movers, attribution)
    sparkline_line = f"\n  trend: {sparkline}" if sparkline else ""
    top_movers_line = f"\n  movers: {top_movers}" if top_movers else ""
    attribution_line = f"\n  why: {attribution}" if attribution else ""

    variables = {
        "scope_name": escape_alert_value(item.scope_name, message_format),
        "scope_type": escape_alert_value(item.scope_type, message_format),
        "scope_label": escape_alert_value(scope_label, message_format),
        "direction": escape_alert_value(item.direction, message_format),
        "direction_label": escape_alert_value(
            "up" if item.direction == "spike" else "down",
            message_format,
        ),
        # Percent-unit catalog metrics render stored fractions ×100 with a "%"
        # suffix; every other unit/scope passes the raw float through to the
        # shared stringifier unchanged. percent_delta stays a relative change.
        "actual_count": escape_alert_value(
            format_metric_alert_value(item.actual_count, metric_unit), message_format
        ),
        "expected_count": escape_alert_value(
            format_metric_alert_value(item.expected_count, metric_unit), message_format
        ),
        # Sits ON the number a reader would otherwise take for a raw count, so
        # the qualification arrives before the misreading rather than a line
        # after it. Empty for every scope whose expectation IS a baseline.
        "expected_basis": escape_alert_value(
            _ADOPTION_ADJUSTED_LABEL
            if item.scope_type == SCOPE_RELEASE_REGRESSION and has_baseline(item.expected_count)
            else "",
            message_format,
        ),
        "absolute_delta": escape_alert_value(
            format_metric_alert_value(item.absolute_delta, metric_unit), message_format
        ),
        # ${percent_delta} stays a BARE NUMBER on purpose, including the "0.0"
        # placeholder at a zero baseline. Its documented contract is a number the
        # operator composes with their own units and separators, so emitting
        # prose here would break arithmetic-shaped custom templates mid-string.
        # The default templates therefore use ${percent_delta_label} instead,
        # which is the variable that names the undefined ratio. A rule whose
        # operator SAVED a custom items_template before that switch still prints
        # "0.0%" and no code may rewrite their string — that is documented for
        # operators in website/docs/use/alerting.md ("Message templates").
        "percent_delta": escape_alert_value(f"{item.percent_delta:.1f}", message_format),
        "percent_delta_label": escape_alert_value(
            format_percent_delta(item.percent_delta, item.expected_count), message_format
        ),
        "bucket": escape_alert_value(item.bucket, message_format),
        "details_url": escape_alert_value(item.details_path or "", message_format),
        "monitoring_url": escape_alert_value(item.monitoring_path or "", message_format),
        "details_line": escape_alert_value(details_line, message_format),
        "monitoring_line": escape_alert_value(monitoring_line, message_format),
        "drift_field": escape_alert_value(item.drift_field or "", message_format),
        "drift_type": escape_alert_value(item.drift_type or "", message_format),
        "sample_value": escape_alert_value(item.sample_value or "", message_format),
        "drift_line": escape_alert_value(drift_line, message_format),
        "sparkline": escape_alert_value(sparkline, message_format),
        "top_movers": escape_alert_value(top_movers, message_format),
        "sparkline_line": escape_alert_value(sparkline_line, message_format),
        "top_movers_line": escape_alert_value(top_movers_line, message_format),
        "attribution": escape_alert_value(attribution, message_format),
        "attribution_line": escape_alert_value(attribution_line, message_format),
        # A digest groups by direction, so a reader who has scrolled past the
        # heading has nothing else telling them which way the number moved —
        # the sign alone does not, because format_percent_delta prints an
        # unsigned magnitude for a spike. Both arrows are BMP, so one UTF-16
        # unit each.
        "direction_arrow": "\u25b2" if item.direction == "spike" else "\u25bc",
        # Already-escaped markup: NOT passed through escape_alert_value, which
        # would turn the tags into literal text. format_alert_link escapes the
        # label and the URL separately, because the two slots have different
        # rules — a backslash-escaped URL inside MarkdownV2 parentheses 404s.
        "scope_link": format_alert_link(item.scope_name, item.details_path or "", message_format),
    }
    return AlertTemplateContext(variables=variables, message_format=message_format)


def _digest_headline(items: list[AlertDeliveryItem], total: int) -> str:
    """ "24 alerts · 7 down, 17 up · worst checkout:complete:annual -86%".

    Deterministic on purpose. This is the line that lands in a phone's
    notification preview, so it must be true on the delivery where the LLM is
    off, times out, or spends its first 230 characters clearing its throat —
    and it must name the worst DROP, because that is what a reader triaging a
    morning digest is looking for.
    """
    if not items:
        return f"{total} alerts"
    # ``has_baseline`` decides "new", never a sign test: a signed catalog
    # metric at a baseline of -100 is an ordinary drop or spike with a real
    # percent, and filing it under "new" both miscounted the headline and made
    # it ineligible to be named the worst mover (tripl-0zpq.102).
    downs = [i for i in items if i.direction != "spike" and has_baseline(i.expected_count)]
    ups = [i for i in items if i.direction == "spike" and has_baseline(i.expected_count)]
    new = [i for i in items if not has_baseline(i.expected_count)]
    parts = [f"{total} alerts"]
    counts = [
        f"{len(downs)} down" if downs else "",
        f"{len(ups)} up" if ups else "",
        f"{len(new)} new" if new else "",
    ]
    counted = ", ".join(part for part in counts if part)
    if counted:
        parts.append(counted)
    worst = max(downs, key=lambda i: abs(i.percent_delta), default=None) or max(
        ups, key=lambda i: abs(i.percent_delta), default=None
    )
    if worst is not None:
        arrow = "down" if worst.direction != "spike" else "up"
        parts.append(f"worst {worst.scope_name} {arrow} {abs(worst.percent_delta):.0f}%")
    return " · ".join(parts)


def _digest_window_label(items: list[AlertDeliveryItem], timezone_name: str | None) -> str:
    """The period the digest covers, in the PROJECT's clock.

    The reader set "daily at 10:00" as a wall-clock time in their own zone;
    telling them the window in UTC would make them do the arithmetic the
    schedule already did for them.
    """
    buckets = [item.bucket for item in items if item.bucket is not None]
    if not buckets:
        return ""
    zone = resolve_timezone(timezone_name)
    first = min(buckets)
    last = max(buckets)
    if first.tzinfo is None:
        first = first.replace(tzinfo=UTC)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    start = first.astimezone(zone)
    end = last.astimezone(zone)
    if start.date() == end.date():
        return f"{start:%b %d, %H:%M}–{end:%H:%M} {zone.key}"
    return f"{start:%b %d %H:%M} – {end:%b %d %H:%M} {zone.key}"


_WINDOW_LABEL_SEPARATOR = " · "


def _join_window_label(window_label: str, part_label: str) -> str:
    """The window line, carrying the part marker when the digest needs one.

    Either half may be empty — a digest whose items all lost their bucket has no
    window, and the overwhelming majority of digests are one message and have no
    part — so this never leaves a dangling separator for the reader to wonder at.
    """
    return _WINDOW_LABEL_SEPARATOR.join(part for part in (window_label, part_label) if part)


def _digest_groups(
    items: list[AlertDeliveryItem],
) -> list[tuple[str, list[AlertDeliveryItem]]]:
    """Order a digest's items the way a person triages one.

    DROPS FIRST, and that ordering is the whole point. A drop needs an existing
    baseline to be a drop at all, so it is structurally the smaller class — and
    a fall in a checkout, login or payment event is close to always the thing
    worth acting on before a rise in an impression counter. Ordered the other
    way round, on a real 24-item morning the two revenue-shaped drops sat about
    thirty phone-lines below the fold.

    NO-BASELINE ITEMS GET THEIR OWN TRAILING GROUP rather than the top of the
    spikes. Sorting by percent puts them first by construction (an undefined
    ratio has no magnitude to rank), which is exactly backwards: a counter that
    went from nothing to something is usually a new event shipping, not an
    incident, and it was crowding out the items that were.
    """
    drops, spikes, unbaselined = [], [], []
    for item in items:
        # Same predicate as ``_digest_headline`` above, from the same function:
        # the heading says "3 new" and this builds the group under it, so the
        # two cannot be allowed to bucket one item differently.
        if not has_baseline(item.expected_count):
            unbaselined.append(item)
        elif item.direction == "spike":
            spikes.append(item)
        else:
            drops.append(item)

    by_percent = lambda item: -abs(item.percent_delta)  # noqa: E731
    by_absolute = lambda item: -abs(item.actual_count - item.expected_count)  # noqa: E731
    groups: list[tuple[str, list[AlertDeliveryItem]]] = []
    if drops:
        groups.append((f"{len(drops)} down", sorted(drops, key=by_percent)))
    if spikes:
        groups.append((f"{len(spikes)} up", sorted(spikes, key=by_percent)))
    if unbaselined:
        groups.append((f"{len(unbaselined)} new", sorted(unbaselined, key=by_absolute)))
    return groups


def _build_items_text(
    items: list[AlertDeliveryItem],
    *,
    message_format: str,
    items_template: str,
    session: Session | None = None,
    scan_config_id: uuid.UUID | None = None,
    item_context_cache: dict[uuid.UUID, tuple[str, ...]] | None = None,
    metric_units_cache: dict[str, str | None] | None = None,
    digest: bool = False,
) -> str:
    """Render every item it is given, whole.

    Deliberately has no length budget of its own. It used to stop at the first
    item that would not fit and append a "+N more of 14 not shown" tail, which
    dropped matched scopes that the success path then stamped as notified. What
    a channel with a per-message ceiling does instead is carry fewer items per
    message and send more messages — see :func:`split_telegram_messages`, which
    chooses the subsets and hands them here one group at a time.
    """
    metric_units = _resolve_metric_units(session, items, metric_units_cache)

    def render_one(item: AlertDeliveryItem) -> str:
        return render_alert_template(
            items_template,
            _build_item_template_context(
                item,
                message_format=message_format,
                session=session,
                scan_config_id=scan_config_id,
                item_context_cache=item_context_cache,
                metric_unit=(
                    metric_units.get(item.scope_ref) if item.scope_type == SCOPE_METRIC else None
                ),
            ),
        ).rstrip()

    if not digest:
        return "\n".join(line for line in (render_one(item) for item in items) if line)

    # Headings are ESCAPED FIRST and bolded second. Wrapping first and escaping
    # the result turns the markup into literal text; and a heading is the one
    # string in the body built at runtime rather than read off an item, which
    # is where that has been got wrong before.
    blocks: list[str] = []
    for heading, group in _digest_groups(items):
        rendered = [line for line in (render_one(item) for item in group) if line]
        if not rendered:
            continue
        label = format_alert_bold(escape_alert_value(heading, message_format), message_format)
        blocks.append("\n".join([label, *rendered]))
    return "\n\n".join(blocks)


def _build_template_context(
    delivery: AlertDelivery,
    *,
    destination: AlertDestination,
    rule: AlertRule,
    scan_name: str,
    project: Project | None,
    message_format_override: str | None = None,
    session: Session | None = None,
    item_context_cache: dict[uuid.UUID, tuple[str, ...]] | None = None,
    metric_units_cache: dict[str, str | None] | None = None,
    items: list[AlertDeliveryItem] | None = None,
    summary_items: list[AlertDeliveryItem] | None = None,
    part_label: str = "",
    digest: bool = False,
    ai_explanation: str | None = None,
    project_timezone: str | None = None,
) -> AlertTemplateContext:
    message_format = message_format_override or rule.message_format or ALERT_MESSAGE_FORMAT_PLAIN
    items_template = normalize_message_template(rule.items_template)
    # The two templates are INDEPENDENT operator signals and are gated
    # separately. A rule that saved a custom message_template and left the item
    # template alone must still get the compact digest items — reading one
    # column to decide both would hand it the verbose ones.
    if items_template is None:
        items_template = (
            get_digest_items_template(message_format)
            if digest
            else get_default_items_template(message_format)
        )
    # ``items`` is one message's share of a delivery split across several (see
    # split_telegram_messages). Its count, not the delivery's, is what the
    # header may claim: a reader looking at message 2 of 2 counts what is in
    # front of them, and the dispatch-side chunking already gives each of its
    # chunks its own matched_count for the same reason.
    rendered_items = delivery.items if items is None else items
    rendered_count = delivery.matched_count if items is None else len(items)
    # ...but the SUMMARY is the other half of that split, and it does not follow
    # the same rule. ``${matched_count}`` counts what is in front of the reader;
    # ``${headline}`` and ``${window_label}`` describe the digest, which is one
    # thing however many messages carry it. Computing them per part would have
    # message 2 of 3 announce "9 alerts" over its own share and name a window
    # that closes hours before the digest's does — three disagreeing summaries
    # of one morning, which is exactly the reading a digest exists to prevent.
    summarised_items = rendered_items if summary_items is None else summary_items
    summarised_count = rendered_count if summary_items is None else len(summary_items)

    variables = {
        "project_name": escape_alert_value(project.name if project else "", message_format),
        "project_slug": escape_alert_value(project.slug if project else "", message_format),
        "channel": escape_alert_value(destination.type, message_format),
        "destination_name": escape_alert_value(destination.name, message_format),
        "rule_name": escape_alert_value(rule.name, message_format),
        "scan_name": escape_alert_value(scan_name, message_format),
        "matched_count": escape_alert_value(rendered_count, message_format),
        "items_count": escape_alert_value(rendered_count, message_format),
        "items_text": _build_items_text(
            rendered_items,
            message_format=message_format,
            items_template=items_template,
            session=session,
            scan_config_id=delivery.scan_config_id,
            item_context_cache=item_context_cache,
            metric_units_cache=metric_units_cache,
            digest=digest,
        ),
        # A deterministic summary line, computed here and never by the model.
        # It is what lands in the phone's notification preview, so it has to be
        # true on every delivery including the one where the LLM is off, times
        # out, or writes 230 characters of preamble before saying anything.
        "headline": escape_alert_value(
            _digest_headline(summarised_items, summarised_count), message_format
        ),
        # An immediate alert arrives AT the anomaly, so "when" is implicit. A
        # digest is decoupled from its data by up to a whole day, and nothing
        # else in the compact line carries a timestamp.
        #
        # The part marker rides here rather than on the headline because the
        # headline is what a phone shows in its notification preview, where
        # "24 alerts, 7 down" is the useful sentence and "(2/3)" is noise. On
        # the line below it, it is the answer to the question a whole-digest
        # headline provokes: the reader counts nine items under "24 alerts" and
        # needs to be told the other fifteen are in the neighbouring messages.
        "window_label": escape_alert_value(
            _join_window_label(
                _digest_window_label(summarised_items, project_timezone) if digest else "",
                part_label,
            ),
            message_format,
        ),
        # Pre-escaped and pre-wrapped: it carries its own trailing blank line so
        # the template needs no conditional, and it is empty when there is no
        # note rather than leaving a hole.
        "ai_explanation_block": (
            f"{format_alert_bold('AI', message_format)} "
            f"{escape_alert_value(ai_explanation, message_format)}\n\n"
            if ai_explanation
            else ""
        ),
    }
    return AlertTemplateContext(variables=variables, message_format=message_format)


def _render_delivery_message(
    delivery: AlertDelivery,
    *,
    destination: AlertDestination,
    rule: AlertRule,
    scan_name: str,
    project: Project | None,
    message_format_override: str | None = None,
    session: Session | None = None,
    item_context_cache: dict[uuid.UUID, tuple[str, ...]] | None = None,
    metric_units_cache: dict[str, str | None] | None = None,
    items: list[AlertDeliveryItem] | None = None,
    summary_items: list[AlertDeliveryItem] | None = None,
    part_label: str = "",
    digest: bool = False,
    ai_explanation: str | None = None,
    project_timezone: str | None = None,
) -> tuple[str, str]:
    """Render (message, message_format) for a delivery.

    ``items`` renders a subset — one message's share of a delivery split across
    several. None means the whole delivery, which is what every channel without
    a per-message ceiling gets.

    ``summary_items`` is what the digest header DESCRIBES, which on a split is
    not what the body lists: every part summarises the whole digest and carries
    ``part_label`` to say which slice of it the reader is holding.

    ``digest`` selects the compact grouped layout. It is a parameter rather
    than something read off the destination, because the caller is the only
    thing that knows: the drain arm ships a genuine digest from a destination
    whose cadence has already been cleared.
    """
    template = normalize_message_template(rule.message_template)
    context = _build_template_context(
        delivery,
        destination=destination,
        rule=rule,
        scan_name=scan_name,
        project=project,
        message_format_override=message_format_override,
        session=session,
        item_context_cache=item_context_cache,
        metric_units_cache=metric_units_cache,
        items=items,
        summary_items=summary_items,
        part_label=part_label,
        digest=digest,
        ai_explanation=ai_explanation,
        project_timezone=project_timezone,
    )
    if template is None:
        template = (
            get_digest_message_template(context.message_format)
            if digest
            else get_default_message_template(context.message_format)
        )
    return render_alert_template(template, context).rstrip(), context.message_format


def split_telegram_messages(
    delivery: AlertDelivery,
    *,
    destination: AlertDestination,
    rule: AlertRule,
    scan_name: str,
    project: Project | None,
    message: str,
    message_format: str,
    items: list[AlertDeliveryItem] | None = None,
    summary_items: list[AlertDeliveryItem] | None = None,
    session: Session | None = None,
    item_context_cache: dict[uuid.UUID, tuple[str, ...]] | None = None,
    metric_units_cache: dict[str, str | None] | None = None,
    ai_explanation: str | None = None,
    max_chars: int = TELEGRAM_MESSAGE_MAX_CHARS,
    digest: bool = False,
    part_offset: int = 0,
    project_timezone: str | None = None,
) -> list[tuple[str, list[AlertDeliveryItem]]]:
    """``message`` as one or more messages that each clear Telegram's ceiling.

    Returns (text, the items that text carries) in send order, so the caller
    knows exactly what has gone out if a later message fails.

    ``message`` is the already-rendered whole — the caller needs it anyway for
    the payload snapshot — so the common case, which is every first attempt,
    costs one length check and no re-render at all. Only when it does not fit
    are the items packed greedily into messages, each one measured as the reader
    will receive it: header, items and AI note assembled and counted in
    Telegram's UTF-16 units. That is what the reserve-based budget it replaces
    could only estimate — the header comes from a user-editable template and the
    AI note from a language model, so both could exceed their reserve and no
    items budget could see it.

    The AI note rides on the first message only. It summarises the whole
    delivery, and repeating it under every part would cost the reader nothing
    but length.

    A digest's HEADER, by contrast, is repeated on every part and describes the
    whole digest on each — the reader is holding one digest, not three — with a
    "2/3" marker on the window line saying which slice the body under it is.
    ``items`` and ``summary_items`` are how those two halves differ: ``items``
    is the set to PACK, ``summary_items`` the whole the header DESCRIBES. They
    are the same set on a first attempt, so ``summary_items`` may be left None;
    a Telegram RESUME packs only the undelivered remainder and has to hand in
    the whole delivery, or every part of the retry summarises the remainder and
    the reader gets "9 alerts" under two earlier messages that said 24
    (tripl-0zpq.35). Deriving it from ``items`` here, as this did, silently
    undid the answer the caller had already worked out for the unsplit render.

    ``part_offset`` is how many messages of this delivery the reader ALREADY
    has from earlier attempts, and it continues the marker across them. Without
    it a resumed 24-item digest numbers its remainder "1/2", "2/2" under a "1/3"
    and "2/3" still on the reader's screen, which reads as a second digest
    rather than the rest of one. The denominator is a running total and may
    exceed what an earlier message named: "3/4" after "1/3" says the digest took
    one message more than first planned, which is true, and is the honest
    version of a count that restarts. It also means a resumed digest cannot take
    the no-re-render shortcut above — ``message`` arrived without a marker.

    Nothing is ever dropped: an item too long to share a message gets one of its
    own. A SINGLE item that alone exceeds the ceiling is the one thing this
    cannot fix — it is returned as its own message and Telegram refuses it, so
    the delivery fails visibly instead of quietly losing the item.
    """
    delivery_items = delivery.items if items is None else items
    # What every part's HEADER describes, which on a resume is not what it
    # lists. None means the two are the same set, which is every first attempt.
    summarised_items = delivery_items if summary_items is None else summary_items

    def render_part(
        part: list[AlertDeliveryItem], *, with_ai_note: bool, part_label: str = ""
    ) -> str:
        text, part_format = _render_delivery_message(
            delivery,
            destination=destination,
            rule=rule,
            scan_name=scan_name,
            project=project,
            message_format_override=message_format,
            session=session,
            item_context_cache=item_context_cache,
            metric_units_cache=metric_units_cache,
            items=part,
            # The header summarises the whole digest on EVERY part — see
            # _build_template_context. Only the digest layout has a header that
            # makes a claim about scope, so nothing else opts in.
            summary_items=summarised_items if digest else None,
            part_label=part_label,
            digest=digest,
            # A digest carries its note INSIDE the layout, above the list, so it
            # is rendered into the part rather than appended after it — and only
            # into the first part, for the same reason the appended one is.
            ai_explanation=ai_explanation if (digest and with_ai_note) else None,
            project_timezone=project_timezone,
        )
        if with_ai_note and ai_explanation and not digest:
            text = _append_ai_explanation(text, ai_explanation, part_format)
        return text

    if len(delivery_items) <= 1 or telegram_visible_length(message, message_format) <= max_chars:
        if not (digest and part_offset):
            return [(message, list(delivery_items))]
        # One message is still SOME message: a resumed digest has to say which,
        # and this one is the last, which is the fact the reader most wants
        # after a failure. ``message`` was rendered without a marker, so it is
        # re-rendered with one and re-MEASURED — stamping it blind is the
        # hazard the packing reserve below exists to prevent, and this path
        # never packed and so has no reserve. If the marker is what pushes it
        # over, fall through and let the packer split it under a budget that
        # does reserve room.
        final_label = f"{part_offset + 1}/{part_offset + 1}"
        only_part = render_part(delivery_items, with_ai_note=True, part_label=final_label)
        if telegram_visible_length(only_part, message_format) <= max_chars:
            return [(only_part, list(delivery_items))]

    # The marker names a total that does not exist until packing has finished,
    # so it cannot be rendered while packing. Instead the packer works to a
    # budget short by the widest marker this delivery could ever carry — it
    # cannot split into more parts than it has items, and ``part_offset`` adds
    # to BOTH sides of the slash — and the finished parts are re-rendered with
    # the real one. Reserving is what stops that second render pushing a part
    # back over the ceiling it was just measured against.
    widest_part = part_offset + len(delivery_items)
    widest_part_label = f"{widest_part}/{widest_part}"
    budget = max_chars - (len(_WINDOW_LABEL_SEPARATOR) + len(widest_part_label) if digest else 0)

    parts: list[tuple[str, list[AlertDeliveryItem]]] = []
    current: list[AlertDeliveryItem] = []
    current_text = ""
    for item in delivery_items:
        candidate = [*current, item]
        candidate_text = render_part(candidate, with_ai_note=not parts)
        # ``current`` being non-empty is what keeps a lone oversized item in:
        # closing an empty message to make room for it would drop it forever.
        if current and telegram_visible_length(candidate_text, message_format) > budget:
            parts.append((current_text, current))
            current = [item]
            current_text = render_part(current, with_ai_note=False)
            continue
        current = candidate
        current_text = candidate_text
    parts.append((current_text, current))
    # A lone part carries no marker — "1/1" is noise on the overwhelming
    # majority of digests — UNLESS the reader already holds earlier ones, where
    # the marker is the only thing telling them this is the last.
    if not digest or (len(parts) == 1 and not part_offset):
        return parts
    return [
        (
            render_part(
                part_items,
                with_ai_note=index == 0,
                part_label=f"{part_offset + index + 1}/{part_offset + len(parts)}",
            ),
            part_items,
        )
        for index, (_text, part_items) in enumerate(parts)
    ]


def _describe_age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _recent_alert_history(
    session: Session,
    delivery: AlertDelivery,
    *,
    now: datetime,
) -> list[str]:
    """What this rule already told the reader about these same scopes.

    Matching is by ``scope_ref`` rather than the whole rule: a rule covering a
    hundred events would otherwise drag in unrelated history and the model would
    "recall" something the reader never saw about this event. Only ``sent``
    deliveries count — a failed one was never read, so claiming to have reported
    it would be a lie.

    Returns newest-first prose lines; an empty list simply leaves the prompt as
    it was, which is the correct behaviour for a genuinely first-time alert.
    """
    scope_refs = {item.scope_ref for item in delivery.items if item.scope_ref}
    if not scope_refs:
        return []

    matching_items = (
        select(AlertDeliveryItem.delivery_id)
        .where(
            AlertDeliveryItem.delivery_id == AlertDelivery.id,
            AlertDeliveryItem.scope_ref.in_(scope_refs),
        )
        .exists()
    )
    previous = (
        session.execute(
            select(AlertDelivery)
            .where(
                AlertDelivery.rule_id == delivery.rule_id,
                AlertDelivery.id != delivery.id,
                AlertDelivery.status == AlertDeliveryStatus.sent.value,
                AlertDelivery.sent_at.is_not(None),
                AlertDelivery.sent_at >= now - _AI_HISTORY_WINDOW,
                matching_items,
            )
            .order_by(AlertDelivery.sent_at.desc())
            .limit(_AI_HISTORY_MAX_DELIVERIES)
        )
        .scalars()
        .all()
    )

    lines: list[str] = []
    for past in previous:
        sent_at = past.sent_at
        if sent_at is None:
            continue
        # SQLite hands back naive datetimes where PostgreSQL is tz-aware; the
        # subtraction below would raise on the mix.
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        snapshot = past.payload_snapshot if isinstance(past.payload_snapshot, dict) else {}
        said = str(snapshot.get("ai_explanation") or "").strip()
        scopes = ", ".join(sorted({item.scope_name for item in past.items if item.scope_name})[:4])
        line = f"- {_describe_age(now - sent_at)} ({sent_at:%Y-%m-%d %H:%M} UTC)"
        if scopes:
            line += f", about {scopes}"
        if said:
            head = said[:_AI_HISTORY_EXPLANATION_CHARS]
            if len(said) > _AI_HISTORY_EXPLANATION_CHARS:
                head += "…"
            line += f'. You already told them: "{head}"'
        else:
            line += ". (no AI note was attached that time)"
        lines.append(line)
    return lines


def _build_ai_explanation(
    delivery: AlertDelivery,
    *,
    scan_name: str,
    project_name: str,
    item_context_cache: dict[uuid.UUID, tuple[str, ...]],
    session: Session | None = None,
    now: datetime | None = None,
    max_items: int = _AI_EXPLANATION_MAX_ITEMS,
) -> str | None:
    """LLM summary of the delivery's items, or None when AI is off or fails.

    Reuses the (sparkline, top_movers) pairs already built for template
    rendering — no extra DB queries. Failure here must never block the alert,
    so any error degrades to None.
    """
    ai_config = app_settings_service.get_ai_config_sync()
    lines: list[str] = [f"Project: {project_name}", f"Scan: {scan_name}", "Alert items:"]
    # The correlation id cannot answer "did this co-fire?". It is the inbox
    # handle, every item carries one, and it is keyed per SCOPE — so counting
    # group members finds exactly one member for every item and the tag would
    # silently never appear again. Peers are counted inside this delivery
    # instead, on the pair that makes co-firing mean something: one bucket, one
    # direction.
    cofiring_sizes: Counter[tuple[datetime, str]] = Counter(
        (item.bucket, item.direction) for item in delivery.items
    )
    for item in delivery.items[:max_items]:
        sparkline, top_movers, attribution = _unpack_item_context(
            item_context_cache.get(item.id, ())
        )
        if item.scope_type == SCOPE_RELEASE_REGRESSION:
            # Same basis clause as the rendered message. Without it the model
            # writes the note from "observed 345 vs expected 715.7" alone and
            # re-teaches the raw-count reading the message line just removed.
            lines.append(
                f"- [release regression] {item.scope_name}: "
                f"{release_regression_basis(_drift_facts(item))}; observed "
                f"{plain_alert_number(item.actual_count)} vs adoption-adjusted "
                f"expected {plain_alert_number(item.expected_count)}"
            )
            continue
        if item.scope_type == SCOPE_SOURCE_FRESHNESS:
            # A delay, not a volume move: quoting "actual 7 vs expected 3" as
            # counts would have the model explain a traffic drop that is really
            # a late warehouse load.
            lines.append(f"- [source freshness] {source_freshness_line(_drift_facts(item))}")
            continue
        if item.scope_type in {"schema", "distribution"}:
            drift_bits = " ".join(
                part
                for part in (
                    item.drift_type or "",
                    f"field={item.drift_field}" if item.drift_field else "",
                    f"sample={item.sample_value}" if item.sample_value else "",
                )
                if part
            )
            lines.append(f"- [{item.scope_type} drift] {item.scope_name}: {drift_bits}")
            continue
        # "no baseline" rather than "+0%" for a zero-expected item: the note the
        # model writes from this prompt is what the reader receives, and "+0%"
        # reads as "nothing moved" for the one class where everything did
        # (tripl-l429.24).
        line = (
            f"- [{item.scope_type}] {item.scope_name}: {item.direction}, "
            f"actual {item.actual_count} vs expected {item.expected_count} "
            f"({format_percent_delta(item.percent_delta, item.expected_count, spec='+.0f')}), "
            f"bucket {item.bucket:%Y-%m-%d %H:%M}"
        )
        if sparkline:
            line += f", recent trend (old→new): {sparkline}"
        if attribution:
            # The stored detection-time decomposition, the same sentence the
            # message line carries — so the note cannot credit a different
            # dimension than the alert body does.
            line += f", attribution: {attribution}"
        if top_movers:
            line += f", top movers: {top_movers}"
        if cofiring_sizes.get((item.bucket, item.direction), 0) > 1:
            line += " [co-fired with other items]"
        lines.append(line)
    # What was already said about these scopes, so a recurring drift reads as
    # "still going, now worse" instead of the same paragraph again (tripl-ikee).
    # Best-effort: a history lookup must never cost the reader their alert.
    if session is not None:
        try:
            history = _recent_alert_history(session, delivery, now=now or datetime.now(UTC))
        except Exception:  # noqa: BLE001
            logger.warning("Alert history lookup for the AI explanation failed", exc_info=True)
            history = []
        if history:
            lines.append("")
            lines.append(
                "Previously sent by this rule for these same scopes (most recent first). "
                "Say what has CHANGED since; do not repeat what the reader already read:"
            )
            lines.extend(history)
    try:
        raw = llm_service.complete(
            ai_config.alert_explanation_system_prompt,
            "\n".join(lines),
            max_tokens=_AI_EXPLANATION_MAX_TOKENS,
            temperature=0.3,
            config=ai_config,
        )
    except Exception:  # noqa: BLE001
        logger.warning("AI explanation generation failed", exc_info=True)
        return None
    if raw is None:
        return None
    explanation = raw.strip()
    return explanation or None


def _append_ai_explanation(text: str, explanation: str, message_format: str) -> str:
    return f"{text}\n\nAI: {escape_alert_value(explanation, message_format)}"


def _build_email_subject(
    *,
    template: str | None,
    rule: AlertRule,
    project: Project | None,
    matched_count: int,
    destination: AlertDestination,
    message_format: str,
    rule_name_override: str | None = None,
) -> str:
    """Render the subject template. Falls back to a sensible default.

    ``rule_name_override`` exists for the scheduled digest, where one email
    carries several rules and no single ``rule.name`` is the truth. It is a
    parameter rather than an assignment to ``rule.name`` on purpose: the
    AlertRule the sender loaded is a live ORM object in the session's identity
    map, so writing to it would be flushed by the next commit and would
    permanently rename the operator's rule — from a subject line.
    """
    name = rule_name_override or rule.name
    if template is None:
        prefix = project.name if project else "tripl"
        return f"[{prefix}] {name} — {matched_count} alert(s)"
    variables = {
        "project_name": escape_alert_value(project.name if project else "", message_format),
        "project_slug": escape_alert_value(project.slug if project else "", message_format),
        "rule_name": escape_alert_value(name, message_format),
        "destination_name": escape_alert_value(destination.name, message_format),
        "matched_count": escape_alert_value(matched_count, message_format),
    }
    rendered = render_alert_template(
        template,
        AlertTemplateContext(variables=variables, message_format=ALERT_MESSAGE_FORMAT_PLAIN),
    ).strip()
    # Subject MUST be single-line — strip any newline injection that snuck in.
    return rendered.replace("\r", " ").replace("\n", " ") or name


def _build_jira_adf_body(text: str) -> dict[str, object]:
    """Render plain text as Atlassian Document Format (ADF).

    One paragraph per non-empty line is enough for the alert message — we don't
    need the full rich tree, just a structure Jira will accept and display as a
    multi-line ticket body.
    """
    paragraphs: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line:
            paragraphs.append({"type": "paragraph", "content": []})
            continue
        paragraphs.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": line}],
            }
        )
    if not paragraphs:
        paragraphs.append({"type": "paragraph", "content": []})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _build_ticket_subject(
    *,
    rule: AlertRule,
    project: Project | None,
    matched_count: int,
) -> str:
    """Single-line summary for Jira / Linear titles. Matches the email subject
    default so all three ticket-style channels stay consistent."""
    prefix = project.name if project else "tripl"
    return f"[{prefix}] {rule.name} — {matched_count} alert(s)"


def _is_telegram_markdown_parse_error(error: Exception) -> bool:
    message = str(error).lower()
    return "can't parse entities" in message or "can't find end of" in message


def _is_telegram_message_too_long_error(error: Exception) -> bool:
    """True for Telegram's over-4096 rejection.

    ``_post_json`` turns the HTTPError into ValueError("HTTP 400 from <url>:
    Bad Request: message is too long"), which
    :func:`_is_telegram_markdown_parse_error` correctly does not match — it is
    not a parse failure, and re-rendering as plain text makes a long message
    longer, not shorter.

    Since :func:`split_telegram_messages` measures each message as Telegram
    counts it, this can now only mean one alert item is longer than 4096 units
    on its own — nothing a smaller budget could fix. It is matched so the
    sender can say that in the delivery's error, instead of leaving the raw
    HTTP 400 in the Inbox.
    """
    return "message is too long" in str(error).lower()


def _webhook_item_payload(item: AlertDeliveryItem) -> dict[str, object]:
    """One matched item as outbound JSON.

    ``percent_delta`` is ``null`` — not ``0.0`` — when there was no baseline to
    divide by. The stored column is NOT NULL and holds the ``0.0`` placeholder,
    but shipping that placeholder made the payload say "no change" about the one
    class of anomaly that moved the most, and a consumer writing the obvious
    ``percent_delta > threshold`` had no way to tell the two apart
    (tripl-l429.27). ``expected_count: 0`` still travels beside it as
    corroboration; it is no longer the only thing standing between a consumer
    and a wrong number.
    """
    return {
        "scope_type": item.scope_type,
        "scope_ref": item.scope_ref,
        "scope_name": item.scope_name,
        "direction": item.direction,
        "actual_count": item.actual_count,
        "expected_count": item.expected_count,
        "absolute_delta": item.absolute_delta,
        "percent_delta": percent_delta_or_none(item.percent_delta, item.expected_count),
        "bucket": item.bucket.isoformat() if item.bucket else None,
        "details_url": item.details_path,
        "monitoring_url": item.monitoring_path,
        "drift_field": item.drift_field,
        "drift_type": item.drift_type,
        "sample_value": item.sample_value,
    }


def _build_webhook_payload(
    delivery: AlertDelivery,
    *,
    destination: AlertDestination,
    rule: AlertRule,
    scan_name: str,
    project: Project | None,
    message: str,
) -> dict[str, object]:
    """Structured JSON body so downstream automation (Zapier/n8n/etc.) can parse
    individual fields without scraping the rendered ``message`` text."""
    return {
        "project": {
            "name": project.name if project else None,
            "slug": project.slug if project else None,
        },
        "destination": {"id": str(destination.id), "name": destination.name},
        "rule": {"id": str(rule.id), "name": rule.name},
        "scan": {"id": str(delivery.scan_config_id), "name": scan_name},
        "matched_count": delivery.matched_count,
        "message": message,
        "items": [_webhook_item_payload(item) for item in delivery.items],
    }


def _build_plan_digest_message(
    session: Session,
    *,
    project: Project,
    now: datetime,
) -> str:
    window_from = now - timedelta(days=DIGEST_WINDOW_DAYS)
    dead_cutoff = now - timedelta(days=DEAD_EVENT_DAYS)

    # Event counts are scoped to the MAIN plan branch, mirroring the API read
    # paths (resolve_branch_id): an open working branch deep-copies every event
    # row, so an unscoped count reports each event once per branch.
    main_branch_id = session.scalar(
        select(PlanBranch.id).where(
            PlanBranch.project_id == project.id,
            PlanBranch.kind == BranchKind.main.value,
        )
    )

    schema_drifts = session.execute(
        select(func.count(SchemaDrift.id))
        .join(EventType, EventType.id == SchemaDrift.event_type_id)
        .where(
            EventType.project_id == project.id,
            SchemaDrift.detected_at >= window_from,
            SchemaDrift.status.in_(("open", "snoozed")),
            (SchemaDrift.status != "snoozed")
            | (SchemaDrift.snoozed_until.is_(None))
            | (SchemaDrift.snoozed_until <= now),
        )
    ).scalar_one()
    # Catalog metric anomalies are project-global: ``metric``-scope rows carry a
    # NULL ``scan_config_id`` by design (models/metric_anomaly.py) and are keyed
    # purely by ``scope_ref`` = str(metric_definition_id), so an inner join on
    # ScanConfig drops every one of them. That is how a week of nothing but
    # catalog-metric anomalies used to read "Metric anomalies: 0" with no Top
    # anomalies section at all (tripl-0zpq.34). Resolve the project's metrics once
    # here, outside both statements below, and reuse the result for the count, the
    # top-5 filter and the top-5 labels. ``display_name`` rather than ``name``
    # because that is what alerting already calls a metric everywhere else
    # (worker/tasks/metrics/alert_payload._build_alert_scope_names).
    metric_display_names = {
        str(metric_id): display_name
        for metric_id, display_name in session.execute(
            select(MetricDefinition.id, MetricDefinition.display_name).where(
                MetricDefinition.project_id == project.id
            )
        ).all()
    }
    # Project scoping for anomalies, in the shape detection_reset_service already
    # uses (and the rule simulator reaches with a second query): the scan config
    # when there is one, the owning MetricDefinition when there is not. A
    # metric-scope row's ScanConfig columns are all NULL under the outer join, so
    # the first arm can never match one and the second arm can never admit a
    # scan-backed row. A project with no catalog metrics keeps the original single
    # predicate rather than an empty IN, and the left join then admits exactly the
    # rows the inner join did, because a NULL scan_config_id satisfies neither.
    anomaly_project_scope = (
        or_(
            ScanConfig.project_id == project.id,
            and_(
                MetricAnomaly.scan_config_id.is_(None),
                MetricAnomaly.scope_type == SCOPE_METRIC,
                MetricAnomaly.scope_ref.in_(list(metric_display_names)),
            ),
        )
        if metric_display_names
        else ScanConfig.project_id == project.id
    )
    metric_anomalies = session.execute(
        select(func.count(MetricAnomaly.id))
        .outerjoin(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
        .where(anomaly_project_scope, MetricAnomaly.bucket >= window_from)
    ).scalar_one()
    distribution_drifts = session.execute(
        select(func.count(DistributionDrift.id))
        .join(ScanConfig, ScanConfig.id == DistributionDrift.scan_config_id)
        .where(
            ScanConfig.project_id == project.id,
            DistributionDrift.bucket >= window_from,
            DistributionDrift.band == DistributionDriftBand.significant.value,
        )
    ).scalar_one()
    total_events = session.execute(
        select(func.count(Event.id)).where(
            Event.project_id == project.id,
            Event.branch_id == main_branch_id,
            Event.status != "archived",
        )
    ).scalar_one()
    live_events = session.execute(
        select(func.count(Event.id)).where(
            Event.project_id == project.id,
            Event.branch_id == main_branch_id,
            Event.status != "archived",
            Event.last_seen_at.is_not(None),
        )
    ).scalar_one()
    dead_events = session.execute(
        select(func.count(Event.id)).where(
            Event.project_id == project.id,
            Event.branch_id == main_branch_id,
            Event.status != "archived",
            Event.status.in_(["implemented", "live"]),
            (Event.last_seen_at.is_(None)) | (Event.last_seen_at < dead_cutoff),
        )
    ).scalar_one()
    sunset_overdue = session.execute(
        select(func.count(Event.id)).where(
            Event.project_id == project.id,
            Event.branch_id == main_branch_id,
            Event.status == EventStatus.deprecated,
            Event.sunset_at.is_not(None),
            Event.sunset_at < now,
            Event.last_seen_at.is_not(None),
            Event.last_seen_at > Event.sunset_at,
        )
    ).scalar_one()

    top_rows = session.execute(
        select(MetricAnomaly, ScanConfig.name)
        .outerjoin(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
        .where(anomaly_project_scope, MetricAnomaly.bucket >= window_from)
        .order_by(MetricAnomaly.bucket.desc(), func.abs(MetricAnomaly.z_score).desc())
        .limit(5)
    ).all()
    top_lines = []
    for anomaly, scan_name in top_rows:
        # The only rows the WHERE above admits without a scan config are the
        # catalog-metric ones, and they have no scan whose name could open the
        # line — the series belongs to the project, not to a scan. Without a
        # substitution the outer join's NULL would print the literal "None"
        # followed by a bare metric uuid. "catalog" reads with the scope type
        # that follows it ("catalog metric:Signup conversion") and keeps the
        # column shape every other line has. The lookup cannot miss, since the
        # same mapping is what let the row through the filter.
        source = "catalog" if scan_name is None else scan_name
        scope_label = (
            metric_display_names.get(anomaly.scope_ref, anomaly.scope_ref)
            if scan_name is None
            else anomaly.scope_ref
        )
        top_lines.append(
            f"- {source} {anomaly.scope_type}:{scope_label} "
            f"{anomaly.direction} actual={anomaly.actual_count} "
            f"expected={anomaly.expected_count:.1f} z={anomaly.z_score:.1f}"
        )

    coverage = (live_events / total_events * 100) if total_events else 0.0
    lines = [
        f"Weekly tripl digest for {project.name}",
        f"Window: last {DIGEST_WINDOW_DAYS} days",
        "",
        f"- Active schema drifts: {schema_drifts}",
        f"- Metric anomalies: {metric_anomalies}",
        f"- Significant distribution drifts: {distribution_drifts}",
        f"- Live coverage: {live_events}/{total_events} events ({coverage:.1f}%)",
        f"- Dead implemented events: {dead_events}",
        f"- Deprecated events still receiving data: {sunset_overdue}",
    ]
    if top_lines:
        lines.extend(["", "Top anomalies:", *top_lines])
    return "\n".join(lines)


def _build_sunset_alert_message(
    session: Session,
    *,
    project: Project,
    now: datetime,
) -> str | None:
    """Return a plaintext alert message when deprecated events are still
    receiving data past their sunset_at, or None when there are none.

    Scoped to the MAIN plan branch, for the same reason and by the same
    resolution as the event counts in :func:`_build_plan_digest_message` above:
    an open working branch deep-copies every event row and carries ``status``,
    ``sunset_at`` and ``last_seen_at`` across unchanged
    (``plan_branch_service``), so an unscoped query returns one row per branch.
    This message is the digest's "Deprecated events still receiving data" line
    expanded into named events — the first ``_SUNSET_ALERT_MAX_EVENTS`` of them,
    see below — and that line is already main-scoped, so without this predicate
    the pair disagreed about one project: the digest said 1 while the alert said
    "Count: 2" and listed the same event twice (tripl-0zpq.31).

    A project with no main branch row resolves no id, the predicate becomes
    ``branch_id IS NULL``, and ``Event.branch_id`` is NOT NULL — so the alert
    stays silent instead of listing every branch's copy. That is the right way
    to fail here: the message exists to be acted on, and one that repeats each
    event once per open branch is worse than none.

    The named list is capped and ``Count:`` is not. The count is its own
    COUNT(*) over the same predicates, so it stays the true total and stays
    equal to the digest's counter however long the list runs; the lines under it
    are a capped page of what that counted, because this message goes out daily
    to real destinations and the argument for its cap is with the constant. When
    the cap bites, the message ends in an "… and N more not shown" tail: a
    silently shortened list reads exactly like a complete one, and the list IS
    the work item.
    """
    main_branch_id = session.scalar(
        select(PlanBranch.id).where(
            PlanBranch.project_id == project.id,
            PlanBranch.kind == BranchKind.main.value,
        )
    )
    # Named once and used by both queries below, so the total and the lines
    # under it cannot come to disagree about what "overdue" means: the equality
    # the digest is held to is asserted against the COUNT(*), and the rendered
    # lines have to be a page of exactly what that counted.
    overdue_scope = (
        Event.project_id == project.id,
        Event.branch_id == main_branch_id,
        Event.status == EventStatus.deprecated,
        Event.sunset_at.is_not(None),
        Event.sunset_at < now,
        Event.last_seen_at.is_not(None),
        Event.last_seen_at > Event.sunset_at,
    )
    total = session.execute(select(func.count(Event.id)).where(*overdue_scope)).scalar_one()

    if not total:
        return None

    overdue_events = session.execute(
        select(Event.id, Event.name, Event.sunset_at, Event.last_seen_at)
        .where(*overdue_scope)
        .order_by(Event.name)
        .limit(_SUNSET_ALERT_MAX_EVENTS)
    ).all()

    lines = [
        f"Deprecated events still receiving data after sunset — {project.name}",
        f"Count: {total}",
        "",
    ]
    for _eid, name, sunset_at, last_seen_at in overdue_events:
        lines.append(f"- {name} (sunset {sunset_at:%Y-%m-%d}, last seen {last_seen_at:%Y-%m-%d})")
    not_shown = total - len(overdue_events)
    if not_shown:
        lines.append(
            f"… and {not_shown} more not shown "
            f"(this list is the first {len(overdue_events)} by name)"
        )
    return "\n".join(lines)
