from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from tripl.alert_templates import percent_delta_of
from tripl.alerting_matching import (
    PROJECT_GLOBAL_SCOPE_TYPES,
    AlertMatchCandidate,
    is_project_global_scope,
    rule_matches_anomaly,
)
from tripl.core.analyzers.anomaly_detector import SCOPE_METRIC
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_pending_item import AlertPendingItem
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.domain_enums import AnomalyDirection
from tripl.models.scan_config import ScanConfig
from tripl.services import app_settings_service
from tripl.worker.tasks.alerts_pagerduty import queue_pagerduty_resolves
from tripl.worker.tasks.metrics.alert_payload import (
    _build_alert_scope_names,
    _build_attribution_lines,
    _build_delivery_snapshot,
    _build_event_type_by_event_id,
    _load_enabled_alert_destinations,
)
from tripl.worker.tasks.metrics.lifecycle_alerts import _get_lifecycle_candidates
from tripl.worker.tasks.metrics.signals import (
    _get_active_distribution_drift_candidates,
    _get_active_metric_anomaly_candidates,
    _get_active_property_drift_candidates,
    _get_active_release_regression_candidates,
    _get_active_schema_drift_candidates,
    _get_active_variable_value_drift_candidates,
    _get_latest_active_anomalies,
    _get_source_freshness_candidates,
)
from tripl.worker.tasks.metrics.urls import (
    _build_item_paths,
    _get_org_slug,
    _get_project_slug,
)

# Pure rule/anomaly matchers live in tripl.alerting_matching so the in-UI
# simulator and the live pipeline use a single source of truth. Kept as an
# alias here because this module references it via the private name.
_rule_matches_anomaly = rule_matches_anomaly
_CORRELATION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "tripl-alert-correlation")


def _as_utc(value: datetime | None) -> datetime | None:
    """Postgres hands back tz-aware values for timestamptz; SQLite does not.

    Comparing a naive stored value against ``datetime.now(UTC)`` raises, so the
    mute-expiry checks below normalise first rather than assume the driver.
    """
    if value is None:
        return None
    return _utc_bucket(value)


def _utc_bucket(bucket: datetime) -> datetime:
    if bucket.tzinfo is None:
        return bucket.replace(tzinfo=UTC)
    return bucket.astimezone(UTC)


def _bucket_is_newer(bucket: datetime, previous: datetime | None) -> bool:
    """Compare stored SQLite and PostgreSQL buckets as UTC instants."""
    if previous is None:
        return True
    return _utc_bucket(bucket) > _utc_bucket(previous)


def _latest_bucket(bucket: datetime, previous: datetime | None) -> datetime:
    """Keep the original timestamp representation when advancing state."""
    if previous is None or _bucket_is_newer(bucket, previous):
        return bucket
    return previous


def _cooldown_elapsed(
    last_notified_at: datetime | None,
    *,
    now: datetime,
    cooldown_minutes: int,
) -> bool:
    """Whether ``cooldown_minutes`` have passed since this scope was last notified.

    Keyed on the last SUCCESSFUL notification (``alerts.py`` stamps
    ``last_notified_at`` only on a sent delivery), not on the open/close flag, so
    a scope that closes and reopens on the next collection is still inside its
    cooldown while one that reopens days later is not.

    A NULL means the operator has never been told about this scope — a cooldown
    cannot have elapsed on a message that was never sent, so it reads as elapsed
    and the first delivery goes out.
    """
    last = _as_utc(last_notified_at)
    if last is None:
        return True
    return now - last >= timedelta(minutes=cooldown_minutes)


# The partition a project-global scope hashes into, where a config-scoped one
# hashes its scan config id.
#
# A NON-UUID literal on purpose: no scan config id can ever render as this
# string, so the project-global id space is provably DISJOINT from every
# config-scoped one and no config can collide into a catalog metric's incident
# handle. It is also drift-proof by construction — there is no row behind it to
# be created, deleted or re-elected, which is precisely what went wrong with the
# anchor it replaces.
#
# The PROJECT ID is deliberately not hashed in its place. ``rule_id`` is already
# in the key and a rule belongs to exactly one destination, which belongs to
# exactly one project, so a project component adds no discriminating power — it
# would only put a different KIND of uuid into the slot that otherwise holds a
# config id, which is the overlap this literal exists to rule out.
# ``AlertCorrelationState`` is looked up with an explicit ``project_id`` filter
# anyway (see ``_suppressed_correlation_group_ids``).
_PROJECT_GLOBAL_PARTITION = "project-global"


def _scope_partition_id(scope_type: str, *, config_id: uuid.UUID) -> uuid.UUID | None:
    """Which partition a scope lives in: NO config if project-global, else the firing one.

    One function for the one question this module keeps asking, because all
    three answers have to agree: the ``AlertRuleState`` a run writes, the
    ``AlertPendingItem`` it buffers, and the incident handle
    ``_correlation_group_id`` hashes. A ``metric`` scope is project-global — its
    anomaly row carries a NULL ``scan_config_id`` of its own — so all three store
    or hash nothing for the config, and every scan's run converges on one state
    row, one cooldown clock and one incident. Converges even when those runs are
    concurrent: sharing a row across workers is what makes its creation a race,
    which ``_claim_rule_state`` settles in the database rather than in the load.

    They used to disagree. The state and the buffer were keyed project-wide while
    the handle hashed the FIRING config, so a three-scan project minted three
    handles for one project-global scope: an Inbox acknowledgement or mute on one
    of them left the other two paging on the next collection, and the Inbox
    listed up to N rows for a single incident.

    ``services/_alerting_deliveries`` asks the same question of the
    false-positive ratchet and answers it the same way.

    ``lifecycle`` (GH #258) is project-global for the same reason: a finding
    hangs on an event, not on a scan, and EVERY config's run emits the open
    findings as candidates. Stored under NULL, those runs converge on one state
    and ``_claim_rule_state``'s converging INSERT lets exactly one of them send,
    so a three-scan project alerts once per finding — with no "anchor" config to
    elect, and so nothing to re-elect when a config is created or deleted.
    """
    return None if is_project_global_scope(scope_type) else config_id


def _correlation_group_id(
    *,
    scan_config_id: uuid.UUID | None,
    rule_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    direction: str,
) -> uuid.UUID:
    """The stable handle for one ongoing incident: one rule, one scope, one direction.

    The BUCKET is deliberately absent. While it was part of the key, every hour
    of the same incident was a brand-new group and nothing the user did in the
    inbox survived the next collection: acknowledging, resolving or muting
    silenced exactly the bucket already delivered, and an hour later an unseen
    group alerted again. Leaving it out makes the group live as
    long as the incident does, and it must stay out.

    The SCOPE is present because ``_SUPPRESSING_INBOX_STATUSES`` gates the whole
    group. Keyed on scan_config:rule:direction alone, one inbox action silenced
    every other scope the rule watched, and the sole release path
    (``_reopen_closed_incidents``) waited for every scope of the rule to close —
    which the suppressed scope, still firing and now unseen, prevented. On
    production one 2026-07-30 operator note ("these screens are switched off")
    on group bd6c96f5 covered 7 unrelated iOS scopes of a single "drop" rule.

    ``_reopen_closed_incidents`` now resets a scope's groups as soon as THAT
    scope's alert state is closed, so a genuinely new incident is never silenced
    by an old decision.

    ``scan_config_id`` is the scope's PARTITION, not "the config collecting right
    now": a NULL is the project-global one, which is what a ``metric`` scope
    stores in its state row and its buffered row (``_scope_partition_id``). It
    renders through ``_PROJECT_GLOBAL_PARTITION`` rather than through ``None``'s
    repr, so the string that is hashed says what it means and cannot be produced
    by any scan config id. Callers pass the partition, never a config id "for a
    metric scope anyway" — that is the bug this rule replaced.
    """
    partition = _PROJECT_GLOBAL_PARTITION if scan_config_id is None else str(scan_config_id)
    return uuid.uuid5(
        _CORRELATION_NAMESPACE,
        f"{partition}:{rule_id}:{scope_type}:{scope_ref}:{direction}",
    )


# Statuses that stop re-delivery. ``acknowledged`` means "seen, being worked on"
# — it belongs here: an operator who acked an incident and kept getting paged
# for it every hour reported the inbox as decorative, which it was, since ack
# was the one action with no effect on delivery at all.
_SUPPRESSING_INBOX_STATUSES = ("acknowledged", "resolved", "false_positive", "muted")


