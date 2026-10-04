"""Activity builder: edit history and external design specs.

Seeds the ``created`` first row of every authored event plus a small, realistic
``EventChange`` edit history (both reachable via
``/events/{id}/history``) and one ``EventPhoto`` of kind ``figma`` that embeds an
external design URL with no stored bytes — the no-secret embed path, so the demo
needs no storage backend. Both are reachable through the events API.

The history is also what the audit builder derives the demo's ``event.update``
rows from, so the two surfaces cannot disagree about an edit they both describe.
That is why the retirements below live HERE rather than being invented there: a
real PATCH writes both surfaces, and an audit row for an edit with no history
behind it would be a shape production never produces.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.bucketing import to_utc
from tripl.models.domain_enums import EventPhotoKind
from tripl.models.event import Event
from tripl.models.event_change import EventChange
from tripl.models.event_photo import EventPhoto
from tripl.services.demo.scenario import DemoContext

_FIGMA_URL = "https://www.figma.com/file/DEMO0paywall/Paywall-Spec?node-id=0-1"

# (event name, days ago, ((field, old value, new value), ...)).
#
# Edits sharing one entry are ONE request: the events API writes an EventChange
# per field but a single audit row per PATCH, so anything grouped here stays
# grouped there.
#
# Only fields ``event_service._TRACKED_FIELDS`` records may appear here — six of
# them since 9fbc5811: status, name, title, description, sunset_at and
# superseded_by_event_id. (This comment said "the four fields" and quoted docs
# wording, "and nothing else", that no longer exists; the count was stale, not
# the rule — tripl-0zpq.244.) The history used to carry a
# ``metric_breakdown_columns`` edit, which is a row the product cannot produce,
# so the demo was teaching a capability that does not exist. Harmless while it
# sat on one page; not harmless once the audit builder began deriving
# ``event.update`` payloads from these rows and carrying the invented field onto
# a second surface.
_EDITS: tuple[tuple[str, int, tuple[tuple[str, str | None, str | None], ...]], ...] = (
    # Home Screen View: reviewed -> shipped -> renamed to the convention.
    ("Home Screen View", 5, (("status", "in_review", "live"),)),
    (
        "Home Screen View",
        3,
        (("description", "User opens the home screen.", "User lands on the home screen."),),
    ),
    ("Home Screen View", 1, (("name", "Home View", "Home Screen View"),)),
    # The two retirements the catalog asserts and nothing explained: without
    # these, an archived and a deprecated event sit there with no record of
    # anybody having decided either.
    ("Legacy CTA Click", 2, (("status", "live", "archived"),)),
    # Two fields in one request, which is how deprecation actually works: the
    # sunset date is what makes it actionable rather than a label. ``sunset_at``
    # is filled from the event itself below, so the history cannot disagree with
    # the date the catalog shows.
    ("Promo Applied", 4, (("status", "live", "deprecated"), ("sunset_at", None, None))),
)

# Read off the event rather than authored here, so the two cannot drift.
_DERIVED_FROM_EVENT = "sunset_at"


def _edit_instant(now: datetime, days_ago: int, created_at: datetime) -> datetime:
    """An edit instant that cannot precede the event it edits.

    ``plan.staggered_created_ats`` spreads creation across ~3 weeks and keeps
    every event at least ~2 days old, so these offsets sit safely inside that
    window today — but a recipe change that made one event younger would
    otherwise seed an edit dated before the event existed.

    ``to_utc`` because ``created_at`` comes back from the database: SQLite drops
    the offset on a timezone-aware column and hands back a naive datetime, which
    cannot be compared with the aware ``ctx.now`` at all.
    """
    return max(now - timedelta(days=days_ago), to_utc(created_at) + timedelta(hours=1))


async def build_activity(session: AsyncSession, ctx: DemoContext) -> None:
    await _build_creation_history(session, ctx)
    await _build_event_history(session, ctx)
    await _build_figma_spec(session, ctx)


async def _build_creation_history(session: AsyncSession, ctx: DemoContext) -> None:
    """The first history row of EVERY authored event, the way a real create writes it.

    ``event_service.create_event`` and ``bulk_create_events`` both file
    ``EventChange(field="created", new_value=event.name)`` as an event's first
    history row, so "who created this and when" is answered by the same list as
    every later edit. The demo seeder writes its events directly, so until
    tripl-0zpq.244 only the three events the ``_EDITS`` table touches had ANY
    history at all and the other fifteen opened on an empty History tab — on a
    project whose whole point is to look lived-in.

    Dated from the event's own ``created_at``, which is the column the detail
    page reads "first seen" from, so the first history row and the headline date
    cannot disagree. ``_edit_instant`` already keeps every seeded EDIT at least
    an hour after that instant, so the creation row stays first.
    """
    if not ctx.event_ids:
        return
    events = (
        (await session.execute(select(Event).where(Event.id.in_(list(ctx.event_ids.values())))))
        .scalars()
        .all()
    )
    session.add_all(
        [
            EventChange(
                event_id=event.id,
                user_id=ctx.created_by,
                field="created",
                old_value=None,
                new_value=event.name,
                created_at=event.created_at,
                updated_at=event.created_at,
            )
            for event in events
        ]
    )
    await session.flush()


async def _build_event_history(session: AsyncSession, ctx: DemoContext) -> None:
    """The demo's edit trail: one event shipped, and two retired."""
    ids = [ctx.event_ids[name] for name, _, _ in _EDITS if name in ctx.event_ids]
    events = {
        event.id: event
        for event in (await session.execute(select(Event).where(Event.id.in_(ids)))).scalars().all()
    }

    for name, days_ago, fields in _EDITS:
        event_id = ctx.event_ids.get(name)
        event = events.get(event_id) if event_id is not None else None
        if event is None:
            continue
        stamp = _edit_instant(ctx.now, days_ago, event.created_at)
        for changed_field, old_value, new_value in fields:
            if changed_field == _DERIVED_FROM_EVENT:
                # ``str``, not ``isoformat``: ``event_service._record_changes``
                # stringifies every tracked value with ``str(new_val)``, so an
                # isoformat here would be the one history row in the demo whose
                # formatting no real edit produces.
                new_value = str(event.sunset_at) if event.sunset_at else None
            session.add(
                EventChange(
                    event_id=event.id,
                    user_id=ctx.created_by,
                    field=changed_field,
                    old_value=old_value,
                    new_value=new_value,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
    await session.flush()


async def _build_figma_spec(session: AsyncSession, ctx: DemoContext) -> None:
    """A figma-kind attachment: embed URL only, no blob and no storage backend."""
    session.add(
        EventPhoto(
            project_id=ctx.project_id,
            event_id=ctx.event_ids["Paywall View"],
            uploaded_by_user_id=ctx.created_by,
            original_filename="Paywall redesign spec",
            content_type="",
            size_bytes=0,
            kind=EventPhotoKind.figma.value,
            external_url=_FIGMA_URL,
            storage_backend=None,
            storage_key=None,
            sort_order=0,
        )
    )
    await session.flush()
