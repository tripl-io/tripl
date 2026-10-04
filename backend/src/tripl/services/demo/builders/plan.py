"""Plan-schema builder: the design canvas of the demo.

Seeds event types, field definitions, meta fields, events (with field values,
tags, and meta VALUES), one event-type relation, and — when a real creator is
known — one event-type owner. Every entity is reachable through the normal plan
API (``/events``, ``/relations``, ``/event-types/{id}/owners``).

The declarative :func:`event_specs` list is the single source of truth for event
volumes; the warehouse builder imports it to shape the metric series.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.services.demo import noise
from tripl.services.demo.scenario import DemoContext

# ---------------------------------------------------------------------------
# Declarative schema data (stable semantic keys, never runtime ids)
# ---------------------------------------------------------------------------

# (name, display_name, color, order)
_EVENT_TYPE_SPECS: tuple[tuple[str, str, str, int], ...] = (
    ("screen_view", "Screen View", "#6366f1", 0),
    ("click", "Click", "#22c55e", 1),
    ("purchase", "Purchase", "#f59e0b", 2),
)

# (event_type, name, display_name, field_type, is_required, enum_options, order)
_FIELD_SPECS: tuple[tuple[str, str, str, str, bool, list[str] | None, int], ...] = (
    ("screen_view", "screen_name", "Screen Name", "string", True, None, 0),
    ("screen_view", "platform", "Platform", "enum", False, ["ios", "android", "web"], 1),
    ("click", "button_id", "Button ID", "string", False, None, 0),
    ("click", "screen_name", "Screen Name", "string", False, None, 1),
    ("click", "platform", "Platform", "enum", False, ["ios", "android", "web"], 2),
    ("purchase", "product_id", "Product ID", "string", False, None, 0),
    ("purchase", "amount", "Amount", "number", False, None, 1),
    ("purchase", "currency", "Currency", "enum", False, ["USD", "EUR"], 2),
    ("purchase", "platform", "Platform", "enum", False, ["ios", "android", "web"], 3),
    ("purchase", "payload", "Payload", "json", False, None, 4),
)

# (name, display_name, field_type, link_template, order)
_META_FIELD_SPECS: tuple[tuple[str, str, str, str | None, int], ...] = (
    ("jira", "Jira ticket", "url", "https://jira.example.com/${value}", 0),
    ("owner_team", "Owner team", "string", None, 1),
)


@dataclass(frozen=True)
class EventSpec:
    """Declarative description of a seeded event (semantic keys, no runtime ids)."""

    event_type: str
    name: str
    description: str
    base: int
    status: str
    tags: tuple[str, ...]
    # (field_key, value) where field_key is "<event_type>.<field_name>".
    field_values: tuple[tuple[str, str], ...]
    last_seen_at: datetime | None
    metric_breakdown_columns: tuple[str, ...]
    sunset_at: datetime | None = None


def event_specs(now: datetime) -> list[EventSpec]:
    """The demo's event roster, parametrised by the scenario clock.

    Status guide: ``live`` (implemented, recent traffic), ``implemented``
    (built, no warehouse data yet), ``in_review`` (new/scanned), ``archived``
    (retired), ``deprecated`` (sunset showcase).
    """
    seven_days_ago = now - timedelta(days=7)
    two_days_ago = now - timedelta(days=2)
    one_day_ago = now - timedelta(days=1)
    return [
        # screen_view events
        EventSpec(
            event_type="screen_view",
            name="Home Screen View",
            description="User lands on the home screen.",
            base=1800,
            status="live",
            tags=("core", "onboarding"),
            field_values=(
                ("screen_view.screen_name", "home"),
                ("screen_view.platform", "${platform}"),
            ),
            last_seen_at=now - timedelta(minutes=15),
            metric_breakdown_columns=("platform",),
        ),
        EventSpec(
            event_type="screen_view",
            name="Paywall View",
            description="User sees the paywall / subscription upsell screen.",
            base=600,
            status="live",
            tags=("core", "checkout"),
            field_values=(
                ("screen_view.screen_name", "paywall"),
                ("screen_view.platform", "${platform}"),
            ),
            last_seen_at=now - timedelta(hours=1),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="screen_view",
            name="Onboarding Step 1 View",
            description="First step of the onboarding flow.",
            base=900,
            status="live",
            tags=("onboarding",),
            field_values=(
                ("screen_view.screen_name", "onboarding_step1"),
                ("screen_view.platform", "${platform}"),
            ),
            last_seen_at=now - timedelta(hours=2),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="screen_view",
            name="Onboarding Step 2 View",
            description="Second step of the onboarding flow.",
            base=700,
            status="in_review",
            tags=("onboarding",),
            field_values=(
                ("screen_view.screen_name", "onboarding_step2"),
                ("screen_view.platform", "${platform}"),
            ),
            last_seen_at=now - timedelta(hours=3),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="screen_view",
            name="Profile Screen View",
            description="User opens their profile.",
            base=400,
            status="live",
            tags=("core",),
            field_values=(
                ("screen_view.screen_name", "profile"),
                ("screen_view.platform", "${platform}"),
            ),
            last_seen_at=now - timedelta(hours=4),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="screen_view",
            name="Settings Screen View",
            description="User opens app settings.",
            base=200,
            status="in_review",
            tags=(),
            field_values=(("screen_view.screen_name", "settings"),),
            last_seen_at=None,
            metric_breakdown_columns=(),
        ),
        # click events
        EventSpec(
            event_type="click",
            name="Buy Button Click",
            description="User taps the primary CTA on the paywall.",
            base=300,
            status="live",
            tags=("core", "checkout"),
            field_values=(
                ("click.button_id", "buy_now"),
                ("click.screen_name", "paywall"),
                ("click.platform", "${platform}"),
            ),
            last_seen_at=now - timedelta(minutes=30),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="click",
            name="Skip Onboarding Click",
            description="User skips onboarding.",
            base=180,
            status="live",
            tags=("onboarding",),
            field_values=(
                ("click.button_id", "skip_onboarding"),
                ("click.screen_name", "onboarding_step1"),
                ("click.platform", "${platform}"),
            ),
            last_seen_at=now - timedelta(hours=5),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="click",
            name="Restore Purchase Click",
            description="User taps restore purchase.",
            base=40,
            status="in_review",
            tags=("checkout",),
            field_values=(
                ("click.button_id", "restore_purchase"),
                ("click.screen_name", "paywall"),
            ),
            last_seen_at=now - timedelta(hours=8),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="click",
            name="Share Button Click",
            description="User shares content.",
            base=120,
            status="in_review",
            tags=(),
            field_values=(("click.button_id", "share"),),
            last_seen_at=None,
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="click",
            name="Legacy CTA Click",
            description="Old primary CTA, replaced by buy_now.",
            base=20,
            status="archived",
            tags=("checkout",),
            field_values=(("click.button_id", "old_cta"),),
            last_seen_at=seven_days_ago,
            metric_breakdown_columns=(),
        ),
        # purchase events
        EventSpec(
            event_type="purchase",
            name="Purchase Completed",
            description="User successfully completes an in-app purchase.",
            base=120,
            status="live",
            tags=("core", "checkout"),
            field_values=(
                ("purchase.product_id", "${product_id}"),
                ("purchase.amount", "9.99"),
                ("purchase.currency", "USD"),
                ("purchase.platform", "${platform}"),
            ),
            last_seen_at=now - timedelta(minutes=45),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="purchase",
            name="Purchase Failed",
            description="An in-app purchase was attempted but failed.",
            base=15,
            status="live",
            tags=("checkout",),
            field_values=(
                ("purchase.product_id", "${product_id}"),
                ("purchase.currency", "USD"),
                ("purchase.platform", "${platform}"),
            ),
            last_seen_at=now - timedelta(hours=2),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="purchase",
            name="Trial Started",
            description="User starts a free trial.",
            base=250,
            status="live",
            tags=("checkout", "onboarding"),
            field_values=(
                ("purchase.product_id", "${product_id}"),
                ("purchase.platform", "${platform}"),
            ),
            last_seen_at=now - timedelta(hours=1),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="purchase",
            name="Subscription Renewed",
            description="Recurring subscription renewal processed.",
            base=80,
            status="in_review",
            tags=("checkout",),
            field_values=(
                ("purchase.product_id", "${product_id}"),
                ("purchase.amount", "9.99"),
                ("purchase.currency", "USD"),
            ),
            last_seen_at=now - timedelta(hours=6),
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="purchase",
            name="Refund Processed",
            description="A refund was issued to the user.",
            base=8,
            status="in_review",
            tags=(),
            field_values=(
                ("purchase.product_id", "${product_id}"),
                ("purchase.amount", "9.99"),
            ),
            last_seen_at=None,
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="purchase",
            name="Promo Applied",
            description="User redeemed a promotional code.",
            base=35,
            status="deprecated",
            sunset_at=one_day_ago,
            tags=("checkout", "onboarding"),
            field_values=(
                ("purchase.product_id", "${product_id}"),
                ("purchase.platform", "${platform}"),
            ),
            last_seen_at=two_days_ago,
            metric_breakdown_columns=(),
        ),
        EventSpec(
            event_type="purchase",
            name="Subscription Cancelled",
            description="User cancels their subscription.",
            base=22,
            status="implemented",
            tags=("checkout",),
            field_values=(
                ("purchase.product_id", "${product_id}"),
                ("purchase.platform", "${platform}"),
            ),
            last_seen_at=one_day_ago,
            metric_breakdown_columns=(),
        ),
    ]


# Meta VALUES to attach: event name -> {meta field name: value}. Demonstrates
# the plan-metadata editor rendering real values (jira link + owning team).
_META_VALUES: dict[str, dict[str, str]] = {
    "Home Screen View": {"owner_team": "Growth"},
    "Purchase Completed": {"jira": "PAY-1234", "owner_team": "Payments"},
}

# "First seen" stagger shape (PR #51 follow-up to tripl-2su6 .21). Core-tagged
# events anchor the start of the seeded history; the rest ramp in between
# ``_STAGGER_OLDEST_AGE_DAYS`` and ``_STAGGER_YOUNGEST_AGE_DAYS`` before now.
# The youngest floor sits ~2 days back (jitter included) so nothing reads as
# first seen "just now", while enough of the catalog lands inside the Overview
# KPI's 14-day lookback that the "active events" sparkline shows a gentle ramp.
_CORE_TAG = "core"
_CORE_ANCHOR_JITTER_HOURS = 12
_STAGGER_OLDEST_AGE_DAYS = 21.0
_STAGGER_YOUNGEST_AGE_DAYS = 2.5
_STAGGER_JITTER_HOURS = 6


def staggered_created_ats(specs: list[EventSpec], *, now: datetime, seed: int) -> list[datetime]:
    """Deterministic per-event "first seen" timestamps across the history window.

    Returns one ``created_at`` per spec, in spec order. Core-tagged events (the
    always-live backbone like Home Screen View / Purchase Completed) sit within
    hours of the seeded history start, so the oldest events still anchor the
    ~3-week window; the remaining catalog ramps in over the following weeks in
    spec order, with roughly a third landing inside the last 14 days and nothing
    younger than ~2 days. Every offset derives from ``(seed, event name)`` via
    :func:`noise.derive_seed` — same recipe, same clock, same timestamps.
    """
    # Same source of truth the warehouse builder seeds from, so the history span
    # keeps a single definition (~23 days, hour-aligned).
    history_start = noise.hour_buckets(now, days=noise.DEMO_HISTORY_DAYS)[0]
    non_core_count = sum(1 for spec in specs if _CORE_TAG not in spec.tags)
    last_rank = max(non_core_count - 1, 1)
    age_span_days = _STAGGER_OLDEST_AGE_DAYS - _STAGGER_YOUNGEST_AGE_DAYS

    created_ats: list[datetime] = []
    rank = 0
    for spec in specs:
        jitter_seed = noise.derive_seed(seed, f"first_seen:{spec.name}")
        if _CORE_TAG in spec.tags:
            anchor_jitter = timedelta(hours=jitter_seed % _CORE_ANCHOR_JITTER_HOURS)
            created_ats.append(history_start + anchor_jitter)
            continue
        age_days = _STAGGER_OLDEST_AGE_DAYS - (rank / last_rank) * age_span_days
        rank += 1
        # Symmetric +/- jitter off the linear ramp so first-seen dates don't sit
        # on a perfect grid; the youngest event stays >= ~2 days old.
        jitter_hours = jitter_seed % (2 * _STAGGER_JITTER_HOURS + 1) - _STAGGER_JITTER_HOURS
        created_ats.append(now - timedelta(days=age_days, hours=jitter_hours))
    return created_ats


async def build_plan(session: AsyncSession, ctx: DemoContext) -> None:
    await _build_event_types(session, ctx)
    await _build_fields(session, ctx)
    await _build_meta_fields(session, ctx)
    await _build_events(session, ctx)
    await _build_meta_values(session, ctx)
    await _build_relation(session, ctx)
    await _build_owner(session, ctx)


async def _build_event_types(session: AsyncSession, ctx: DemoContext) -> None:
    for name, display_name, color, order in _EVENT_TYPE_SPECS:
        et = EventType(
            project_id=ctx.project_id,
            branch_id=ctx.branch_id,
            name=name,
            display_name=display_name,
            color=color,
            order=order,
        )
        session.add(et)
        await session.flush()
        ctx.event_type_ids[name] = et.id


async def _build_fields(session: AsyncSession, ctx: DemoContext) -> None:
    for et_name, name, display_name, field_type, is_required, enum_options, order in _FIELD_SPECS:
        fd = FieldDefinition(
            event_type_id=ctx.event_type_ids[et_name],
            name=name,
            display_name=display_name,
            field_type=field_type,
            is_required=is_required,
            enum_options=enum_options,
            order=order,
        )
        session.add(fd)
        await session.flush()
        ctx.field_ids[f"{et_name}.{name}"] = fd.id


async def _build_meta_fields(session: AsyncSession, ctx: DemoContext) -> None:
    for name, display_name, field_type, link_template, order in _META_FIELD_SPECS:
        mf = MetaFieldDefinition(
            project_id=ctx.project_id,
            branch_id=ctx.branch_id,
            name=name,
            display_name=display_name,
            field_type=field_type,
            link_template=link_template,
            order=order,
        )
        session.add(mf)
        await session.flush()
        ctx.meta_field_ids[name] = mf.id


async def _build_events(session: AsyncSession, ctx: DemoContext) -> None:
    specs = event_specs(ctx.now)
    # "First seen" (the event's created_at) is STAGGERED across the seeded
    # ~23-day metric history, not stamped uniformly — a single shared timestamp
    # makes every event first seen the same instant (and leaves the Overview
    # 14-day "active events" sparkline a flat zero), while the provisioning
    # instant makes everything first seen "just now". See staggered_created_ats.
    created_ats = staggered_created_ats(specs, now=ctx.now, seed=ctx.seed)
    events: list[tuple[EventSpec, Event]] = []
    for order, spec in enumerate(specs):
        ev = Event(
            project_id=ctx.project_id,
            branch_id=ctx.branch_id,
            event_type_id=ctx.event_type_ids[spec.event_type],
            name=spec.name,
            description=spec.description,
            order=order,
            status=spec.status,
            sunset_at=spec.sunset_at,
            last_seen_at=spec.last_seen_at,
            created_at=created_ats[order],
            metric_breakdown_columns=list(spec.metric_breakdown_columns),
        )
        session.add(ev)
        events.append((spec, ev))
    await session.flush()

    for spec, ev in events:
        ctx.event_ids[spec.name] = ev.id
        for field_key, value in spec.field_values:
            session.add(
                EventFieldValue(
                    event_id=ev.id,
                    field_definition_id=ctx.field_ids[field_key],
                    value=value,
                    # These values are HAND-AUTHORED by the recipe, exactly as if a
                    # human had typed them in the event editor (which stamps
                    # ``is_authored=True`` — see event_service), so a scan must not
                    # rewrite them. Left unauthored, the demo's own guided first
                    # scan replaced the documented ``${product_id}`` / ``${platform}``
                    # templates with whatever literal the synthetic warehouse
                    # happened to emit, which also cut the seeded variable value
                    # contexts loose from the field they describe.
                    is_authored=True,
                )
            )
        for tag_name in spec.tags:
            session.add(EventTag(event_id=ev.id, name=tag_name))
    await session.flush()


async def _build_meta_values(session: AsyncSession, ctx: DemoContext) -> None:
    for event_name, values in _META_VALUES.items():
        event_id = ctx.event_ids[event_name]
        for meta_field_name, value in values.items():
            session.add(
                EventMetaValue(
                    event_id=event_id,
                    meta_field_definition_id=ctx.meta_field_ids[meta_field_name],
                    value=value,
                )
            )
    await session.flush()


async def _build_relation(session: AsyncSession, ctx: DemoContext) -> None:
    # A click belongs to the screen it happened on, joined on ``screen_name``.
    session.add(
        EventTypeRelation(
            project_id=ctx.project_id,
            branch_id=ctx.branch_id,
            source_event_type_id=ctx.event_type_ids["click"],
            target_event_type_id=ctx.event_type_ids["screen_view"],
            source_field_id=ctx.field_ids["click.screen_name"],
            target_field_id=ctx.field_ids["screen_view.screen_name"],
            relation_type="belongs_to",
            description="A click belongs to the screen view it happened on.",
        )
    )
    await session.flush()


async def _build_owner(session: AsyncSession, ctx: DemoContext) -> None:
    # Owners reference a real user. The demo creator is the only real user in the
    # seed; if none is known (e.g. a direct builder test), skip — never invent one.
    if ctx.created_by is None:
        return
    session.add(
        EventTypeOwner(
            event_type_id=ctx.event_type_ids["screen_view"],
            user_id=ctx.created_by,
            granted_by=ctx.created_by,
        )
    )
    await session.flush()