def _suppressed_correlation_group_ids(
    session: Session,
    *,
    project_id: uuid.UUID,
) -> set[uuid.UUID]:
    now = datetime.now(UTC)
    rows = session.execute(
        select(AlertCorrelationState).where(
            AlertCorrelationState.project_id == project_id,
            AlertCorrelationState.status.in_(_SUPPRESSING_INBOX_STATUSES),
        )
    ).scalars()
    suppressed: set[uuid.UUID] = set()
    for state in rows:
        muted_until = _as_utc(state.muted_until)
        if state.status == "muted" and muted_until is not None and muted_until <= now:
            state.status = "open"
            state.muted_until = None
            continue
        suppressed.add(state.correlation_group_id)
    return suppressed


def _reopen_closed_incidents(
    session: Session,
    *,
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID | None,
    rule_id: uuid.UUID,
    scope_keys: Iterable[tuple[str, str]],
) -> None:
    """Clear an incident-scoped inbox decision once the incident is over.

    Without this, suppression would be permanent: acknowledging a drop would
    silence that scope's drops forever. Called for the scopes whose alert state
    is closed, so their next firing is a new incident and alerts normally.

    Per SCOPE, matching ``_correlation_group_id``. The old rule-wide reset needed
    every scope of the rule to be quiet at once, which a suppressed scope could
    never be — it kept firing, unseen, holding its own release hostage.

    ONE PARTITION PER CALL. ``scan_config_id`` is the partition the ids being
    released were minted under — ``None`` for the project-global (``metric``,
    ``lifecycle``)
    scopes, the firing config for every other scope — so a caller whose closed
    keys span both makes two calls (``_prepare_alert_deliveries`` does). Ids
    rebuilt under the wrong partition match no stored row, and a release that
    matches nothing is a suppression that never ends: the incident stays
    acknowledged forever and its scope can never alert again.

    A mute is deliberately excluded, timed or indefinite. "Acknowledged" means
    "I am on this incident" and dies with it; "muted until T" means "do not tell
    me before T" regardless of what the signal does in between. Resetting it here
    killed a seven-day mute on the first quiet collection and paged the user
    again hours later.

    An INDEFINITE mute (``muted_until`` NULL — "muted until I unmute") is the
    same promise with no T at all, and is the one a fall-through hurts most: its
    release is a deliberate human act, so nothing downstream would ever restore
    the row. The check below is therefore ``muted_until is None or muted_until >
    now`` and NOT ``is not None and > now``, which read a NULL as an expiry
    infinitely far in the past and silently released the strongest mute in the
    product on the first quiet scan. Unreachable until the inbox
    validator started accepting a mute with no expiry, which is precisely why it
    had to be fixed in the same change.

    Note the OPPOSITE shape at the rule-mute check in
    ``_prepare_alert_deliveries``: on an ``AlertRule`` a NULL ``muted_until``
    means NOT MUTED, because a rule carries no status column to tell "never
    muted" from "muted forever" and NULL is the default on every rule ever
    created. The two lines look alike and must not be made to agree — see
    ``AlertInboxActionRequest.validate_action``.

    A LAPSED mute is still reopened, by ``_suppressed_correlation_group_ids``, so
    mutes have their own lifecycle and this function does not run it.
    """
    group_ids = [
        _correlation_group_id(
            scan_config_id=scan_config_id,
            rule_id=rule_id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            direction=direction.value,
        )
        for scope_type, scope_ref in scope_keys
        for direction in AnomalyDirection
    ]
    if not group_ids:
        return
    now = datetime.now(UTC)
    for state in session.execute(
        select(AlertCorrelationState).where(
            AlertCorrelationState.project_id == project_id,
            AlertCorrelationState.correlation_group_id.in_(group_ids),
            AlertCorrelationState.status != "open",
        )
    ).scalars():
        muted_until = _as_utc(state.muted_until)
        if state.status == "muted" and (muted_until is None or muted_until > now):
            continue
        state.status = "open"
        state.muted_until = None


# The two columns of ``uq_alert_correlation_state_project_group``. Spelled out
# because the claim below names them as conflict targets while the model names
# them as the constraint — they have to stay the same list, exactly as
# ``_RULE_STATE_CONFLICT_KEYS`` and ``_PENDING_ITEM_CONFLICT_KEYS`` do for the
# tables either side of this one.
#
# Named as COLUMNS and not as the constraint. ``ON CONFLICT ON CONSTRAINT`` is
# PostgreSQL-only syntax while SQLite's upsert accepts a column list only, so a
# constraint name here would compile in production and fail in the unit suite.
# There is no partial-index arm to branch on the way the other two have: both
# columns are NOT NULL, so this one constraint answers for every row.
_CORRELATION_STATE_CONFLICT_KEYS = ("project_id", "correlation_group_id")


def _touch_correlation_state(
    session: Session,
    *,
    project_id: uuid.UUID,
    correlation_group_id: uuid.UUID,
    seen_at: datetime,
) -> None:
    """Keep an incident's inbox row alive, converging when a peer opens it first.

    This was a select-then-``session.add``, and since a later fix its two
    halves can be executed by two workers at once. ``check_metrics_due``
    dispatches ``collect_metrics.delay(str(config.id), str(job.id))`` per CONFIG,
    so two configs of one project collect in parallel; the handle they touch is
    ``_correlation_group_id``, which hashes the scope's PARTITION, and for a
    project-global ``metric`` scope that partition is the same NULL in both runs.
    The two used to hash their own firing config and write different rows — the
    bug that fix addressed — so agreeing on ONE handle is the point of that
    change, and it is also what turned the first touch of a handle into a race.

    ``_claim_rule_state`` does not serialize this one. Its key is the rule state,
    which carries no ``direction``, while the incident handle does — so a scope
    that dropped in the morning and spikes in the afternoon reuses the single
    state row it already has (both runs take the branch for an EXISTING state,
    where nothing is claimed) and then touches a second, brand-new handle. That
    flip is the common case rather than a corner: 106 of 223 live scopes fired in
    BOTH directions inside one day, recorded at ``_buffer_pending_items``.

    ``uq_alert_correlation_state_project_group`` is a plain, non-deferrable
    unique constraint — ``f4a8d3c72e19._move_correlation_states`` leans on
    exactly that when it merges rekeyed rows — so the loser of that race raised
    IntegrityError at the next flush, inside ``_prepare_alert_deliveries``, which
    nothing wraps in a savepoint. That took the whole ``collect_metrics`` run
    down with it: the anomaly recalculation, the cooldown updates and every
    delivery for every scope of that scan. The same blast radius as the rule
    state race one table over, and reachable for the same reason.

    INSERT ... ON CONFLICT DO NOTHING and a re-read, the shape
    ``_claim_rule_state``, ``_buffer_pending_items`` and ``detect.py`` already
    use. Not a ``begin_nested()`` savepoint: that flushes the caller's pending
    work at ROOT level on the way in — the reason
    ``_event_identity.insert_event_claiming_identity`` has to flush ABOVE its own
    ``try`` — and both call sites reach this function holding unflushed ORM work
    (the delivery path an unflushed ``AlertDeliveryItem``, the buffer path the
    rule-state rows the send gate has just left dirty), whose own failure would
    then surface inside the ``try`` and be read as this row's collision.

    DO NOTHING rather than DO UPDATE, because the row already there is the
    OPERATOR's: ``status``, ``muted_until``, ``note``, ``acted_at``,
    ``acted_by`` and ``false_positive_count`` are what an inbox action wrote
    (``_alerting_deliveries._apply_inbox_action_to_state``), and a collection has
    no business restating any of them. Only ``last_seen_at`` moves, and it moves
    in PYTHON after the re-read rather than in a SET clause: the two backends
    have no shared spelling of a maximum. PostgreSQL has ``GREATEST``, SQLite has
    a two-argument ``max()``, and they disagree precisely where it matters —
    ``GREATEST`` ignores a NULL argument while SQLite's ``max()`` returns NULL if
    any argument is NULL, so the same SET clause would set the timestamp on a
    never-seen row in production and ERASE it in the unit suite.

    ``last_seen_at`` is left to the INSERT on the winning path and never
    re-compared there, which is what the early return is for: the row the
    statement just wrote already holds ``seen_at``, so there is nothing to take a
    maximum against and no second statement worth spending.

    A re-read that finds NOTHING is left alone. ``AlertCorrelationState`` is
    never pruned (``_alerting_deliveries._load_orphan_group_rows`` says so and
    budgets for it), so the only way to lose the holder between two statements of
    one transaction is the project being deleted underneath the run and taking
    the row with it by cascade. The guard is here because the collection this
    function is a detail of is worth more than one incident's last-seen stamp.
    """
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    insert = sqlite_insert if dialect == "sqlite" else postgresql_insert
    claim = (
        insert(AlertCorrelationState)
        .values(
            # Spelled out for symmetry with this package's other Core inserts;
            # UUIDMixin's ``default=uuid.uuid4`` would render it anyway. The
            # timestamp columns are left out because theirs are SERVER defaults,
            # which a Core INSERT does not bypass — and so is ``status``, which
            # is passed anyway so this row's opening state is stated where it is
            # made rather than read out of the migration.
            id=uuid.uuid4(),
            project_id=project_id,
            correlation_group_id=correlation_group_id,
            status="open",
            last_seen_at=seen_at,
        )
        .on_conflict_do_nothing(index_elements=list(_CORRELATION_STATE_CONFLICT_KEYS))
    )
    # RETURNING yields the id only when the row was actually inserted — DO
    # NOTHING inserts nothing and returns nothing — so this answers "did THIS
    # statement open the incident" without inferring it from what a later read
    # happens to find.
    created = (
        session.execute(claim.returning(AlertCorrelationState.id)).scalar_one_or_none() is not None
    )
    if created:
        return
    # A second statement on purpose. Under READ COMMITTED it takes a fresh
    # snapshot, which is what lets it see a row this transaction's own INSERT
    # could not: on PostgreSQL the peer may still be UNCOMMITTED when the claim
    # above runs, and ON CONFLICT waits for it to commit or abort, so by the time
    # control reaches here the winner's row is visible either way.
    state = session.execute(
        select(AlertCorrelationState).where(
            AlertCorrelationState.project_id == project_id,
            AlertCorrelationState.correlation_group_id == correlation_group_id,
        )
    ).scalar_one_or_none()
    if state is None:
        return
    # The maximum, written as a guarded assignment rather than ``max(...)`` so
    # the value STORED is the one that was read rather than a normalised copy of
    # it. Only the COMPARISON goes through ``_as_utc``: ``last_seen_at`` and
    # every ``bucket`` that reaches ``seen_at`` are plain
    # ``DateTime(timezone=True)`` columns rather than the module's ``UtcDateTime``
    # decorator, so a driver that drops tzinfo hands back a naive value on one
    # side of this while a caller holding an aware one raises on the comparison
    # alone. ``_as_utc`` is what this module already answers that with.
    stored = _as_utc(state.last_seen_at)
    observed = _as_utc(seen_at) or seen_at
    if stored is None or stored < observed:
        state.last_seen_at = seen_at


