"""Deliver held alerts on their destination's cadence.

A destination with ``delivery_schedule_cron`` set does not deliver after every
metrics collection. ``_prepare_alert_deliveries`` buffers its matched signals
into ``alert_pending_items`` instead, and this task turns the accumulated
buffer into ordinary ``AlertDelivery`` rows when a cron boundary passes.

The correctness argument, in one place, because it is the whole feature:

**Nothing is lost.** The buffered rows a flush ships are claimed by DELETING
them in the SAME transaction that mints the deliveries. There is no timestamp
predicate anywhere. That matters because ``created_at`` is
``server_default=func.now()`` — the INSERTing transaction's *start* clock — and
``collect_metrics`` builds buffer rows minutes before it commits. A row can
carry ``created_at = 09:00:01`` and only become visible at 09:00:20, so a flush
that selected ``created_at <= 09:00:10`` would neither see it nor ever see it
again once the next window's lower bound moved past. Claiming by row lifecycle
instead, a row that commits after the flush's snapshot is simply still
buffered, and lands in the next digest.

**Nothing is sent twice.** A row leaves the buffer exactly once, because the
DELETE and the delivery INSERT share a transaction: a rollback restores both,
a commit removes both. The window itself is claimed by a compare-and-set on
``last_flushed_at``, so a second tick — or a second worker — computing the same
fire instant gets ``rowcount == 0`` and does nothing.

That last one is a claim about a ROW. The SCOPE-level version of it needs one
more thing, because a scope can also be delivered by the IMMEDIATE path: where
``AlertRuleState.last_notified_at`` is NULL, dispatch's re-send gate fires on
the NULL regardless of cooldown, so that scope would arrive from both paths at
once. Clearing a destination's cadence therefore SPLITS its buffer, in the
transaction that clears the column
(``services._alerting_destinations.update_destination``): a row whose scope
carries that NULL is discarded there and the immediate path delivers it, while a
row whose scope a digest has already STAMPED is left for the drain arm below —
for that one the gate needs a strictly newer bucket AND an elapsed cooldown, so
the immediate path would deliver nothing and discarding it would destroy an
incident undelivered.

**Everything up to the moment of sending.** The digest carries every INCIDENT
that had committed at the instant of the flush's snapshot, each with the
numbers from the most recent collection that committed by then, because the
buffer is an upsert keyed on the scope AND its direction — the five components
``_correlation_group_id`` hashes. A scope that dropped and later spiked inside
one window is two incidents and ships two lines; a scope that fired once and
went quiet still ships its line, because nothing prunes a buffered row before
its digest. "Up to the moment of sending" is a promise about the NUMBERS being
current, not that the roster is a snapshot of what is broken right now.

The advisory lock is a coarse guard against overlapping runs, deliberately NOT
the correctness argument: it is a no-op off Postgres, so the compare-and-set
and the row lifecycle have to hold on their own — and they do.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from tripl.alerting_matching import AlertMatchCandidate, DriftAlertCandidate
from tripl.core.alert_schedule import previous_fire_at
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_pending_item import AlertPendingItem
from tripl.models.alert_rule import AlertRule
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services.active_org_scope import in_active_org
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session

# Everything from ``tripl.worker.tasks.metrics`` is imported INSIDE the
# functions below, never at module load. ``celery_app`` imports this module
# from its own bottom-of-file task registration, which can itself be reached
# from a partially-initialized ``tripl.worker.tasks.alerts`` — and a
# ``from tripl.worker.tasks.metrics.x import y`` at that moment resolves
# ``metrics/__init__``, which does ``from ...alerts import send_alert_delivery``
# against the half-built module and raises. ``maintenance.py`` defers its
# ``send_alert_delivery`` import for exactly this reason (see its comment).

logger = logging.getLogger(__name__)

# Distinct from the two metrics dispatchers (4_021_968_017 / _018) so the
# flusher never contends with them and none of the three can starve another.
_ALERT_FLUSH_ADVISORY_LOCK_KEY = 4_021_968_019

# A buffered row this old is not going to be delivered by its own cadence any
# more — the cron may never fire again (``0 0 30 2 *``), or the destination may
# have been left disabled. Dropping it is safe: a scope that is still firing
# re-buffers within one collection, so the only thing lost is a measurement
# nobody can act on. Disabling a destination clears its buffer outright, and
# switching it back to "Immediately" clears the part of it the immediate path
# re-delivers (``alerting_service``), so this is the backstop, not the main
# path.
PENDING_ITEM_MAX_AGE = timedelta(days=14)


def _sweep_aged_buffer(session: Session, *, now: datetime) -> int:
    cutoff = now - PENDING_ITEM_MAX_AGE
    result = session.execute(delete(AlertPendingItem).where(AlertPendingItem.updated_at < cutoff))
    removed = int(getattr(result, "rowcount", 0) or 0)
    if removed:
        logger.warning(
            "Dropped %d alert buffer rows older than %s — their destination's "
            "cadence has not fired in that time",
            removed,
            PENDING_ITEM_MAX_AGE,
        )
    return removed


def _rehydrate(row: AlertPendingItem) -> DriftAlertCandidate:
    """Rebuild the candidate the buffer snapshotted.

    ``DriftAlertCandidate`` declares every field ``AlertMatchCandidate``
    requires plus the four drift extras dispatch reads via ``getattr``, so one
    constructor is a faithful round trip. ``id`` carries the source anomaly for
    provenance only — nothing downstream dereferences it, and the row it names
    was deleted and rewritten several collections ago.
    """
    return DriftAlertCandidate(
        id=row.source_anomaly_id or row.id,
        scan_config_id=row.scan_config_id,
        scope_type=row.scope_type,
        scope_ref=row.scope_ref,
        event_id=row.event_id,
        event_type_id=row.event_type_id,
        bucket=row.bucket,
        direction=row.direction,
        actual_count=row.actual_count,
        expected_count=row.expected_count,
        drift_field=row.drift_field,
        drift_type=row.drift_type,
        sample_value=row.sample_value,
        window_from=row.window_from,
    )


def _build_digest(
    session: Session,
    destination: AlertDestination,
    *,
    now: datetime,
) -> list[uuid.UUID]:
    """Claim this destination's buffer and mint deliveries from it.

    Returns the new delivery ids. Caller commits; this function performs no
    commit of its own, because the claim (the DELETE) and the deliveries have
    to land or roll back together.
    """
    from tripl.worker.tasks.metrics.dispatch import (
        _as_utc,
        _create_deliveries,
        _suppressed_correlation_group_ids,
    )
    from tripl.worker.tasks.metrics.urls import _get_project_slug

    claimed = list(
        session.execute(
            select(AlertPendingItem)
            .where(AlertPendingItem.destination_id == destination.id)
            # Plain FOR UPDATE, deliberately not SKIP LOCKED. The window
            # compare-and-set and row lifecycle provide correctness; on Postgres
            # the advisory lock also keeps flushers from overlapping. The only transaction
            # that can hold one of these row locks is a ``collect_metrics``
            # mid-upsert — and skipping that row would drop the freshest
            # observation of the very scope that is firing hardest. Waiting for
            # its commit is bounded and yields the better digest.
            .with_for_update()
            # Deterministic input order, so the payload_snapshot JSON, the
            # AlertDeliveryItem rows and the rendered message — all three built
            # from this one list — agree with each other.
            .order_by(AlertPendingItem.bucket, AlertPendingItem.scope_name, AlertPendingItem.id)
        )
        .scalars()
        .all()
    )
    if not claimed:
        return []

    claimed_ids = [row.id for row in claimed]
    suppressed = _suppressed_correlation_group_ids(session, project_id=destination.project_id)
    project_slug = _get_project_slug(session, destination.project_id)

    rules = {
        rule.id: rule
        for rule in session.execute(
            select(AlertRule).where(AlertRule.id.in_({row.rule_id for row in claimed}))
        ).scalars()
    }
    configs = {
        config.id: config
        for config in session.execute(
            select(ScanConfig).where(
                ScanConfig.id.in_(
                    # A ``metric`` scope stores NULL, and NULL is not an id to
                    # resolve — including it would only widen the IN list with a
                    # value that can never match.
                    {row.scan_config_id for row in claimed if row.scan_config_id is not None}
                )
            )
        ).scalars()
    }

    # A ``metric`` scope is project-global and carries no scan config of its own.
    # The DELIVERY it produces still needs one:
    # ``AlertDelivery.scan_config_id`` is NOT NULL, the inbox INNER JOINs
    # ScanConfig on it, and the payload snapshot renders its name. So one is
    # resolved here, deterministically — oldest, id as tie-break — so that the
    # digest's scan name does not change under the reader between windows.
    #
    # It KEYS nothing, and identity for these rows is the NULL itself: the
    # handle ``dispatch._correlation_group_id`` hashes goes through
    # ``_PROJECT_GLOBAL_PARTITION``, and ``_alerting_deliveries`` normalises this
    # attributed config back to NULL before keying a false-positive override.
    # This is attribution of a message that really was sent, the same
    # distinction ``AlertPendingItem.scan_config_id`` draws.
    #
    # It is NOT inert, though, and a reader must not take "attribution" for
    # "decoration": the FK is ``ondelete="CASCADE"``, so the pick also decides
    # WHOSE deletion destroys this delivery and every ``AlertDeliveryItem`` under
    # it. Every project-global digest resolves to the same config, so where the
    # incident has no other delivery — no immediate send under the firing config,
    # no earlier pick — deleting that one scan takes every row carrying its
    # ``correlation_group_id``, while its ``AlertCorrelationState`` (project FK
    # only, never pruned) survives. An indefinite mute then outlives the only UI
    # that could lift it: the list builds cards from items, the silenced-orphan
    # rescue needs at least one delivery row to render one, and
    # every inbox action re-checks the same item join and 404s without it.
    #
    # No pick avoids this — any config can be deleted, so there is no safe one to
    # prefer — which is why the choice stays the deterministic oldest above.
    # Releasing a state whose rows are gone is the inbox's job, not this
    # attribution's.
    #
    # It is also the one place this change could LOSE DATA. Without it the
    # project-global group falls through the ``config is None`` guard below and
    # is skipped by every digest — while still being deleted as claimed at the
    # end of this function. The alert would be destroyed, undelivered and
    # unrecoverable, within a minute of the new worker booting.
    project_global_config = None
    if any(row.scan_config_id is None for row in claimed):
        project_global_config = (
            session.execute(
                select(ScanConfig)
                .where(ScanConfig.project_id == destination.project_id)
                .order_by(ScanConfig.created_at, ScanConfig.id)
                .limit(1)
            )
            .scalars()
            .first()
        )

    # One delivery per (rule, scan config), exactly as the immediate path
    # produces — which is what keeps the per-rule message/items templates
    # meaningful. What changes is WHEN: they all go out together, on the
    # cadence, instead of trickling out after each collection.
    #
    # A NULL config is a group in its own right: the project-global ``metric``
    # scopes, which is ONE group per rule however many scans observed them —
    # the whole point of keying them on NULL.
    grouped: dict[tuple[uuid.UUID, uuid.UUID | None], list[AlertPendingItem]] = defaultdict(list)
    for row in claimed:
        grouped[(row.rule_id, row.scan_config_id)].append(row)

    delivery_ids: list[uuid.UUID] = []
    for (rule_id, scan_config_id), rows in grouped.items():
        rule = rules.get(rule_id)
        config = project_global_config if scan_config_id is None else configs.get(scan_config_id)
        if rule is None or config is None:
            # Either the rule or the scan was deleted while these alerts waited,
            # or this is the project-global metric group in a project with no
            # scan config left at all to render it against. The rows are still
            # claimed and deleted below, so this cannot loop.
            continue
        # Re-checked at flush, not only at buffer time: an operator who
        # disables or mutes a monitor during the hold window expects the digest
        # to honour that, and on a daily cadence that window is a whole day.
        #
        # Skipping here is a DROP, not a deferral: these rows are already in
        # ``claimed``, so the delete at the end of this function takes them with
        # the rest and a mute silences what it caught instead of releasing it in
        # the first digest after the mute lapses.
        #
        # ``dispatch._prepare_alert_deliveries`` holds the immediate path's twin
        # of the mute check; between them they are the worker's only readers of
        # ``AlertRule.muted_until``.
        if not rule.enabled:
            continue
        muted_until = _as_utc(rule.muted_until)
        if muted_until is not None and muted_until > now:
            continue

        # Filtered on the id the buffered ROW carries, never on one recomputed
        # here. Since tripl-0zpq.27 that is also the id the immediate path
        # computes for the same incident — both hash the partition the row
        # stores — so a decision taken in the Inbox while a digest is being held
        # silences it here, and a decision taken on a digest silences the
        # immediate path too. For a ``metric`` scope on a multi-scan project the
        # two used to disagree, which made a mute a coin-flip on which scan
        # collected next.
        live = [row for row in rows if row.correlation_group_id not in suppressed]
        if not live:
            continue

        candidates: list[AlertMatchCandidate] = [_rehydrate(row) for row in live]
        delivery_ids.extend(
            _create_deliveries(
                session,
                config,
                project_slug=project_slug,
                rule=rule,
                destination=destination,
                anomalies=candidates,
                scope_names={(row.scope_type, row.scope_ref): row.scope_name for row in live},
                correlation_by_anomaly={
                    id(candidate): row.correlation_group_id
                    for candidate, row in zip(candidates, live, strict=True)
                },
                # A digest is not the product of any one scan job — its rows
                # come from however many collections happened in the window —
                # so it claims none.
                scan_job_id=None,
                # One delivery, so one message and one AI note over everything.
                chunk_items=False,
            )
        )

    # Delete exactly what was claimed, by id. NEVER by destination_id: a row
    # that committed between the SELECT above and this statement would be
    # destroyed without ever being delivered, and it would be unrecoverable.
    session.execute(delete(AlertPendingItem).where(AlertPendingItem.id.in_(claimed_ids)))
    return delivery_ids


def _dispatch_digest(
    session: Session,
    *,
    destination_id: uuid.UUID,
    delivery_ids: list[uuid.UUID],
    send_one: object,
    send_group: object,
    combinable: frozenset[str],
) -> None:
    """Send this destination's flushed deliveries as one message, or as N.

    Combining is decided by the number of DISTINCT RULES, never by the number
    of deliveries. ``_delivery_chunks`` already splits ONE rule's matches into
    several AlertDelivery rows for a channel with a per-message item cap, and
    bundling those back together would stack two "N alerts" banners for the
    same rule in one message — while re-doing the split the chunking exists to
    avoid. One rule therefore always keeps the ordinary per-delivery path,
    byte-identical to an immediate send.
    """
    destination = session.get(AlertDestination, destination_id)
    rule_ids = set(
        session.execute(
            select(AlertDelivery.rule_id).where(AlertDelivery.id.in_(delivery_ids))
        ).scalars()
    )
    combine = destination is not None and destination.type in combinable and len(rule_ids) > 1
    if combine:
        send_group.delay([str(value) for value in delivery_ids])  # type: ignore[attr-defined]
        return
    for delivery_id in delivery_ids:
        send_one.delay(str(delivery_id))  # type: ignore[attr-defined]


@celery_app.task(name="tripl.worker.tasks.alert_flush.flush_due_alert_digests")  # type: ignore[untyped-decorator]
def flush_due_alert_digests() -> dict[str, int]:
    """Send the digest for every destination whose cadence has come round."""
    from tripl.worker.tasks.alert_digest_send import COMBINABLE_CHANNELS, send_alert_digest
    from tripl.worker.tasks.alerts import send_alert_delivery
    from tripl.worker.tasks.metrics.dispatch import _as_utc
    from tripl.worker.tasks.metrics.schedule import (
        _release_advisory_lock,
        _try_acquire_advisory_lock,
    )

    session = _get_sync_session()
    lock_conn, acquired = _try_acquire_advisory_lock(session, _ALERT_FLUSH_ADVISORY_LOCK_KEY)
    if not acquired:
        logger.info("flush_due_alert_digests: another run holds the lock; skipping this tick")
        session.close()
        return {"checked": 0, "flushed": 0, "deliveries": 0, "swept": 0}

    checked = 0
    flushed = 0
    dispatched: dict[uuid.UUID, list[uuid.UUID]] = {}
    swept = 0
    try:
        now = datetime.now(UTC)
        swept = _sweep_aged_buffer(session, now=now)
        session.commit()

        # DRAIN. A destination with no cadence has no window to wait for, and
        # the scheduled loop below only looks at destinations that HAVE one, so
        # anything buffered against this one would sit until the 14-day sweep
        # quietly dropped it. Ship it on the next tick instead.
        #
        # This arm is not the handoff. Switching a destination back to
        # "Immediately" SPLITS its buffer in the transaction that clears the
        # column (``services._alerting_destinations.update_destination``): a row
        # whose scope has never been notified is discarded there, because the
        # IMMEDIATE path fires on that NULL ``AlertRuleState.last_notified_at``
        # regardless of cooldown and would otherwise deliver the same scope this
        # arm just did.
        #
        # What the split leaves is ours to deliver, because nothing else will:
        #
        # * the rows it KEEPS — a scope a digest already reported carries a
        #   stamp, and dispatch's re-send gate then needs a strictly newer
        #   bucket AND an elapsed cooldown (1440 minutes by default), neither of
        #   which holds at the moment of the switch. Those incidents arrive
        #   HERE, once: the gate that blocks the immediate path for them is the
        #   same column the split reads to leave them behind;
        # * a ``collect_metrics`` that read the cadence BEFORE the switch and
        #   committed its buffer rows after it;
        # * rows left by an older worker or a hand-edit that never went through
        #   the service.
        #
        # It deliberately does NOT filter on ``last_notified_at``. That
        # predicate reads as "someone has already been told about this scope",
        # but a state row carries no direction while a buffered row does, so it
        # would destroy a buffered DROP the moment an unrelated SPIKE on the
        # same scope was sent — the undeliverable-sibling trap
        # ``dispatch._buffer_pending_items`` argues in full, and a stamped
        # scope is now exactly what the split hands this arm.
        draining = (
            session.execute(
                select(AlertDestination)
                .join(AlertPendingItem, AlertPendingItem.destination_id == AlertDestination.id)
                .where(
                    AlertDestination.enabled.is_(True),
                    AlertDestination.delivery_schedule_cron.is_(None),
                    in_active_org(AlertDestination.project_id),
                )
                .order_by(AlertDestination.id)
                .distinct()
            )
            .scalars()
            .all()
        )
        for destination in draining:
            try:
                delivery_ids = _build_digest(session, destination, now=now)
            except Exception:
                session.rollback()
                logger.exception("Failed to drain buffer for destination %s", destination.id)
                continue
            destination.last_flushed_at = None
            session.commit()
            if delivery_ids:
                flushed += 1
                dispatched.setdefault(destination.id, []).extend(delivery_ids)

        rows = session.execute(
            select(AlertDestination, Project.timezone)
            .join(Project, Project.id == AlertDestination.project_id)
            .where(
                AlertDestination.enabled.is_(True),
                AlertDestination.delivery_schedule_cron.isnot(None),
                in_active_org(AlertDestination.project_id),
            )
            .order_by(AlertDestination.id)
        ).all()

        for destination, project_timezone in rows:
            checked += 1
            cron = destination.delivery_schedule_cron
            if cron is None:  # pragma: no cover - filtered in SQL
                continue
            last = _as_utc(destination.last_flushed_at)

            if last is None:
                # First tick after a cadence was attached: adopt the clock and
                # send nothing. Anything already buffered waits for the next
                # real fire rather than being dumped now. A compare-and-set so
                # two workers cannot both adopt. (The API stamps this when the
                # schedule is set, so this is defence, not the usual path.)
                session.execute(
                    update(AlertDestination)
                    .where(
                        AlertDestination.id == destination.id,
                        AlertDestination.last_flushed_at.is_(None),
                    )
                    .values(last_flushed_at=now)
                    .execution_options(synchronize_session=False)
                )
                session.commit()
                continue

            try:
                fire_at = previous_fire_at(cron, tz_name=project_timezone, now=now, not_before=last)
            except ValueError:
                # One unusable expression must not stop every other
                # destination's digest. It is already rejected at write time,
                # so reaching here means the row predates that validation.
                logger.exception(
                    "Destination %s has an unusable delivery schedule %r", destination.id, cron
                )
                continue
            if fire_at is None or fire_at <= last:
                continue

            # (A) Claim the WINDOW. The only guard against a second tick or a
            # second worker shipping this same digest: both recompute the same
            # ``fire_at`` for the window they are in, so exactly one of them can
            # move the watermark past it and the loser gets ``rowcount == 0``.
            # Storing the fire instant rather than ``now`` is what makes that
            # work — ``now`` is different on every tick, so the predicate would
            # pass every time and reject nothing.
            #
            # What it does NOT do is collapse a repeated DST wall-clock time,
            # and it is not trying to. On the autumn fold 02:30 happens twice
            # and resolves to two instants an hour apart (``_utc_instants`` in
            # ``core/alert_schedule``), so both pass this predicate and that day
            # gets two windows — one per real hour, which is the cadence
            # working rather than a double send. Nothing is delivered twice
            # either way: the buffer is claimed by deletion in (B), so the
            # second window carries only what arrived after the first.
            claimed_window = int(
                getattr(
                    session.execute(
                        update(AlertDestination)
                        .where(
                            AlertDestination.id == destination.id,
                            AlertDestination.last_flushed_at < fire_at,
                        )
                        .values(last_flushed_at=fire_at)
                        .execution_options(synchronize_session=False)
                    ),
                    "rowcount",
                    0,
                )
                or 0
            )
            if claimed_window != 1:
                session.rollback()
                continue

            # (B) Claim the CONTENT, in the same transaction.
            try:
                delivery_ids = _build_digest(session, destination, now=now)
            except Exception:
                # Roll the watermark back with everything else: advancing it
                # while the rows stayed buffered would mean a full cadence
                # period of silence for alerts that were ready to go.
                session.rollback()
                logger.exception("Failed to build digest for destination %s", destination.id)
                continue

            # (C) One commit for the watermark, the deliveries and the claim.
            session.commit()

            if not delivery_ids:
                # Empty window: no message, and the watermark still advanced —
                # mandatory, or the first alert to arrive would find the window
                # still "due" and flush within a minute, turning a daily
                # destination back into a near-immediate one.
                continue
            flushed += 1
            dispatched.setdefault(destination.id, []).extend(delivery_ids)

        # Enqueued after the commit, exactly as collect_metrics does it. If a
        # publish is lost the deliveries are already `pending`, so the existing
        # stranded-delivery reaper ships them within 15 minutes. Digest members
        # retain their digest layout when the reaper dispatches them.
        for destination_id, delivery_ids in dispatched.items():
            try:
                _dispatch_digest(
                    session,
                    destination_id=destination_id,
                    delivery_ids=delivery_ids,
                    send_one=send_alert_delivery,
                    send_group=send_alert_digest,
                    combinable=COMBINABLE_CHANNELS,
                )
            except Exception:
                logger.exception("Failed to dispatch digest for destination %s", destination_id)

        return {
            "checked": checked,
            "flushed": flushed,
            "deliveries": sum(len(ids) for ids in dispatched.values()),
            "swept": swept,
        }
    finally:
        _release_advisory_lock(lock_conn, _ALERT_FLUSH_ADVISORY_LOCK_KEY)
        session.close()
