"""Who watches what (#259): auto-subscribe, manual Watch/Unwatch and mute.

The write helpers never commit; they join the caller's transaction, so an
auto-subscribe lands or rolls back with the comment / event / branch that
caused it. Each has a sync core (``*_sync``) for Celery workers and an async
wrapper running the same core through ``AsyncSession.run_sync``, so the API and
the workers cannot drift.

This module imports models and ``project_lookup`` only at module level: every
service that auto-subscribes imports it, and ``_branch_counterparts`` (which
imports ``plan_branch_service``) is pulled in lazily where an event id has to
be canonicalised.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.metric_definition import MetricDefinition
from tripl.models.plan_branch import PlanBranch
from tripl.models.subscription import Subscription, SubscriptionEntityType, SubscriptionReason
from tripl.schemas.notification import SubscriptionState
from tripl.services.project_lookup import get_project_id_by_slug

EntityRef = tuple[str, uuid.UUID]

EVENT = SubscriptionEntityType.event.value
EVENT_TYPE = SubscriptionEntityType.event_type.value
METRIC = SubscriptionEntityType.metric.value
BRANCH = SubscriptionEntityType.branch.value

_EntityModel = type[Event] | type[EventType] | type[MetricDefinition] | type[PlanBranch]
_ENTITY_MODELS: dict[str, _EntityModel] = {
    EVENT: Event,
    EVENT_TYPE: EventType,
    METRIC: MetricDefinition,
    BRANCH: PlanBranch,
}


# ── sync cores (usable from a Celery task's Session) ────────────────────────


def _find_sync(
    session: Session, user_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
) -> Subscription | None:
    return session.scalar(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.entity_type == entity_type,
            Subscription.entity_id == entity_id,
        )
    )


def _insert_ignoring_duplicate(
    session: Session,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    reasons: list[str],
    muted: bool,
) -> None:
    """INSERT ... ON CONFLICT DO NOTHING on the (user, entity) key.

    A plain ``session.add`` would turn two concurrent auto-subscribes (the same
    person commenting twice at once) into an IntegrityError that fails the
    comment itself; losing that race must be a no-op instead.
    """
    values = {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "project_id": project_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "reasons": reasons,
        "muted": muted,
    }
    keys = ["user_id", "entity_type", "entity_id"]
    if session.get_bind().dialect.name == "sqlite":
        session.execute(
            sqlite_insert(Subscription).values(**values).on_conflict_do_nothing(index_elements=keys)
        )
    else:
        session.execute(
            pg_insert(Subscription).values(**values).on_conflict_do_nothing(index_elements=keys)
        )


def subscribe_sync(
    session: Session,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    reason: str,
    unmute: bool = False,
) -> None:
    """Make sure ``user_id`` watches the entity, recording ``reason``.

    An existing row gains the reason (reasons are a set) and keeps its mute
    unless ``unmute`` — auto-subscribe never un-mutes a thread someone muted.
    """
    reason = SubscriptionReason(reason).value
    row = _find_sync(session, user_id, entity_type, entity_id)
    if row is None:
        _insert_ignoring_duplicate(
            session,
            user_id=user_id,
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            reasons=[reason],
            muted=False,
        )
        row = _find_sync(session, user_id, entity_type, entity_id)
        if row is None or reason in (row.reasons or []):
            return
    if reason not in (row.reasons or []):
        # A new list, not an in-place append: JSON columns only see reassignment.
        row.reasons = [*(row.reasons or []), reason]
    if unmute and row.muted:
        row.muted = False


def drop_reason_sync(
    session: Session,
    *,
    user_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    reason: str,
) -> None:
    """Remove one reason; a row left with none and no mute is deleted.

    A muted row survives: the mute is a choice the user made, not a reason the
    system recorded.
    """
    row = _find_sync(session, user_id, entity_type, entity_id)
    if row is None:
        return
    remaining = [r for r in (row.reasons or []) if r != reason]
    if not remaining and not row.muted:
        session.delete(row)
        return
    row.reasons = remaining


def subscriber_ids_sync(
    session: Session,
    refs: Iterable[EntityRef],
    *,
    reasons: Iterable[str] | None = None,
) -> set[uuid.UUID]:
    """Users with an UNMUTED subscription on any of ``refs``.

    ``reasons`` narrows to rows carrying at least one of them (filtered in
    Python: the column is JSON, and the candidate set is one entity's watchers).
    """
    wanted = set(refs)
    if not wanted:
        return set()
    by_type: dict[str, set[uuid.UUID]] = {}
    for entity_type, entity_id in wanted:
        by_type.setdefault(entity_type, set()).add(entity_id)
    reason_filter = set(reasons) if reasons is not None else None
    out: set[uuid.UUID] = set()
    for entity_type, ids in by_type.items():
        rows = session.execute(
            select(Subscription.user_id, Subscription.reasons).where(
                Subscription.entity_type == entity_type,
                Subscription.entity_id.in_(ids),
                Subscription.muted.is_(False),
            )
        ).all()
        for user_id, row_reasons in rows:
            if reason_filter is None or reason_filter & set(row_reasons or []):
                out.add(user_id)
    return out


def muted_user_ids_sync(session: Session, ref: EntityRef) -> set[uuid.UUID]:
    entity_type, entity_id = ref
    rows = session.scalars(
        select(Subscription.user_id).where(
            Subscription.entity_type == entity_type,
            Subscription.entity_id == entity_id,
            Subscription.muted.is_(True),
        )
    )
    return set(rows.all())


def rekey_event_subscriptions_sync(
    session: Session, *, target_by_event_id: dict[uuid.UUID, uuid.UUID]
) -> None:
    """Move ``event`` subscriptions from each source event id to its target id.

    Used where a branch row's discussion moves to its main row (the merge, and
    the rescue before a branch row is deleted): the watchers go with the
    thread. Each source row is inserted under the target ON CONFLICT DO
    NOTHING; when the user already watches the target, the reasons are merged
    into that row (a set) and a mute on either side survives. The source row
    is then deleted, so nobody is left watching a row that is going away.
    """
    moves = {src: dst for src, dst in target_by_event_id.items() if src != dst}
    if not moves:
        return
    rows = session.scalars(
        select(Subscription).where(
            Subscription.entity_type == EVENT,
            Subscription.entity_id.in_(list(moves)),
        )
    ).all()
    for row in rows:
        target_id = moves[row.entity_id]
        reasons = list(row.reasons or [])
        _insert_ignoring_duplicate(
            session,
            user_id=row.user_id,
            project_id=row.project_id,
            entity_type=EVENT,
            entity_id=target_id,
            reasons=reasons,
            muted=row.muted,
        )
        target = _find_sync(session, row.user_id, EVENT, target_id)
        if target is not None and target.id != row.id:
            merged = list(dict.fromkeys([*(target.reasons or []), *reasons]))
            if merged != list(target.reasons or []):
                # A new list, not an in-place append: JSON columns only see reassignment.
                target.reasons = merged
            if row.muted and not target.muted:
                target.muted = True
        session.delete(row)
    session.flush()


# ── async wrappers (request path) ───────────────────────────────────────────


async def subscribe(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    reason: str,
    unmute: bool = False,
) -> None:
    """Async :func:`subscribe_sync`; a ``None`` user (system actor) is a no-op."""
    if user_id is None:
        return
    await session.run_sync(
        lambda s: subscribe_sync(
            s,
            user_id=user_id,
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            reason=reason,
            unmute=unmute,
        )
    )


async def drop_reason(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    reason: str,
) -> None:
    await session.run_sync(
        lambda s: drop_reason_sync(
            s, user_id=user_id, entity_type=entity_type, entity_id=entity_id, reason=reason
        )
    )


async def rekey_event_subscriptions(
    session: AsyncSession, *, target_by_event_id: dict[uuid.UUID, uuid.UUID]
) -> None:
    """Async :func:`rekey_event_subscriptions_sync` (same transaction)."""
    targets = dict(target_by_event_id)
    if not targets:
        return
    await session.run_sync(lambda s: rekey_event_subscriptions_sync(s, target_by_event_id=targets))


async def subscriber_ids(
    session: AsyncSession,
    refs: Iterable[EntityRef],
    *,
    reasons: Iterable[str] | None = None,
) -> set[uuid.UUID]:
    ref_list = list(refs)
    reason_list = list(reasons) if reasons is not None else None
    return await session.run_sync(lambda s: subscriber_ids_sync(s, ref_list, reasons=reason_list))


async def canonical_event_id(
    session: AsyncSession, project_id: uuid.UUID, event: Event
) -> uuid.UUID:
    """The id an event's subscriptions are kept under: its discussion home.

    The main twin of a branch copy, else the event itself — the same row
    ``event_comment_service.event_thread`` anchors new threads on, so watching
    an event and commenting on it agree whichever branch the page was on.
    """
    from tripl.services._branch_counterparts import main_counterparts

    twins = await main_counterparts(session, project_id=project_id, events=[event])
    return twins.get(event.id, event).id


# ── the Watch button (API) ──────────────────────────────────────────────────


async def resolve_entity(
    session: AsyncSession, slug: str, entity_type: str, entity_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """(project_id, canonical entity id) for a Watch target, 404 if not in the project."""
    project_id = await get_project_id_by_slug(session, slug)
    model = _ENTITY_MODELS.get(entity_type)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown entity type")
    row = await session.scalar(
        select(model).where(model.id == entity_id, model.project_id == project_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    if entity_type == EVENT:
        return project_id, await canonical_event_id(session, project_id, cast(Event, row))
    return project_id, entity_id


async def _find(
    session: AsyncSession, user_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
) -> Subscription | None:
    return await session.run_sync(lambda s: _find_sync(s, user_id, entity_type, entity_id))


def _state(entity_type: str, entity_id: uuid.UUID, row: Subscription | None) -> SubscriptionState:
    """The Watch button's state.

    ``watching`` means "there are reasons I follow this" — a muted row that
    still has reasons is watching AND muted (the button reads "Muted", with
    Unmute / Unwatch). A reason-less muted row exists only to hold the mute
    (an owner silencing one event of their type): muted, not watching.
    """
    known = {reason.value for reason in SubscriptionReason}
    reasons = [r for r in (row.reasons or []) if r in known] if row is not None else []
    return SubscriptionState.model_validate(
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "watching": row is not None and (not row.muted or bool(reasons)),
            "muted": row is not None and row.muted,
            "reasons": reasons,
        }
    )


async def get_state(
    session: AsyncSession,
    slug: str,
    entity_type: str,
    entity_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
) -> SubscriptionState:
    _, canonical = await resolve_entity(session, slug, entity_type, entity_id)
    return _state(entity_type, canonical, await _find(session, user_id, entity_type, canonical))


async def watch(
    session: AsyncSession,
    slug: str,
    entity_type: str,
    entity_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
) -> SubscriptionState:
    """Manual Watch: subscribe with reason ``manual`` and un-mute."""
    project_id, canonical = await resolve_entity(session, slug, entity_type, entity_id)
    await subscribe(
        session,
        user_id=user_id,
        project_id=project_id,
        entity_type=entity_type,
        entity_id=canonical,
        reason=SubscriptionReason.manual.value,
        unmute=True,
    )
    await session.commit()
    return _state(entity_type, canonical, await _find(session, user_id, entity_type, canonical))


async def unwatch(
    session: AsyncSession,
    slug: str,
    entity_type: str,
    entity_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
) -> SubscriptionState:
    """Unwatch: the whole row goes, every reason and the mute with it."""
    _, canonical = await resolve_entity(session, slug, entity_type, entity_id)
    row = await _find(session, user_id, entity_type, canonical)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return _state(entity_type, canonical, None)


async def set_muted(
    session: AsyncSession,
    slug: str,
    entity_type: str,
    entity_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    muted: bool,
) -> SubscriptionState:
    """Mute or un-mute. Muting something not watched keeps a reason-less muted row,
    which is what silences it for an owner who reaches it through the event type."""
    project_id, canonical = await resolve_entity(session, slug, entity_type, entity_id)
    row = await _find(session, user_id, entity_type, canonical)
    if row is None:
        if not muted:
            return _state(entity_type, canonical, None)
        await session.run_sync(
            lambda s: _insert_ignoring_duplicate(
                s,
                user_id=user_id,
                project_id=project_id,
                entity_type=entity_type,
                entity_id=canonical,
                reasons=[],
                muted=True,
            )
        )
        row = await _find(session, user_id, entity_type, canonical)
    if row is not None:
        row.muted = muted
        if not muted and not (row.reasons or []):
            # Un-muting a row that only existed to hold the mute: nothing left.
            await session.delete(row)
            row = None
    await session.commit()
    return _state(entity_type, canonical, row)