# Telegram rejects a sendMessage body over 4096 characters with a 400, and
# ``last_notified_at`` is stamped only on a SUCCESSFUL send (alerts.py), while
# the re-send gate below treats a NULL ``last_notified_at`` as "never told
# them" — so one oversized delivery is rebuilt and re-rejected on every
# collection, forever.
#
# Sized off the 29 Telegram deliveries acme-ios has ever sent ("TG dev",
# default templates, AI note attached): least squares over their
# (matched_count, rendered chars) gave 400 chars per item on a 516-char base,
# which crosses 4096 just under 9 items — and on the widest base observed (682,
# the AI note varies) exactly at 8.
#
# Those renders PREDATE aafa632, which collapsed the duplicate
# details/monitoring line when both resolve to the same page — worth about 90
# characters per item on an event scope, so the honest present-day crossover is
# nearer 13 than 9. Keeping 8 anyway: the per-item cost varies fourfold with
# which optional lines an item carries (97-389 chars across those same
# deliveries), the item template is user-editable, and being early with a second
# message costs nothing while being late costs the whole delivery.
#
# No delivery that large exists yet: the biggest real one is 5 items / 2420
# chars, because volume scopes could not alert at all (see
# ``signals._emission_lag``). The replay that unblocks them rendered 14 items /
# 4154 chars, which is when this ceiling starts to matter.
#
# This bounds the ITEM COUNT, not characters: the item template is user-editable
# and the AI explanation is generated after dispatch, so a chunk this size can
# still overshoot. The hard 4096 ceiling is enforced where the finished message
# exists — ``alerts_messages.split_telegram_messages`` measures each one
# assembled, in Telegram's UTF-16 units, and sends a chunk as several messages
# when it has to. This estimate stays anyway: it costs nothing and keeps the
# common case to one message per delivery. Only Telegram is capped — Slack,
# email and webhook have no comparable limit, and chunking jira/linear would file
# duplicate issues.
_MAX_ITEMS_PER_DELIVERY: dict[str, int] = {AlertDestinationType.telegram.value: 8}


def _delivery_chunks(
    anomalies: list[AlertMatchCandidate],
    *,
    channel: str,
    chunk_items: bool = True,
) -> list[list[AlertMatchCandidate]]:
    """Split one rule's matches into deliveries the channel can actually carry.

    Chunking rather than truncating: every chunk is its own AlertDelivery with
    its own items, so no scope is silently dropped and each chunk stamps
    ``last_notified_at`` on the states it covers once it lands.

    ``chunk_items=False`` for a DIGEST. The item cap is a character-ceiling
    estimate, and a digest's compact line is roughly a seventh of a verbose
    one — so the estimate that was right for the immediate path splits a
    perfectly deliverable digest into three messages. ``split_telegram_messages``
    still enforces the real 4096 ceiling at send time, measured after entity
    parsing, and it drops nothing.

    Passed by the CALLER rather than read off ``destination.delivery_schedule_cron``
    on purpose: the flush's drain arm ships a genuine digest from a destination
    whose cadence has already been cleared, so the column would misclassify it.
    """
    if not chunk_items:
        return [anomalies]
    limit = _MAX_ITEMS_PER_DELIVERY.get(str(channel))
    if limit is None or len(anomalies) <= limit:
        return [anomalies]
    return [anomalies[start : start + limit] for start in range(0, len(anomalies), limit)]


def _retire_config_anchored_metric_states(
    session: Session,
    destinations: list[AlertDestination],
) -> None:
    """Delete metric states an OLD worker anchored on a scan config.

    Nothing in this tree writes one: ``_scope_partition_id`` answers NULL for a
    ``metric`` scope, so every path that creates a state stores NULL. Such a row
    can only arrive during a rolling deploy. ``migrate`` is a one-shot that app,
    celery-worker and celery-beat merely WAIT on, so the previous release's worker
    is still collecting while c9e2a71b4d38 commits, and one more collection under
    the old code inserts a state anchored on ``min(config id)`` — legal, because
    the partial unique index only covers the NULL space. The migration cannot
    clear a row created after it ran.

    That row is in NEITHER partition ``_prepare_alert_deliveries`` loads, so no
    write path can reach it: it is never matched, never closed, and
    ``_stamp_rule_state`` filters it out too. The READ paths have no scope or
    config predicate at all — ``_alerting_monitors`` and ``project_service`` load
    every state of a rule — so ``summarize_monitor_states`` counts it as one
    permanently active scope and the monitor never returns to "healthy" again.
    That is the rot the reset migration exists to clear, re-created after its migration.

    DELETED rather than closed, for the reason ``collapse_metric_rule_states``
    gives for dropping these rows instead of merging them: they are permanently
    ``is_active=True`` with a frozen bucket, and a closed one would still sit in
    the rollup as a second copy of a scope whose real row is the project-global
    one beside it. Nothing is lost with it — the pre-fix ``_stamp_rule_state``
    dropped the config filter for a metric scope, so any notification stamp this
    row carries is on the project-global row as well, and that row is reconciled
    by the ordinary open/close pass either way. No send decision can change:
    every path that takes one already cannot see this row.

    Scoped to the rules this run dispatches through, which is not a gap:
    disabling or deleting a rule or its destination deletes every state of the
    rule (``_alerting_destinations.clear_rule_states``), and deleting the rule
    itself cascades them.
    """
    rule_ids = [
        rule.id for destination in destinations for rule in destination.rules if rule.enabled
    ]
    if not rule_ids:
        return
    session.execute(
        delete(AlertRuleState)
        .where(
            AlertRuleState.rule_id.in_(rule_ids),
            AlertRuleState.scope_type == SCOPE_METRIC,
            AlertRuleState.scan_config_id.is_not(None),
        )
        .execution_options(synchronize_session=False)
    )


# The four columns of ``uq_alert_rule_state_scope``, and the same key MINUS the
# config for the partial ``uq_alert_rule_state_metric_scope``. Spelled out
# because the claim below names them as conflict targets while the model names
# them as the constraint and the index — they have to stay the same two lists,
# exactly as ``_PENDING_ITEM_*_CONFLICT_KEYS`` do for the buffer one table over.
_RULE_STATE_CONFLICT_KEYS = ("rule_id", "scan_config_id", "scope_type", "scope_ref")
_RULE_STATE_METRIC_CONFLICT_KEYS = ("rule_id", "scope_type", "scope_ref")


def _claim_rule_state(
    session: Session,
    *,
    rule_id: uuid.UUID,
    scan_config_id: uuid.UUID | None,
    scope_type: str,
    scope_ref: str,
    now: datetime,
    bucket: datetime,
) -> tuple[AlertRuleState | None, bool]:
    """Open a scope's state row, and say whether THIS run is the one that opened it.

    ``created`` is the send claim. Exactly one of the runs racing for a
    ``(rule, partition, scope)`` can insert it, and that run is the one that
    notifies; a run that loses adopts the row already there and stays quiet for
    this collection instead of shipping the peer's alert a second time.

    The load in ``_prepare_alert_deliveries`` is NOT an existence check that can
    be trusted to still hold at the insert. ``check_metrics_due`` dispatches
    ``collect_metrics.delay(config_id, job_id)`` per CONFIG, so two configs of
    one project collect in parallel workers, and a ``metric`` scope's state is
    shared by all of them (it stores the project-global NULL partition — see
    ``_scope_partition_id``). Both runs' loads can miss the row and both then
    insert it. The config-partitioned scopes race the same way whenever a manual
    replay overlaps the scheduled collection of the same config, which is the
    concurrency ``detect.py`` already upserts its anomaly rows against.

    Before ``uq_alert_rule_state_metric_scope`` existed the loser merely wrote a
    SECOND project-global row — one cooldown clock per config, the duplicate the
    index was added to forbid. With the index the loser's INSERT raises
    IntegrityError instead, and nothing wraps ``_prepare_alert_deliveries`` in a
    savepoint, so that failure takes the whole ``collect_metrics`` run down with
    it: the anomaly recalculation, the cooldown updates and every delivery for
    every scope of that scan, on every collection for as long as the scope keeps
    firing. Same blast radius as the oversized ``scope_name``,
    reached from a different direction.

    INSERT ... ON CONFLICT rather than a SAVEPOINT and a re-select, which is the
    shape ``_buffer_pending_items`` and ``detect.py`` already use, and the choice
    is not only taste: a statement that converges raises nothing, so there is no
    ``except`` here for someone else's failure to land in.
    ``_event_identity.insert_event_claiming_identity`` has to flush the caller's
    pending work ABOVE its ``try`` precisely because ``begin_nested()`` performs
    that flush itself, at ROOT level, and a caller's own failing row would
    otherwise be read as this function's collision.

    DO NOTHING rather than DO UPDATE, unlike the buffer's upsert: the row that is
    already there is the authority, and what it holds is the cooldown clock
    (``opened_at``, ``last_notified_at``, ``last_notified_delivery_id``). An
    update would stamp this run's fresh ``now`` over the timer the shared row
    exists to keep, which is that reset wearing another hat.

    WHICH unique index this conflicts against depends on the partition, the same
    branch ``_buffer_pending_items`` makes for the buffer. A NULL config escapes
    the four-column constraint entirely (SQL treats NULLs as DISTINCT), so the
    metric arm names the partial index and has to repeat its predicate through
    ``index_where``. Both arms compile identically on SQLite and PostgreSQL — the
    dialect only picks the ``insert()`` construct.

    On PostgreSQL the peer may still be UNCOMMITTED when this runs: ON CONFLICT
    waits for it to commit or abort, so the re-read below sees the winner's row
    either way, and if the peer aborted this INSERT lands and ``created`` is
    True. The re-read is a second statement on purpose — under READ COMMITTED it
    takes a fresh snapshot, which is what lets it see a row this transaction's
    earlier load could not.

    Returns ``(None, False)`` when the holder we lost to is gone by the time of
    the re-read — ``_alerting_destinations.clear_rule_states`` wipes a rule's
    states when its destination or the rule itself is disabled. There is nothing
    to converge on and nothing worth inserting for a rule that is going away, so
    the caller skips the scope for this collection.
    """
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    insert = sqlite_insert if dialect == "sqlite" else postgresql_insert
    statement = insert(AlertRuleState).values(
        # Spelled out for symmetry with this package's other Core inserts;
        # UUIDMixin's ``default=uuid.uuid4`` would render it anyway. The
        # timestamp columns are left out because theirs are SERVER defaults,
        # which a Core INSERT does not bypass.
        id=uuid.uuid4(),
        rule_id=rule_id,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        is_active=True,
        opened_at=now,
        closed_at=None,
        last_anomaly_bucket=bucket,
    )
    if scan_config_id is None:
        claim = statement.on_conflict_do_nothing(
            index_elements=list(_RULE_STATE_METRIC_CONFLICT_KEYS),
            index_where=AlertRuleState.scan_config_id.is_(None),
        )
    else:
        claim = statement.on_conflict_do_nothing(
            index_elements=list(_RULE_STATE_CONFLICT_KEYS),
        )
    # RETURNING yields the id only when the row was actually inserted — DO
    # NOTHING inserts nothing and returns nothing — so this is the claim itself
    # and not an inference from what the re-read finds.
    created = session.execute(claim.returning(AlertRuleState.id)).scalar_one_or_none() is not None
    state = session.execute(
        select(AlertRuleState).where(
            AlertRuleState.rule_id == rule_id,
            # Spelled as IS NULL rather than left to ``== None`` to render
            # itself, the same care ``_buffer_pending_items`` takes with its
            # fallback read: a silently wrong answer here is a state row the run
            # cannot see and a notification nobody gets.
            AlertRuleState.scan_config_id.is_(None)
            if scan_config_id is None
            else AlertRuleState.scan_config_id == scan_config_id,
            AlertRuleState.scope_type == scope_type,
            AlertRuleState.scope_ref == scope_ref,
        )
    ).scalar_one_or_none()
    return state, created


def _prepare_alert_deliveries(
    session: Session,
    config: ScanConfig,
    *,
    scan_job_id: uuid.UUID | None,
    buffered: list[int] | None = None,
) -> list[uuid.UUID]:
    """Mint deliveries for immediate destinations, buffer for scheduled ones.

    ``buffered`` is an out-parameter rather than a second return value so every
    existing call site keeps working unchanged: it appends the number of alerts
    held for a later digest, which is otherwise invisible from outside the
    database. ``alerts_queued == 0`` alone cannot distinguish
    "held 12" from "nothing matched", and on a cadence that is the difference
    between working and silently swallowing every alert.
    """
    active_candidates: dict[tuple[str, str], AlertMatchCandidate] = {}
    active_candidates.update(_get_latest_active_anomalies(session, config))
    active_candidates.update(_get_active_metric_anomaly_candidates(session, config))
    active_candidates.update(_get_active_schema_drift_candidates(session, config))
    active_candidates.update(_get_active_distribution_drift_candidates(session, config))
    active_candidates.update(_get_active_release_regression_candidates(session, config))
    active_candidates.update(_get_active_variable_value_drift_candidates(session, config))
    # Property drift (F23, #306): one candidate per active row this config
    # detected, gated by ``include_property_drifts``.
    active_candidates.update(_get_active_property_drift_candidates(session, config))
    # "Data is late" (issue #269): at most one candidate per scan config, gated
    # by ``include_source_freshness`` in ``rule_matches_anomaly``.
    active_candidates.update(_get_source_freshness_candidates(session, config))
    # Event lifecycle findings (GH #258): one candidate per open finding, gated
    # by ``include_lifecycle``. Every config emits them; their states live in
    # the project-global partition, so a multi-scan project alerts once.
    active_candidates.update(_get_lifecycle_candidates(session, config))
    destinations = _load_enabled_alert_destinations(session, config.project_id)
    if not destinations:
        return []

    # Before the state loads below, because what it removes is exactly what
    # neither of them can see — and one statement for the whole run, not one per
    # rule, since it finds nothing on every collection but the first after a
    # deploy that raced.
    _retire_config_anchored_metric_states(session, destinations)

    now = datetime.now(UTC)
    project_slug = _get_project_slug(session, config.project_id)
    scope_names = _build_alert_scope_names(session, list(active_candidates.values()))
    # Event-anchored candidates store a NULL event_type_id on purpose; without
    # this map an ``event_type`` filter is silently inert for every one of them.
    event_type_by_event_id = _build_event_type_by_event_id(
        session, list(active_candidates.values())
    )
    delivery_ids: list[uuid.UUID] = []
    buffered_count = 0
    suppressed_group_ids = _suppressed_correlation_group_ids(
        session,
        project_id=config.project_id,
    )

    for destination in destinations:
        enabled_rules = [rule for rule in destination.rules if rule.enabled]
        if not enabled_rules:
            continue

        # A destination on a delivery cadence holds its alerts in
        # ``alert_pending_items`` until ``flush_due_alert_digests`` mints the
        # digest, and the cadence IS that destination's rate limiter. The
        # rule's ``cooldown_minutes`` is therefore NOT applied a second time
        # here.
        #
        # Applying both puts two limiters of comparable period in series, and
        # the default makes them the SAME period: ``cooldown_minutes`` defaults
        # to 1440 and "daily at 09:00" is the cadence people ask for. The
        # cooldown clock starts when the digest is SENT, a hair after the fire
        # instant, so the next day's collections are all a few seconds short of
        # elapsed and buffer nothing — every other digest arrives empty and the
        # real delivery rate halves. Letting the cadence own the rate is both
        # simpler and what a digest means: here is what is wrong right now.
        #
        # The freshness half of the gate is untouched, so a scope that produced
        # no new bucket is still not re-reported, and the buffer's unique key
        # collapses a scope that re-fires all day into ONE digest line per
        # DIRECTION — a scope that flips ships one line per incident, which is
        # what ``_buffer_pending_items`` explains at length.
        cooldown_applies = destination.delivery_schedule_cron is None

        for rule in enabled_rules:
            # Two loads because there are two partitions. Most scopes are
            # config-partitioned, so this run sees only its own config's states;
            # ``metric`` and ``lifecycle`` scopes are project-global
            # (``PROJECT_GLOBAL_SCOPE_TYPES``) and carry NO scan config at all,
            # so every config's run converges on the one row below and shares
            # its cooldown clock.
            #
            # The scope guard on the first load is what keeps the two disjoint,
            # and the ``== config.id`` equality would exclude a NULL anyway —
            # both belts stay on purpose.
            #
            # A row in NEITHER partition — ``metric`` scope WITH a scan config,
            # which only a pre-0zpq.28 worker writes — is unreachable from here
            # by design. Relaxing either filter would NOT retire it: the second
            # load overwrites the first's entry under the same
            # ``(scope_type, scope_ref)`` key, so the close loop would still
            # never see it. It is deleted before this loop instead, by
            # ``_retire_config_anchored_metric_states``.
            #
            # What these two loads are NOT is a race-safe existence check. A
            # miss here only decides which branch the send gate takes; the row
            # is created by a converging INSERT further down
            # (``_claim_rule_state``), because a sibling config's worker can
            # open the same project-global metric state between this load and
            # that insert.
            existing_states = {
                (state.scope_type, state.scope_ref): state
                for state in session.execute(
                    select(AlertRuleState).where(
                        AlertRuleState.rule_id == rule.id,
                        AlertRuleState.scan_config_id == config.id,
                        AlertRuleState.scope_type.not_in(PROJECT_GLOBAL_SCOPE_TYPES),
                    )
                ).scalars()
            }
            for state in session.execute(
                select(AlertRuleState).where(
                    AlertRuleState.rule_id == rule.id,
                    AlertRuleState.scan_config_id.is_(None),
                    AlertRuleState.scope_type.in_(PROJECT_GLOBAL_SCOPE_TYPES),
                )
            ).scalars():
                existing_states[(state.scope_type, state.scope_ref)] = state

            matched_anomalies = [
                candidate
                for candidate in active_candidates.values()
                if _rule_matches_anomaly(
                    rule, candidate, event_type_by_event_id=event_type_by_event_id
                )
            ]
            matched_keys = {
                (anomaly.scope_type, anomaly.scope_ref) for anomaly in matched_anomalies
            }

            just_closed: list[tuple[str, str]] = []
            for key, existing_state in existing_states.items():
                if existing_state.is_active and key not in matched_keys:
                    existing_state.is_active = False
                    existing_state.closed_at = now
                    just_closed.append(key)
            # The incidents this run ENDED — the transition, not every closed
            # state, so a scope that stays quiet is resolved once and not on
            # every collection after. A state carries no direction, so both
            # directions' handles are offered; only the ones that actually
            # paged PagerDuty are acted on, after this run commits.
            if just_closed:
                queue_pagerduty_resolves(
                    session,
                    config.project_id,
                    [
                        _correlation_group_id(
                            scan_config_id=_scope_partition_id(scope_type, config_id=config.id),
                            rule_id=rule.id,
                            scope_type=scope_type,
                            scope_ref=scope_ref,
                            direction=direction.value,
                        )
                        for scope_type, scope_ref in just_closed
                        for direction in AnomalyDirection
                    ],
                )
            # A scope's incident is over once that scope stops firing. Clear its
            # inbox decision now so the NEXT incident on it is not silenced by a
            # stale acknowledge — suppression would otherwise be permanent.
            # Every closed scope, every run, so this stays idempotent.
            #
            # ``key not in matched_keys`` carries the whole safety of this. A
            # state is still closed at this point when the scope's previous
            # anomaly aged out; the loop below is what reopens it. Reading the
            # flag alone would therefore clear the acknowledgement of a scope
            # that is firing RIGHT NOW — and that is the common case, not the
            # rare one, since a scope closes and re-enters on nearly every
            # collection (see the reactivation branch: 93% of sends arrive
            # through it). The rule-wide predicate this replaced was accidentally
            # safe here, because any other live scope of the rule vetoed the
            # reset; per-scope keys removed that veto and have to say it outright.
            closed_keys = [
                key
                for key, state in existing_states.items()
                if not state.is_active and key not in matched_keys
            ]
            if closed_keys:
                # TWO calls, because there are two partitions — the same split
                # the state loads above make, and for the same reason. A metric
                # scope's incident hashes NO config, so rebuilding its ids under
                # ``config.id`` would match nothing and an acknowledged
                # catalog-metric incident would stay suppressed forever, its
                # scope unable to alert again. Each call returns immediately when
                # its side of the split is empty; a key is ``(scope_type,
                # scope_ref)``.
                _reopen_closed_incidents(
                    session,
                    project_id=config.project_id,
                    scan_config_id=None,
                    rule_id=rule.id,
                    scope_keys=[key for key in closed_keys if is_project_global_scope(key[0])],
                )
                _reopen_closed_incidents(
                    session,
                    project_id=config.project_id,
                    scan_config_id=config.id,
                    rule_id=rule.id,
                    scope_keys=[key for key in closed_keys if not is_project_global_scope(key[0])],
                )

            anomalies_to_send: list[AlertMatchCandidate] = []
            for anomaly in matched_anomalies:
                key = (anomaly.scope_type, anomaly.scope_ref)
                current_state = existing_states.get(key)
                should_send = False
                if current_state is None:
                    # Store what the row IS: a metric scope belongs to the
                    # project, not to the config that happened to observe it.
                    # Through the shared helper, so what this row STORES and what
                    # the incident handle below HASHES cannot drift apart.
                    state_config_id = _scope_partition_id(anomaly.scope_type, config_id=config.id)
                    # The INSERT is the send claim, not this branch. "The load
                    # found nothing" is only true as of the load: a parallel
                    # worker collecting a SIBLING config of this project shares
                    # the state row of a metric scope, and it can open the
                    # incident between that load and this line
                    # (``_claim_rule_state``). Whoever's INSERT actually lands
                    # notifies; the other converges on that row and stays quiet
                    # here, which is not a swallowed alert — ``last_notified_at``
                    # is stamped only on a SUCCESSFUL send, so a peer whose
                    # delivery never lands leaves the NULL that makes the next
                    # collection deliver through the branch below.
                    current_state, created = _claim_rule_state(
                        session,
                        rule_id=rule.id,
                        scan_config_id=state_config_id,
                        scope_type=anomaly.scope_type,
                        scope_ref=anomaly.scope_ref,
                        now=now,
                        bucket=anomaly.bucket,
                    )
                    if current_state is None:
                        # Lost the race to a holder that was deleted before we
                        # could read it — the rule or its destination is being
                        # disabled underneath this run. Leave the key out of
                        # ``existing_states`` so a later anomaly on this scope
                        # tries again.
                        continue
                    existing_states[key] = current_state
                    if created:
                        should_send = True
                    else:
                        # Converged. Advance the bucket anyway so this run's
                        # observation is not lost, through the same ``max`` the
                        # gate below uses — the re-read saw the winner's
                        # committed row, so this can only move forwards.
                        current_state.last_anomaly_bucket = _latest_bucket(
                            anomaly.bucket, current_state.last_anomaly_bucket
                        )
                else:
                    if not current_state.is_active:
                        # Reopen the state either way: open/close tracking has to
                        # stay accurate even when the cooldown swallows the
                        # notification, or the scope reads as quiet on the UI.
                        current_state.is_active = True
                        current_state.opened_at = now
                        current_state.closed_at = None
                        # Gated on elapsed time, not on the flag alone. A volume
                        # scope is a candidate for a bounded run of collections
                        # per anomaly bucket, then closes, and its next anomaly
                        # re-enters here — so an ungated reactivation IS the
                        # normal path, not the rare one: over a 24h replay of
                        # live data 406 of 436 sends (93%) came through here, 30
                        # were first-ever scope state and ZERO reached the
                        # cooldown branch below. Raising cooldown_minutes from
                        # 360 to 1440 moved zero deliveries and zero items,
                        # because nothing consulted it. Elapsed time still lets
                        # the case this branch exists for through: a scope that
                        # closed and reopens long after keeps alerting.
                        should_send = not cooldown_applies or _cooldown_elapsed(
                            current_state.last_notified_at,
                            now=now,
                            cooldown_minutes=rule.cooldown_minutes,
                        )
                    elif current_state.last_notified_at is None or (
                        (_bucket_is_newer(anomaly.bucket, current_state.last_anomaly_bucket))
                        and (
                            not cooldown_applies
                            or _cooldown_elapsed(
                                current_state.last_notified_at,
                                now=now,
                                cooldown_minutes=rule.cooldown_minutes,
                            )
                        )
                    ):
                        should_send = True
                    current_state.last_anomaly_bucket = _latest_bucket(
                        anomaly.bucket, current_state.last_anomaly_bucket
                    )
                if should_send:
                    anomalies_to_send.append(anomaly)

            if not anomalies_to_send:
                continue

            # A muted monitor delivers nothing. The rule states above are still
            # updated first, deliberately: open/close tracking has to stay
            # accurate through the mute so the monitor is not stuck "firing" on
            # a stale scope once the mute lapses.
            #
            # HISTORY, not a description of the tree as it stands: the model
            # comment on ``AlertRule.muted_until`` USED TO call worker-side
            # suppression "a separate follow-up", so the Monitors UI shipped a
            # Mute button that wrote a column no worker read and changed
            # nothing. That comment has since been corrected to
            # name the column's only two worker readers — this line and
            # ``alert_flush._build_digest``. A third delivery
            # path added without a mute check of its own would be that bug
            # again.
            #
            # ``is not None and > now`` is correct HERE and must stay: a NULL on
            # an AlertRule means NOT MUTED (it is the default on every rule ever
            # created, and the rule has no status column to say otherwise). The
            # near-identical line in ``_reopen_closed_incidents`` reads a NULL
            # the OPPOSITE way — there it is the indefinite inbox mute — so do
            # not unify them. A rule's permanent lever is
            # ``enabled``.
            rule_muted_until = _as_utc(rule.muted_until)
            if rule_muted_until is not None and rule_muted_until > now:
                continue

            # EVERY item gets a correlation_group_id, not just co-fired ones.
            # The id doubles as the inbox handle, and the inbox only lists items
            # that have one — so while it was reserved for 2+ peers, a solitary
            # alert never reached the inbox and no action could reach it either.
            # That is the common case, and it was unactionable.
            #
            # The id is per SCOPE now (see ``_correlation_group_id``), so peers
            # inside one group are the same scope over time, not the scopes that
            # fired together. Anything asking "did this co-fire?" has to count the
            # DELIVERY's items — ``alerts_messages._build_ai_explanation`` still
            # counts group members and now always sees one.
            #
            # It hashes the PARTITION the scope's rows store, not the config
            # collecting right now (``_scope_partition_id``), so a project-global
            # metric scope lands on the one handle its state row and its buffered
            # row already use. While it hashed the firing config, each scan of a
            # multi-scan project minted its own handle for one project-wide
            # scope: the suppression check below — the whole mechanism behind an
            # Inbox ack or mute — then missed every handle but the one belonging
            # to the scan that happened to collect next, so the silenced incident
            # paged anyway and the Inbox listed it N times.
            correlation_by_anomaly: dict[int, uuid.UUID] = {}
            for anomaly in anomalies_to_send:
                correlation_by_anomaly[id(anomaly)] = _correlation_group_id(
                    scan_config_id=_scope_partition_id(anomaly.scope_type, config_id=config.id),
                    rule_id=rule.id,
                    scope_type=anomaly.scope_type,
                    scope_ref=anomaly.scope_ref,
                    direction=anomaly.direction,
                )

            if suppressed_group_ids:
                anomalies_to_send = [
                    anomaly
                    for anomaly in anomalies_to_send
                    if correlation_by_anomaly.get(id(anomaly)) not in suppressed_group_ids
                ]
            if not anomalies_to_send:
                continue

            if destination.delivery_schedule_cron is None:
                delivery_ids.extend(
                    _create_deliveries(
                        session,
                        config,
                        project_slug=project_slug,
                        rule=rule,
                        destination=destination,
                        anomalies=anomalies_to_send,
                        scope_names=scope_names,
                        correlation_by_anomaly=correlation_by_anomaly,
                        scan_job_id=scan_job_id,
                    )
                )
            else:
                # Held for this destination's next digest window. Nothing is
                # dispatched now and no AlertDelivery exists yet, so the
                # stranded-delivery reaper has nothing to sweep and the Inbox,
                # the delivery history and their created_at orderings are
                # untouched until the digest is actually minted.
                #
                # Held against a window that may never come. If the operator
                # switches this destination back to "Immediately", the service
                # SPLITS these rows rather than leaving all of them for the
                # flusher (``_alerting_destinations.update_destination``). One
                # whose scope has a NULL ``last_notified_at`` is DISCARDED,
                # because the branch above then delivers that scope on the next
                # collection — the gate fires on the NULL — and being the only
                # path that delivers it is what keeps the operator from
                # receiving it twice. One whose scope a digest already stamped
                # is KEPT for the flusher's drain arm, because the gate above
                # needs a newer bucket AND an elapsed cooldown for that scope
                # and would deliver nothing at all.
                buffered_count += _buffer_pending_items(
                    session,
                    config,
                    rule=rule,
                    destination=destination,
                    anomalies=anomalies_to_send,
                    scope_names=scope_names,
                    correlation_by_anomaly=correlation_by_anomaly,
                    scan_job_id=scan_job_id,
                    now=now,
                )

    if buffered is not None:
        buffered.append(buffered_count)
    return delivery_ids


def _create_deliveries(
    session: Session,
    config: ScanConfig,
    *,
    project_slug: str,
    rule: AlertRule,
    destination: AlertDestination,
    anomalies: list[AlertMatchCandidate],
    scope_names: dict[tuple[str, str], str],
    correlation_by_anomaly: dict[int, uuid.UUID],
    scan_job_id: uuid.UUID | None,
    chunk_items: bool = True,
) -> list[uuid.UUID]:
    """Mint the AlertDelivery + AlertDeliveryItem rows for one (rule, destination).

    Extracted verbatim from ``_prepare_alert_deliveries`` so the immediate path
    and the scheduled flush (``worker/tasks/alert_flush.py``) mint deliveries
    through ONE code path. That matters more than it looks: this is where the
    three machine-readable encodings of a delivery are born together — the
    ``payload_snapshot`` JSON, the ``AlertDeliveryItem`` rows, and the chunking
    that keeps a Telegram message under its item cap. A second implementation
    for digests would be a second chance for them to disagree.
    """
    # Resolved ONCE here, on the caller's own session, and used for every link
    # of every item of every chunk below.
    #
    # Here rather than in the two callers precisely because of the paragraph
    # above: this is the one place the immediate path and the scheduled flush
    # meet, so one line fixes both and there is no parameter for a third caller
    # to forget.
    #
    # It used to be read inside the URL builders, once per LINK — three reads
    # per item for an ordinary scope, two for a release regression, since
    # ``_build_item_paths`` runs once for the typed rows and again for the
    # snapshot. ``get_runtime_config_sync`` has no cache, and called with no
    # session it checks out a SECOND pooled connection for its two
    # ``app_settings`` SELECTs. On the digest path it did that while
    # ``alert_flush._build_digest`` held FOR UPDATE locks on the whole buffer
    # and the flush advisory lock on the first connection.
    # Passing ``session`` keeps the reads on the connection this transaction
    # already holds, which is how ``worker/tasks/scan.py`` and
    # ``metrics/tasks.py`` already call this helper.
    #
    # One read is the correctness boundary too, not just the cheap one: the
    # helper swallows a failed read and falls back to the env config, where
    # ``app_base_url`` defaults to ``""``. Independent reads therefore let this
    # delivery's typed items and its frozen ``payload_snapshot`` disagree about
    # the same item's link, with nothing but a warning in the log to say why.
    app_base_url = app_settings_service.get_runtime_config_sync(session).app_base_url
    # The organization every link of this delivery names (F20 PR8): a project
    # slug alone is unique only inside its organization. Once, like the base.
    org_slug = _get_org_slug(session, config.project_id)
    # One read for every chunk, before any delivery row is added (GH #255).
    attribution_lines = _build_attribution_lines(session, config, list(anomalies))
    delivery_ids: list[uuid.UUID] = []
    for chunk in _delivery_chunks(anomalies, channel=destination.type, chunk_items=chunk_items):
        delivery = AlertDelivery(
            project_id=config.project_id,
            scan_config_id=config.id,
            scan_job_id=scan_job_id,
            destination_id=destination.id,
            rule_id=rule.id,
            status=AlertDeliveryStatus.pending.value,
            channel=destination.type,
            matched_count=len(chunk),
            payload_snapshot=None,
        )
        session.add(delivery)
        session.flush()
        # The snapshot is built AFTER the flush because it now contains
        # links back to this delivery's own audit row, and those need
        # the id. Whole-object assignment (not in-place mutation) so
        # SQLAlchemy sees the JSON column change.
        snapshot = _build_delivery_snapshot(
            config,
            project_slug=project_slug,
            app_base_url=app_base_url,
            org_slug=org_slug,
            rule=rule,
            destination=destination,
            anomalies=chunk,
            scope_names=scope_names,
            delivery_id=delivery.id,
            attribution_lines=attribution_lines,
        )
        if not chunk_items:
            # The send path has no other way to tell a digest from an immediate
            # alert: the destination's cadence column cannot answer it (the
            # flush's drain arm ships a real digest from a destination whose
            # cadence is already gone), and by send time the buffer rows are
            # gone. Recorded at creation, where the caller's intent is known.
            snapshot["digest"] = True
        delivery.payload_snapshot = snapshot

        for anomaly in chunk:
            absolute_delta = abs(anomaly.actual_count - anomaly.expected_count)
            # 0.0 at a zero baseline is a PLACEHOLDER, not a measurement:
            # the ratio is undefined and the column is NOT NULL. Nothing
            # may emit it as it stands. Readers go through one of two
            # encodings of the same gate: humans get the words via
            # ``alert_templates.format_percent_delta`` (the message's
            # ${percent_delta_label}, the AI prompt) or the frontend's
            # ``lib/percentDelta`` mirror; machines get JSON ``null`` via
            # ``alert_templates.percent_delta_or_none`` (the generic
            # webhook body, ``payload_snapshot``). The percent gate admits
            # the class on purpose; printing the
            # placeholder reported the largest possible relative move as
            # the smallest.
            # The one deliberate exception is the raw ${percent_delta}
            # template variable, whose documented contract is a bare
            # number; see ``alerts_messages._build_item_template_context``.
            #
            # ZERO is the placeholder condition, not "not positive". A
            # signed catalog metric has a real baseline at -100 and a real
            # 200% move to -300, and the matcher already reads it that way
            # (``alerting_matching.rule_matches_anomaly``: ``abs(expected)``
            # against min_expected_count, ``absolute_delta / abs(expected)``
            # against min_percent_delta) — so a rule fires
            # BECAUSE the move is 200% and storing 0.0 for it reproduced
            # exactly the earlier misreport against a real baseline.
            # The divisor is the MAGNITUDE so the ratio stays a size rather
            # than flipping sign with the level; direction is carried by
            # ``direction``/``actual_count`` and never by this field.
            #
            # Writing the measured number here is what made the readers
            # fixable at all: the column is frozen history and every surface
            # renders it back at read time, so a row stored as 0.0 could never
            # be recovered, while a row stored as 200.0 renders correctly.
            #
            # There is no per-reader copy of this test left to enumerate. The
            # definition is ``alert_templates.has_baseline`` /
            # ``percent_delta_of`` — a leaf module importing only
            # ``tripl.models.*``, so every backend surface can and does route
            # through it: this writer, the audit snapshot
            # (``alert_payload._build_delivery_snapshot``), the message and
            # digest renderers in ``worker/tasks/alerts_messages``,
            # ``services._alerting_deliveries``, ``schemas/alerting``'s response
            # validators, ``services.alerting_service.simulate_rule`` and the
            # demo builder. Only the frontend restates it, in
            # ``lib/percentDelta.hasBaseline``, because it cannot import Python;
            # both sides are pinned against the same grid of baselines
            # (``tests/test_metric_scope_purge.py``, ``lib/percentDelta.test.ts``).
            #
            # Add a reader, route it through the helper — do not re-derive the
            # ratio here or anywhere else.
            percent_delta = percent_delta_of(anomaly.actual_count, anomaly.expected_count)
            details_path, monitoring_path = _build_item_paths(
                project_slug,
                org_slug=org_slug,
                app_base_url=app_base_url,
                scope_type=anomaly.scope_type,
                scope_ref=anomaly.scope_ref,
                event_id=anomaly.event_id,
                delivery_id=delivery.id,
                correlation_group_id=correlation_by_anomaly.get(id(anomaly)),
            )
            session.add(
                AlertDeliveryItem(
                    delivery_id=delivery.id,
                    scope_type=anomaly.scope_type,
                    scope_ref=anomaly.scope_ref,
                    scope_name=scope_names[(anomaly.scope_type, anomaly.scope_ref)],
                    event_type_id=anomaly.event_type_id,
                    event_id=anomaly.event_id,
                    bucket=anomaly.bucket,
                    direction=anomaly.direction,
                    actual_count=anomaly.actual_count,
                    expected_count=anomaly.expected_count,
                    absolute_delta=absolute_delta,
                    percent_delta=percent_delta,
                    details_path=details_path,
                    monitoring_path=monitoring_path,
                    drift_field=getattr(anomaly, "drift_field", None),
                    drift_type=getattr(anomaly, "drift_type", None),
                    sample_value=getattr(anomaly, "sample_value", None),
                    # Only release regressions carry one (see
                    # signals.py). It is snapshotted here rather than
                    # read back at render time because the source rows
                    # are deleted on every recalculation, so an Inbox
                    # retry would otherwise render an unqualified line.
                    window_from=getattr(anomaly, "window_from", None),
                    correlation_group_id=correlation_by_anomaly.get(id(anomaly)),
                )
            )
            item_group_id = correlation_by_anomaly.get(id(anomaly))
            if item_group_id is not None:
                _touch_correlation_state(
                    session,
                    project_id=config.project_id,
                    correlation_group_id=item_group_id,
                    seen_at=anomaly.bucket,
                )
        delivery_ids.append(delivery.id)

    return delivery_ids


# The six columns of ``uq_alert_pending_item_scope``. Spelled out because the
# upsert names them as conflict targets and the model names them as the
# constraint — they have to stay the same list.
_PENDING_ITEM_CONFLICT_KEYS = (
    "destination_id",
    "rule_id",
    "scan_config_id",
    "scope_type",
    "scope_ref",
    "direction",
)

# The five columns of ``uq_alert_pending_item_metric_scope``: the same key minus
# the config, because a ``metric`` scope stores NULL there and SQL treats NULLs
# as DISTINCT. The six-column constraint above can therefore NEVER fire for one,
# so the upsert has to target the partial index instead — the trap ``detect.py``
# documents for MetricAnomaly and solves the same way. Missing this does not
# raise: it buffers a second row on every collection and duplicates the digest
# line.
_PENDING_ITEM_METRIC_CONFLICT_KEYS = (
    "destination_id",
    "rule_id",
    "scope_type",
    "scope_ref",
    "direction",
)


def _buffer_pending_items(
    session: Session,
    config: ScanConfig,
    *,
    rule: AlertRule,
    destination: AlertDestination,
    anomalies: list[AlertMatchCandidate],
    scope_names: dict[tuple[str, str], str],
    correlation_by_anomaly: dict[int, uuid.UUID],
    scan_job_id: uuid.UUID | None,
    now: datetime,
) -> int:
    """Hold matched signals for this destination's next digest window.

    Upsert, not insert: a scope that keeps firing is re-offered on every
    collection, and each one overwrites its buffered row with the newest
    numbers, so a scope firing all day occupies exactly one line — ONE PER
    DIRECTION. The key is ``_correlation_group_id``'s five components plus the
    destination, ``direction`` among them, so one buffered row stands for
    exactly one incident and one Inbox card.

    A scope that DROPPED at 03:00 and SPIKED at 11:00 therefore buffers two
    rows and ships two lines, one in each of the digest's two groups. That is
    the intended reading — two real movements, two incidents, two things an
    operator can acknowledge separately — and it is the common case rather than
    a corner: 106 of 223 live scopes fired in BOTH directions inside one day
    (``models/alert_rule``).

    Collapsing the pair was proposed both ways and neither is available:

    * DROP ``direction`` from the key and one row stands for two incidents
      while ``correlation_group_id`` is a single column that can name only one.
      ``alert_flush._build_digest`` filters on exactly that value, so whichever
      handle survived, an acknowledgement of the drop would either be ignored
      or silently swallow the spike nobody acknowledged.
    * DELETE the sibling on a flip and an incident that was already buffered is
      destroyed before it is ever delivered, which nothing re-offers:
      ``AlertRuleState`` carries no direction, so the flipped scope is already
      reusing that one state row, and ``last_notified_at`` is stamped only on a
      SENT delivery.

    So the digest is every incident that fired inside the window, each with its
    OWN latest numbers — not a snapshot of what is broken at the fire instant.
    Nothing prunes a buffered row when its scope falls quiet either (the only
    deletes are this flush's own claim, the 14-day sweep, the service discarding
    the whole buffer when the destination is disabled, and — when its cadence is
    cleared — the service discarding only the PART of the buffer whose scopes
    the immediate path provably re-delivers, which is that same trap avoided on
    the other side of the handoff), so a scope that fired once at 03:00 and
    stopped still ships that 03:00 line. The flipped drop is no staler than that one: it is its
    incident's last true reading.

    Values are snapshotted rather than referenced. ``_recalculate_*`` deletes
    and rewrites the anomaly rows on every collection, so by flush time the row
    this was built from is gone; ``AlertDeliveryItem.window_from`` carries the
    same warning for the same reason.
    """
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    insert = sqlite_insert if dialect == "sqlite" else postgresql_insert

    for anomaly in anomalies:
        group_id = correlation_by_anomaly[id(anomaly)]
        # Mirror AlertRuleState's key exactly — one helper answers for both, and
        # for the incident handle the caller minted: a metric scope is
        # project-global and carries no config, everything else carries the
        # firing one. Recomputing this at flush time instead would be wrong — the
        # buffered row would then key differently from the rule state that gates
        # it.
        scan_config_id = _scope_partition_id(anomaly.scope_type, config_id=config.id)
        statement = insert(AlertPendingItem).values(
            # Redundant, and kept only for symmetry with this package's other
            # Core upserts — detect.py, metric_rows.py and schema_drift.py all
            # spell out an ``id`` too. UUIDMixin's ``default=uuid.uuid4`` is an
            # ordinary Core ``Column.default``, which SQLAlchemy renders into
            # the INSERT by itself when the column is left out of ``values()``.
            # Do not generalise that to the ``onupdate`` note on the SET clause
            # below: a column default and an ``onupdate`` are different
            # mechanisms, and only the first survives a Core upsert.
            id=uuid.uuid4(),
            project_id=config.project_id,
            destination_id=destination.id,
            rule_id=rule.id,
            scan_config_id=scan_config_id,
            scan_job_id=scan_job_id,
            source_anomaly_id=getattr(anomaly, "id", None),
            scope_type=anomaly.scope_type,
            scope_ref=anomaly.scope_ref,
            scope_name=scope_names[(anomaly.scope_type, anomaly.scope_ref)],
            event_type_id=anomaly.event_type_id,
            event_id=anomaly.event_id,
            bucket=anomaly.bucket,
            direction=anomaly.direction,
            actual_count=anomaly.actual_count,
            expected_count=anomaly.expected_count,
            drift_field=getattr(anomaly, "drift_field", None),
            drift_type=getattr(anomaly, "drift_type", None),
            sample_value=getattr(anomaly, "sample_value", None),
            window_from=getattr(anomaly, "window_from", None),
            correlation_group_id=group_id,
            observation_count=1,
        )
        update_values = {
            "scan_job_id": statement.excluded.scan_job_id,
            "source_anomaly_id": statement.excluded.source_anomaly_id,
            "scope_name": statement.excluded.scope_name,
            "event_type_id": statement.excluded.event_type_id,
            "event_id": statement.excluded.event_id,
            "bucket": statement.excluded.bucket,
            "actual_count": statement.excluded.actual_count,
            "expected_count": statement.excluded.expected_count,
            "drift_field": statement.excluded.drift_field,
            "drift_type": statement.excluded.drift_type,
            "sample_value": statement.excluded.sample_value,
            "window_from": statement.excluded.window_from,
            # A Core upsert bypasses SQLAlchemy's ``onupdate``, so the age
            # sweep's column is advanced by hand.
            "updated_at": now,
            "observation_count": AlertPendingItem.observation_count + 1,
        }
        # Never let a late collection of an OLDER bucket rewind the numbers a
        # newer one already wrote — the same stance ``last_anomaly_bucket =
        # max(...)`` takes in the send gate.
        bucket_not_rewound = AlertPendingItem.bucket <= statement.excluded.bucket
        # WHICH unique index this conflicts against depends on what the row
        # stores, the same branch ``detect.py`` makes for MetricAnomaly. A NULL
        # config escapes the six-column constraint entirely (SQL treats NULLs as
        # DISTINCT), so a metric scope names the partial index instead and has
        # to repeat its predicate through ``index_where``. No second
        # sqlite/postgres arm is needed the way detect.py has one: the dialect
        # was already chosen above and this call compiles identically on both.
        if scan_config_id is None:
            upsert = statement.on_conflict_do_update(
                index_elements=list(_PENDING_ITEM_METRIC_CONFLICT_KEYS),
                index_where=AlertPendingItem.scan_config_id.is_(None),
                set_=update_values,
                where=bucket_not_rewound,
            )
        else:
            upsert = statement.on_conflict_do_update(
                index_elements=list(_PENDING_ITEM_CONFLICT_KEYS),
                set_=update_values,
                where=bucket_not_rewound,
            )
        upserted_group_id = session.execute(
            upsert.returning(AlertPendingItem.correlation_group_id)
        ).scalar_one_or_none()
        # RETURNING yields nothing when the ``where`` guard above vetoed the
        # update (a late collection of an older bucket), so fall back to the
        # row that is actually there.
        if upserted_group_id is None:
            upserted_group_id = session.execute(
                select(AlertPendingItem.correlation_group_id).where(
                    AlertPendingItem.destination_id == destination.id,
                    AlertPendingItem.rule_id == rule.id,
                    # Spelled as IS NULL rather than left to ``== None`` to
                    # render itself: this branch only runs on a late-bucket
                    # collection, so a silently wrong answer here would be very
                    # hard to find.
                    AlertPendingItem.scan_config_id.is_(None)
                    if scan_config_id is None
                    else AlertPendingItem.scan_config_id == scan_config_id,
                    AlertPendingItem.scope_type == anomaly.scope_type,
                    AlertPendingItem.scope_ref == anomaly.scope_ref,
                    AlertPendingItem.direction == anomaly.direction,
                )
            ).scalar_one_or_none()

        # Keep the incident's inbox last-seen live while it waits, so an
        # operator can still acknowledge or mute it before the digest ships.
        #
        # Touch the id the ROW ended up carrying, not the one just computed.
        # Now the two AGREE by construction — both hash the
        # partition this row stores — so a second scan collecting the same
        # project-global metric recomputes the handle already buffered instead of
        # minting one of its own, which is what used to leave a stray
        # AlertCorrelationState holding a decision the digest could never honour.
        #
        # The read-back stays anyway, because two cases still hand back a
        # DIFFERENT id: the late-bucket fallback above (whatever an earlier
        # collection stored), and a row buffered by a worker from before that change
        # during a rolling deploy, which carries the old firing-config hash until
        # its digest ships. Touching what the row carries is what keeps the
        # operator's decision attached to the id the digest will actually
        # deliver. Idempotent either way (``last_seen_at = max(...)``).
        if upserted_group_id is not None:
            _touch_correlation_state(
                session,
                project_id=config.project_id,
                correlation_group_id=upserted_group_id,
                seen_at=anomaly.bucket,
            )
    return len(anomalies)
