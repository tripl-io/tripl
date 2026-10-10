from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache, extensions
from tripl.core.plan_policy import PlanPolicyContext, blocking, refusal_detail
from tripl.models.event import Event
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.plan_branch import BranchStatus, PlanBranch
from tripl.models.plan_branch_approval import PlanBranchApproval
from tripl.models.plan_branch_reviewer import PlanBranchReviewer
from tripl.models.plan_revision import PlanRevision, PlanRevisionKind
from tripl.models.project import Project
from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.models.subscription import Subscription
from tripl.models.user import User
from tripl.schemas.plan_branch import PlanBranchDetailResponse
from tripl.services import (
    _plan_branch_merge_photos,
    _plan_branch_merge_variables,
    project_access,
    subscription_service,
)
from tripl.services._branch_counterparts import main_counterparts
from tripl.services._branch_event_threads import move_event_threads
from tripl.services._celery_dispatch import dispatch
from tripl.services._plan_branch_locks import lock_main_plan_for_merge
from tripl.services._plan_branch_merge_events import apply_event_types, apply_events
from tripl.services._plan_branch_merge_fields import (
    apply_field_definitions,
    apply_meta_fields,
    apply_relations,
)
from tripl.services._plan_branch_merge_photos import apply_photos
from tripl.services._plan_branch_merge_state import MergeContext, MergedEvents
from tripl.services._plan_branch_merge_variables import apply_value_overrides, apply_variables
from tripl.services._plan_branch_sides import require_complete_base
from tripl.services.event_photo_service import BlobRef, delete_unreferenced_blobs
from tripl.services.event_type_owner_service import load_owner_user_ids
from tripl.services.plan_branch_conflicts import (
    _ET_CHANGE_KEYS,
    _entity_changed,
    _field_conflicts_event_type,
    _load_resolutions,
    merge_blocking_conflicts,
)
from tripl.services.plan_branch_service import (
    _reject_main,
    _resolve_project,
    _to_detail,
    ensure_main_branch_id,
)
from tripl.services.plan_revision_service import (
    build_plan_snapshot,
    plan_snapshot_hash,
    with_snapshot_defaults,
)
from tripl.services.project_branch_settings_service import read_branch_merge_policy
from tripl.services.project_links import project_link

logger = logging.getLogger(__name__)

# The merge's arms live in ``_plan_branch_merge_*`` now; these names are kept
# importable from here for the code and tests that import them from this module.
_RENAME_STAGING_PREFIX = _plan_branch_merge_variables._RENAME_STAGING_PREFIX
_split_identity_rows = _plan_branch_merge_photos._split_identity_rows
_three_way_count = _plan_branch_merge_photos._three_way_count
rename_variables_with_parking = _plan_branch_merge_variables.rename_variables_with_parking


async def _load_fresh_approver_ids(
    session: AsyncSession,
    *,
    branch_id: uuid.UUID,
    current_plan_hash: str,
) -> tuple[set[uuid.UUID], int]:
    """Distinct users whose approval matches the branch's CURRENT content.

    An approval stamped for earlier content (or a legacy NULL-hash row) is
    stale — the branch changed after the review, so it must not satisfy any
    merge gate. Returns ``(fresh_ids, stale_count)``; approvals
    whose user was deleted (NULL user_id) never count.
    """
    approvals = await session.execute(
        select(PlanBranchApproval.user_id, PlanBranchApproval.plan_hash).where(
            PlanBranchApproval.branch_id == branch_id
        )
    )
    fresh: set[uuid.UUID] = set()
    stale = 0
    for user_id, plan_hash in approvals.all():
        if user_id is None:
            continue
        if plan_hash == current_plan_hash:
            fresh.add(user_id)
        else:
            stale += 1
    return fresh, stale


async def _check_min_approvals(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch: PlanBranch,
    current_plan_hash: str,
) -> None:
    """Block the merge until the project's approval quota is met.

    Counts distinct users with a FRESH approval (content hash matches the
    branch's current plan). The author's own approval is discarded when the
    policy blocks self-approval (defense in depth — the approve transition
    already rejects it, but rows created before the policy flipped on must
    not satisfy the gate either).
    """
    policy = await read_branch_merge_policy(session, project_id)
    if policy.min_approvals <= 0:
        return

    approver_ids, stale_count = await _load_fresh_approver_ids(
        session, branch_id=branch.id, current_plan_hash=current_plan_hash
    )
    if policy.block_self_approval and branch.created_by is not None:
        approver_ids.discard(branch.created_by)

    if len(approver_ids) < policy.min_approvals:
        raise HTTPException(
            status_code=409,
            detail={
                "insufficient_approvals": {
                    "required": policy.min_approvals,
                    "current": len(approver_ids),
                    "stale": stale_count,
                }
            },
        )


async def _event_thread_twins(
    session: AsyncSession, *, project_id: uuid.UUID, branch_id: uuid.UUID
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Branch row → the main twin its discussion reads through to, for every
    branch row that holds a thread of its own (None: no twin yet).

    Read BEFORE the merge writes anything, so the twin is the very one
    ``event_comment_service.event_thread`` anchors new threads on right now —
    through ``main_counterparts``, which pairs by type and scan identity. The
    merge pairs by type and NAME, and the two part ways when main's row for the
    identity carries another display name (an accepted shadow event, say): the
    merge then creates a second main row under the branch's name, and moving
    the old thread onto THAT split the discussion between it and the twin that
    already held every thread started since.

    Bounded by how many branch rows hold a thread of their own, not by the
    size of the catalog every branch copies.
    """
    anchored_ids = set(
        (
            await session.execute(
                select(EventPhotoComment.event_id)
                .join(Event, Event.id == EventPhotoComment.event_id)
                .where(
                    Event.project_id == project_id,
                    Event.branch_id == branch_id,
                    EventPhotoComment.photo_id.is_(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if not anchored_ids:
        return {}
    anchored = (
        (await session.execute(select(Event).where(Event.id.in_(anchored_ids)))).scalars().all()
    )
    twins = await main_counterparts(session, project_id=project_id, events=anchored)
    return {row.id: twins[row.id].id if row.id in twins else None for row in anchored}


async def _move_event_threads_to_main(
    session: AsyncSession,
    *,
    main_event_id_by_branch_event_id: dict[uuid.UUID, uuid.UUID],
) -> None:
    """Hand main the event discussions that hang on the branch's own rows.

    The discussion is not plan content: no snapshot carries it, so no plan
    arm of the merge sees it, and the deep copy leaves it on main's row, where a
    branch copy reads and writes it through its twin. What sits on a BRANCH row
    is only what had no twin to go to — the thread of an event created on the
    branch, the note typed with it at creation included, or one started before
    main grew a row for that identity. The merge used to leave all of it there:
    main's new row opened with an empty thread, the branch row then read through
    to that empty twin, and deleting the merged branch took the rows with it
    through the cascade.

    None of it was ever copied from main, so all of it is the branch's own and
    there is no base to merge three ways against — unlike the photo threads
    (``_plan_branch_merge_photos``). The rows are MOVED, not copied: ids,
    replies and resolution state go unchanged, beside whatever thread the
    target row already had. The target is
    the twin the branch row already reads its discussion through
    (``_event_thread_twins``), where every thread started since that twin
    appeared already hangs; only a row that had none goes to the main row the
    merge lands its key on, created or matched.

    A branch row that lands nowhere — main deleted the event, or its event type,
    after the cut — keeps its thread: main's deletion stands, as it does for the
    photo threads, and this discussion goes with the branch. So does a row with
    no twin whose type and name several events share, on main or on the branch:
    the key cannot say which of them the thread is about, and the caller does
    not guess. An event the merge itself deletes from main takes main's thread
    with it through the same cascade as a delete on main.
    """
    # The UPDATE itself is shared with every door that deletes a branch row
    # which has a main twin, so the two can never disagree about what a moved
    # thread looks like.
    await move_event_threads(session, target_by_event_id=main_event_id_by_branch_event_id)


async def _move_event_subscriptions_to_main(
    session: AsyncSession, *, landed_by_branch_event_id: dict[uuid.UUID, uuid.UUID]
) -> None:
    """Re-key ``event`` subscriptions still held by branch rows onto main (#259).

    Only branch rows somebody actually watches are touched, so this is bounded
    by the branch's subscriptions, not by the catalog the branch copied.
    """
    if not landed_by_branch_event_id:
        return
    watched = set(
        (
            await session.scalars(
                select(Subscription.entity_id)
                .where(
                    Subscription.entity_type == subscription_service.EVENT,
                    Subscription.entity_id.in_(list(landed_by_branch_event_id)),
                )
                .distinct()
            )
        ).all()
    )
    await subscription_service.rekey_event_subscriptions(
        session,
        target_by_event_id={
            event_id: target_id
            for event_id, target_id in landed_by_branch_event_id.items()
            if event_id in watched
        },
    )


async def _hand_over_event_discussions(
    session: AsyncSession,
    *,
    thread_twins: dict[uuid.UUID, uuid.UUID | None],
    events: MergedEvents,
) -> None:
    """The merge's arm for each branch event's own discussion and its watchers.

    Not plan content, so ungated by any diff. A thread goes to the twin the
    branch row reads through today when the merge kept it, else to the main row
    the branch row itself landed on. A row that landed nowhere — main deleted
    it, or it is one of several namesakes nothing tells apart — keeps its
    thread, as one whose event main deleted always has. The landing is the slot
    the attribute writes used, so a thread about one namesake can no longer
    move onto the other.

    It lives here rather than beside the plan arms: what it moves is not plan
    content, and the helpers it calls are this module's.
    """
    main_target_by_branch_id = events.main_target_by_branch_id
    surviving_main_ids = events.surviving_main_ids
    thread_targets: dict[uuid.UUID, uuid.UUID] = {}
    for branch_event_id, twin_id in thread_twins.items():
        landed = main_target_by_branch_id.get(branch_event_id)
        if twin_id is not None and twin_id in surviving_main_ids:
            thread_targets[branch_event_id] = twin_id
        elif landed is not None and landed.id in surviving_main_ids:
            thread_targets[branch_event_id] = landed.id
    await _move_event_threads_to_main(session, main_event_id_by_branch_event_id=thread_targets)
    # Watchers of a branch-only event with no thread of its own (its author
    # and owner, subscribed at creation) follow it to the main row it landed
    # on, as the thread-holding rows' watchers just did (#259).
    await _move_event_subscriptions_to_main(
        session,
        landed_by_branch_event_id={
            branch_event_id: landed.id
            for branch_event_id, landed in main_target_by_branch_id.items()
            if branch_event_id not in thread_targets and landed.id in surviving_main_ids
        },
    )


async def _apply_merge(
    session: AsyncSession,
    project_id: uuid.UUID,
    main_branch_id: uuid.UUID,
    branch_id: uuid.UUID,
    *,
    resolutions: dict[tuple[str, str, str], str] | None = None,
    base_payload: dict[str, Any] | None = None,
) -> frozenset[BlobRef]:
    """Apply the branch's plan onto main with upsert-by-natural-key.

    Matched event_type/event rows are updated in place (id preserved) so
    runtime rows linked by id (metrics, photos, alerts) survive the merge.
    Each entity and child collection is applied only when the branch changed it
    from the recorded base. This preserves one-sided main edits and deletions;
    conflict detection rejects divergent edits before this function runs.

    ``resolutions`` maps (entity_type, name, field) -> "ours" | "theirs" and
    is honored for event_type metadata fields: "ours" keeps main's current
    value for that field instead of taking the branch's. Defaults to "theirs"
    (branch wins) when no resolution is supplied.

    ``MAIN_WINS_FIELDS`` (catalog ``order``) are the exception on every entity:
    where main moved the value off the base too, or both sides added the row,
    main's stays (``main_keeps``); a move on the branch alone still lands.

    Each entity kind is applied by an arm of its own, in a
    ``_plan_branch_merge_*`` module (``_plan_branch_merge_state`` lists them).
    They run in the order below, which is part of the behaviour: each arm reads
    what the ones before it wrote, and what one leaves for the next is passed
    on explicitly.

    Returns the ``(storage_backend, storage_key, storage_config_id)`` of every
    uploaded photo the merge deleted from main, for ``merge_branch`` to release
    once the merge has committed.
    """
    # Read before anything below writes: the twin each thread-holding branch
    # row reads its discussion through today, which the thread move at the end
    # prefers over the row the merge lands its key on.
    thread_twins = await _event_thread_twins(session, project_id=project_id, branch_id=branch_id)
    branch_origins_complete = bool(
        await session.scalar(
            select(PlanBranch.origin_ids_complete).where(PlanBranch.id == branch_id)
        )
    )
    branch_snapshot_payload = await build_plan_snapshot(session, project_id, branch_id=branch_id)
    ctx = MergeContext(
        session=session,
        project_id=project_id,
        main_branch_id=main_branch_id,
        branch_id=branch_id,
        resolutions=resolutions or {},
        base_payload=base_payload or {},
        branch_origins_complete=branch_origins_complete,
        branch_snapshot_payload=branch_snapshot_payload,
    )
    event_types = await apply_event_types(ctx)
    fields = await apply_field_definitions(ctx, event_types)
    meta_fields = await apply_meta_fields(ctx)
    variables = await apply_variables(ctx)
    events = await apply_events(ctx, event_types, fields, meta_fields)
    await apply_photos(ctx, events)
    await _hand_over_event_discussions(session, thread_twins=thread_twins, events=events)
    await apply_value_overrides(ctx, variables, events)
    await apply_relations(ctx, event_types, fields)
    return frozenset(ctx.released_blobs)


def _touched_event_type_names(
    base_payload: dict[str, Any], branch_payload: dict[str, Any]
) -> set[str]:
    """Event-type names whose metadata differs between base and branch.

    Used to decide which event types' owners must approve the merge. Picks up
    additions, removals, and metadata changes (``_ET_CHANGE_KEYS``). Pure
    add/remove of children under an unchanged type does not count for v1 —
    owner gating triggers on type-level edits only.
    """
    base_by_name = {e["name"]: e for e in base_payload.get("event_types", [])}
    branch_by_name = {e["name"]: e for e in branch_payload.get("event_types", [])}
    touched: set[str] = set()
    for name in set(base_by_name) | set(branch_by_name):
        b = base_by_name.get(name)
        n = branch_by_name.get(name)
        if b is None or n is None or _entity_changed(b, n, _ET_CHANGE_KEYS):
            touched.add(name)
    return touched


async def _owners_still_members(
    session: AsyncSession,
    project_id: uuid.UUID,
    owners_by_et: dict[uuid.UUID, set[uuid.UUID]],
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Drop owners who no longer have a role in the project.

    Removing a member deletes their ownership rows, but a row that predates
    that cleanup (or slipped past it) must neither block a merge on an approval
    its holder can no longer give nor put them on the reviewer list of a
    project they cannot see. An event type left with no member owner counts as
    unowned.
    """
    all_owner_ids: set[uuid.UUID] = set()
    for ids in owners_by_et.values():
        all_owner_ids |= ids
    if not all_owner_ids:
        return {}
    members = await project_access.members_among(session, project_id, all_owner_ids)
    kept = {et_id: ids & members for et_id, ids in owners_by_et.items()}
    return {et_id: ids for et_id, ids in kept.items() if ids}


async def _check_owner_approvals(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    main_branch_id: uuid.UUID,
    branch_id: uuid.UUID,
    base_payload: dict[str, Any],
    branch_payload: dict[str, Any],
    current_plan_hash: str,
) -> None:
    """Block the merge when an owned event type is touched without an owner's
    FRESH approval (stale approvals — content edited after review — don't
    count). Owners attach to live main rows only, so unowned event types
    (including freshly added ones that don't exist on main yet) auto-pass."""
    touched = _touched_event_type_names(base_payload, branch_payload)
    if not touched:
        return

    rows = await session.execute(
        select(EventType.id, EventType.name).where(
            EventType.project_id == project_id,
            EventType.branch_id == main_branch_id,
            EventType.name.in_(list(touched)),
        )
    )
    main_name_to_id = {name: et_id for et_id, name in rows.all()}
    if not main_name_to_id:
        return

    owners = await session.execute(
        select(EventTypeOwner.event_type_id, EventTypeOwner.user_id).where(
            EventTypeOwner.event_type_id.in_(list(main_name_to_id.values()))
        )
    )
    owners_by_et: dict[uuid.UUID, set[uuid.UUID]] = {}
    for et_id, user_id in owners.all():
        owners_by_et.setdefault(et_id, set()).add(user_id)
    owners_by_et = await _owners_still_members(session, project_id, owners_by_et)
    if not owners_by_et:
        return

    approver_ids, _stale = await _load_fresh_approver_ids(
        session, branch_id=branch_id, current_plan_hash=current_plan_hash
    )

    missing: list[dict[str, Any]] = []
    for name, et_id in main_name_to_id.items():
        owners_set = owners_by_et.get(et_id)
        if not owners_set:
            continue
        if not (owners_set & approver_ids):
            missing.append(
                {
                    "event_type": name,
                    "owner_user_ids": [str(u) for u in sorted(owners_set, key=str)],
                }
            )
    if missing:
        raise HTTPException(
            status_code=409,
            detail={"missing_owner_approvals": missing},
        )


async def _check_plan_policies(
    session: AsyncSession,
    *,
    project: Project,
    branch: PlanBranch,
    user_id: uuid.UUID,
    base_payload: dict[str, Any],
    branch_payload: dict[str, Any],
    current_plan_hash: str,
) -> None:
    """Block the merge on an installed extension's blocking plan policy.

    Asked after the project's own gates passed, with who approved the branch's
    CURRENT content (a stale approval clears nothing here either). Without an
    extension nothing is asked and nothing is loaded.
    """
    if not extensions.extensions():
        return
    approver_ids, _stale = await _load_fresh_approver_ids(
        session, branch_id=branch.id, current_plan_hash=current_plan_hash
    )
    violations = await extensions.plan_policy_violations(
        session,
        PlanPolicyContext(
            phase="merge",
            organization_id=project.organization_id,
            project_id=project.id,
            project_slug=project.slug,
            branch_id=branch.id,
            actor_id=user_id,
            base_snapshot=base_payload,
            branch_snapshot=branch_payload,
            author_id=branch.created_by,
            approver_ids=frozenset(approver_ids),
        ),
    )
    refused = blocking(violations)
    if refused:
        raise HTTPException(status_code=409, detail=refusal_detail(refused))


async def assign_owner_reviewers_for_branch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch: PlanBranch,
) -> list[uuid.UUID]:
    """Upsert the owners of every touched event type as reviewers of ``branch``.

    Invoked when a branch enters review (the ``submit`` transition) so the people
    whose approval the merge gate later requires (:func:`_check_owner_approvals`)
    are surfaced as expected reviewers up front, without a manual lookup. The
    branch author is never assigned to review their own branch; whether an author
    may *approve* their own owned type is a separate policy.

    Idempotent: the ``(branch_id, user_id)`` unique key plus the pre-read of
    existing reviewers means a re-submit adds nothing new. Does not commit — it
    joins the caller's transaction.
    """
    main_branch_id = await ensure_main_branch_id(session, project_id)

    base_payload: dict[str, Any] = {}
    if branch.base_revision_id is not None:
        base_rev = await session.get(PlanRevision, branch.base_revision_id)
        if base_rev is not None:
            base_payload = base_rev.payload or {}
    branch_payload = await build_plan_snapshot(session, project_id, branch_id=branch.id)

    touched = _touched_event_type_names(base_payload, branch_payload)
    if not touched:
        return []

    # Only live (main) rows can be owned — a type freshly added on the branch has
    # no owner yet, so it drops out here.
    rows = await session.execute(
        select(EventType.id).where(
            EventType.project_id == project_id,
            EventType.branch_id == main_branch_id,
            EventType.name.in_(list(touched)),
        )
    )
    main_et_ids = [et_id for (et_id,) in rows.all()]
    if not main_et_ids:
        return []

    owners_by_et = await _owners_still_members(
        session, project_id, await load_owner_user_ids(session, main_et_ids)
    )
    owner_ids: set[uuid.UUID] = set()
    for ids in owners_by_et.values():
        owner_ids |= ids
    if branch.created_by is not None:
        owner_ids.discard(branch.created_by)
    if not owner_ids:
        return []

    existing = await session.execute(
        select(PlanBranchReviewer.user_id).where(PlanBranchReviewer.branch_id == branch.id)
    )
    already = {user_id for (user_id,) in existing.all()}
    added: list[uuid.UUID] = []
    for user_id in sorted(owner_ids - already, key=str):
        session.add(PlanBranchReviewer(branch_id=branch.id, user_id=user_id))
        added.append(user_id)
    return added


def _snapshot_event_key(event: dict[str, Any]) -> tuple[str, str]:
    """An event's natural key in a plan snapshot: ``(event_type_name, name)``.

    The name alone is not a key — two event types may each have a ``login`` —
    and a dict keyed on it keeps whichever sorts last.
    """
    return (str(event.get("event_type_name") or ""), str(event["name"]))


def _touched_event_names(
    base_payload: dict[str, Any], branch_payload: dict[str, Any]
) -> set[tuple[str, str]]:
    """Events added or changed on the branch relative to its merge base.

    ADDED = a ``(event_type_name, name)`` present on the branch but not the base
    (moving an event to another type is one of these). CHANGED = a key on both
    whose ``status`` or ``description`` differs. These are exactly the events an
    implementation ticket should cover — a pure reorder (``order`` only) or
    unchanged carry-over is ignored."""
    base_by_key = {_snapshot_event_key(e): e for e in base_payload.get("events", [])}
    touched: set[tuple[str, str]] = set()
    for key, branch_event in {
        _snapshot_event_key(e): e for e in branch_payload.get("events", [])
    }.items():
        base_event = base_by_key.get(key)
        if base_event is None or any(
            base_event.get(field) != branch_event.get(field) for field in ("status", "description")
        ):
            touched.add(key)
    return touched


async def _enqueue_implementation_ticket(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    branch_name: str,
    base_payload: dict[str, Any],
    branch_payload: dict[str, Any],
    post_payload: dict[str, Any],
) -> None:
    """Best-effort: open one tracker ticket covering the branch's added/changed
    events when the project has an enabled tracker config.

    The merge is already committed by the time this runs, so any failure here —
    config lookup, name→id resolution, or the Celery enqueue — must be swallowed
    and logged rather than propagated. It must NEVER fail or roll back the merge.
    """
    try:
        touched = _touched_event_names(base_payload, branch_payload)
        if not touched:
            return
        # Resolve touched keys to the post-merge MAIN event ids the ticket covers.
        post_id_by_key = {_snapshot_event_key(e): e["id"] for e in post_payload.get("events", [])}
        event_ids = [post_id_by_key[key] for key in sorted(touched) if key in post_id_by_key]
        if not event_ids:
            return
        config = await session.scalar(
            select(ProjectTrackerConfig).where(ProjectTrackerConfig.project_id == project_id)
        )
        if config is None or not config.enabled:
            return
        summary = f"Implement {len(event_ids)} event(s) from branch '{branch_name}'"
        # Lazy import: the worker task module pulls in Celery/worker deps that the
        # async request path shouldn't import at module load (cycle avoidance).
        from tripl.worker.tasks.implementation_tickets import create_implementation_ticket

        await dispatch(
            create_implementation_ticket.delay,
            str(project_id),
            str(branch_id),
            event_ids,
            summary,
        )
    except Exception:  # noqa: BLE001 — tracker automation must never break a merge
        logger.exception("Failed to enqueue implementation ticket for branch %s", branch_id)


async def _lock_branch_for_merge(
    session: AsyncSession, project_id: uuid.UUID, branch_id: uuid.UUID
) -> PlanBranch:
    """Load the branch row under a write lock held for the whole merge.

    ``_get_branch`` is a plain ``session.get``, so two merges arriving together
    both read ``approved``, both pass the status gate, and both apply the branch
    onto main — duplicating every add and re-running every field write.
    ``merge_branch`` commits exactly once, at the very end, so
    a row lock taken here is still held when the winner flips the status: the
    loser blocks until that commit, then re-reads ``merged`` and is rejected by
    the existing 400 below.

    ``populate_existing`` matters — the branch may already sit in the identity
    map from an earlier read in the request, and a cached instance would hand
    back the stale pre-lock status.

    On SQLite (tests) ``FOR UPDATE`` is a no-op; the guard is a PostgreSQL one,
    which is what production runs.
    """
    branch = await session.scalar(
        select(PlanBranch)
        .where(PlanBranch.id == branch_id, PlanBranch.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if branch is None:
        raise HTTPException(status_code=404, detail="Branch not found")
    return branch


class _MergeOutcome(NamedTuple):
    # The post-merge snapshot of the live plan.
    post_payload: dict[str, Any]
    # ``(storage_backend, storage_key, storage_config_id)`` of every uploaded
    # photo the merge deleted from main, for ``_release_photo_blobs``
    # (F20 PR11).
    released_blobs: frozenset[BlobRef]


async def _release_photo_blobs(
    session: AsyncSession, *, branch_id: uuid.UUID, blobs: frozenset[BlobRef]
) -> None:
    """Best-effort: delete the blobs the committed merge left no row pointing at.

    Runs after the commit, so a blob is never gone while a rolled-back merge
    would still point at it, and in a session of its own, so a failed read
    cannot leave the request's session in an aborted transaction for the reads
    after it. The merge is committed by then: nothing here may fail it or roll
    it back, and a blob this misses is one orphaned object, logged.
    """
    if not blobs:
        return
    try:
        async with AsyncSession(session.bind, expire_on_commit=False) as blob_session:
            await delete_unreferenced_blobs(blob_session, blobs)
    except Exception:  # noqa: BLE001 — a leaked blob must never break a merge
        logger.exception("Failed to release photo blobs after merging branch %s", branch_id)


async def _commit_merged_plan(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    main_branch_id: uuid.UUID,
    branch: PlanBranch,
    user_id: uuid.UUID,
    resolutions: dict[tuple[str, str, str], str],
    base_payload: dict[str, Any],
) -> _MergeOutcome:
    """Apply the branch onto main, record the revision, and commit.

    Returns the post-merge snapshot of the live plan and the photo blobs the
    merge released.

    Everything that writes lives in here, which makes this the one place a
    database constraint can reject a merge. It used to have no answer for that:
    the IntegrityError travelled all the way to ``unhandled_exception_handler``
    and the caller got a bare 500 naming nothing, on a branch that would keep
    failing the same way until someone renamed a row by hand.

    The known cause — a rename cycle colliding on
    ``uq_variable_project_name`` / ``uq_variable_project_source_name``, or on
    ``uq_event_scan_identity`` now that an event's identity is unique per type
     — is settled by the pairing in ``_apply_merge``. What still
    arrives here is either a shape the pairing declines on purpose — a branch
    that deletes a row and moves another onto its name, where the write of the
    freed identity runs ahead of the removal (the removal-order note in
    ``_plan_branch_merge_variables.apply_variables`` says why that is the
    better failure) — or one we have not modelled. It is still the user's
    merge that cannot proceed, and 409 says that; the constraint's own text
    stays in the log, where an operator can read it against the request id,
    rather than in a response body that would leak the schema.
    """
    # Bound to plain locals BEFORE the first write, and that ordering is the
    # whole point. A failed flush rolls back to the ROOT transaction, and
    # ``SessionTransaction._restore_snapshot(dirty_only=False)`` expires EVERY
    # state in the identity map on the way — ``branch`` included, and ``_expire``
    # takes all of its mapped attributes out of ``__dict__``, the primary key
    # among them. ``expire_on_commit=False`` (``database.py``) does not save us:
    # that flag guards only the COMMIT path. So reading ``branch.id`` in the
    # except arm below would trigger an expired-attribute reload — implicit IO on
    # the sync Session from plain async code, outside ``greenlet_spawn``, i.e.
    # ``MissingGreenlet`` — and the caller would get back exactly the bare 500
    # this function exists to replace.
    branch_id = branch.id
    branch_name = branch.name
    try:
        released_blobs = await _apply_merge(
            session,
            project_id,
            main_branch_id,
            branch_id,
            resolutions=resolutions,
            base_payload=base_payload,
        )
        # Post-merge snapshot of the live plan.
        post_payload = await build_plan_snapshot(session, project_id, branch_id=main_branch_id)
        session.add(
            PlanRevision(
                project_id=project_id,
                created_by=user_id,
                summary=f"Merged branch '{branch_name}'",
                kind=PlanRevisionKind.merge.value,
                branch_id=branch_id,
                payload=post_payload,
            )
        )
        branch.status = BranchStatus.merged.value
        branch.merged_at = datetime.now(UTC)
        branch.merged_by = user_id
        await session.commit()
    except IntegrityError as exc:
        # Explicit, though ``get_session`` would also roll back on the way out:
        # this leaves the session usable and drops the ``FOR UPDATE`` lock on the
        # branch at the point of failure rather than at the edge of the request.
        await session.rollback()
        # ``branch_id``, never ``branch.id`` — see the note above the try.
        logger.exception("Merge of branch %s was rejected by a database constraint", branch_id)
        raise HTTPException(
            status_code=409,
            detail={
                "merge_constraint_violation": True,
                "message": (
                    "Merging this branch would break a uniqueness rule on main — "
                    "most often two rows ending up with the same name or the same "
                    "scan identity. Rename the clashing entity on the branch and "
                    "merge again."
                ),
            },
        ) from exc
    return _MergeOutcome(post_payload=post_payload, released_blobs=released_blobs)


async def merge_branch(
    session: AsyncSession,
    slug: str,
    branch_id: uuid.UUID,
    user_id: uuid.UUID,
) -> PlanBranchDetailResponse:
    project = await _resolve_project(session, slug)
    branch = await _lock_branch_for_merge(session, project.id, branch_id)
    _reject_main(branch)
    if branch.status == BranchStatus.merged.value:
        raise HTTPException(status_code=400, detail="Branch is already merged")
    if branch.status != BranchStatus.approved.value:
        raise HTTPException(status_code=409, detail="Branch must be approved before merging")

    main_branch_id = await ensure_main_branch_id(session, project.id)
    # Main is locked from here to the commit, BEFORE main_payload is read for
    # the conflict check. Without it a main edit committed
    # between that read and ``_apply_merge`` was invisible to the check, and
    # the apply step, which compares the branch against the BASE, wrote the
    # branch's value over it: no conflict, no warning, no trail. Plan writes to
    # main hold main's row FOR SHARE (``api.deps.get_branch_id_override``, the
    # photo path), so each one now either commits before this lock is granted,
    # and the check below sees it, or waits until this merge commits and
    # applies on top of it. A lock rather than re-checking main's fingerprint
    # before the apply: a recheck still leaves the gap between the recheck and
    # the apply, and would fail a merge the user can only retry, while the lock
    # is one statement whose effect a two-session test can pin. Taken after the
    # branch's own lock, the order every merge uses; ``_plan_branch_locks``
    # holds the deadlock audit. A no-op off PostgreSQL.
    await lock_main_plan_for_merge(session, main_branch_id)
    base_payload: dict[str, Any] = {}
    if branch.base_revision_id is not None:
        base_rev = await session.get(PlanRevision, branch.base_revision_id)
        if base_rev is not None:
            base_payload = with_snapshot_defaults(base_rev.payload or {})
    require_complete_base(base_payload)
    main_payload = await build_plan_snapshot(session, project.id, branch_id=main_branch_id)
    branch_payload = await build_plan_snapshot(session, project.id, branch_id=branch.id)

    # Modify-modify clashes on event_type fields are surfaced via the inline
    # resolution flow; entity-level adds/removes and conflicts on other entity
    # kinds stay hard blockers — they aren't covered by v1 resolutions. The
    # gate is shared with ``merge_blocked_by`` and the merge preview.
    blocking = merge_blocking_conflicts(
        base_payload,
        main_payload,
        branch_payload,
        origins_complete=branch.origin_ids_complete,
    )
    field_conflicts = _field_conflicts_event_type(base_payload, main_payload, branch_payload)
    if blocking:
        raise HTTPException(status_code=409, detail={"conflicts": blocking})

    resolution_map: dict[tuple[str, str, str], str] = {}
    if field_conflicts:
        resolutions = await _load_resolutions(session, branch.id)
        unresolved: list[dict[str, Any]] = []
        for fc in field_conflicts:
            key = (fc["entity_type"], fc["name"], fc["field"])
            res = resolutions.get(key)
            if res is None:
                unresolved.append(
                    {
                        "entity_type": fc["entity_type"],
                        "name": fc["name"],
                        "field": fc["field"],
                    }
                )
            else:
                resolution_map[key] = res.choice
        if unresolved:
            raise HTTPException(
                status_code=409,
                detail={"unresolved_field_conflicts": unresolved},
            )

    current_plan_hash = plan_snapshot_hash(branch_payload)
    await _check_min_approvals(
        session, project_id=project.id, branch=branch, current_plan_hash=current_plan_hash
    )

    await _check_owner_approvals(
        session,
        project_id=project.id,
        main_branch_id=main_branch_id,
        branch_id=branch.id,
        base_payload=base_payload,
        branch_payload=branch_payload,
        current_plan_hash=current_plan_hash,
    )

    await _check_plan_policies(
        session,
        project=project,
        branch=branch,
        user_id=user_id,
        base_payload=base_payload,
        branch_payload=branch_payload,
        current_plan_hash=current_plan_hash,
    )

    outcome = await _commit_merged_plan(
        session,
        project_id=project.id,
        main_branch_id=main_branch_id,
        branch=branch,
        user_id=user_id,
        resolutions=resolution_map,
        base_payload=base_payload,
    )
    post_payload = outcome.post_payload
    await session.refresh(branch)

    # The blobs of photos the merge deleted from main that no row holds any
    # more. Best-effort; the merge is already committed.
    await _release_photo_blobs(session, branch_id=branch.id, blobs=outcome.released_blobs)

    # The merge rewrote main's event types and meta fields behind the service
    # functions that invalidate these caches on every other write, so main's
    # lists (and the project list's counts) would stay pre-merge until their
    # TTLs ran out. Best-effort by construction: delete_prefix swallows Redis
    # errors.
    for prefix in (
        cache.prefix_event_types(project.id),
        cache.prefix_meta_fields(project.id),
        cache.prefix_projects(),
    ):
        await cache.delete_prefix(prefix)

    # Post-merge tracker automation (best-effort; the merge is already committed).
    await _enqueue_implementation_ticket(
        session,
        project_id=project.id,
        branch_id=branch.id,
        branch_name=branch.name,
        base_payload=base_payload,
        branch_payload=branch_payload,
        post_payload=post_payload,
    )

    # Refresh main's search index right away — the merge just rewrote main's
    # entities, and the next worker-side refresh (post-scan) or CRUD edit may be
    # far off. Best-effort: the merge is committed, so a search-index failure
    # must never fail the merge response. Lazy import mirrors the ticket task
    # above (avoids service-module import cycles).
    #
    # In a session of its own. On the request's session a failed flush left it
    # waiting for a rollback (a failed statement leaves Postgres in an aborted
    # transaction), so the reads below 500'd a merge that is already committed —
    # and rolling back here instead would expire every object the caller holds
    # on that session, the current user the router audits with included.
    try:
        from tripl.services.search_service import reindex_project_branch

        async with AsyncSession(session.bind, expire_on_commit=False) as reindex_session:
            await reindex_project_branch(
                reindex_session, project_id=project.id, branch_id=main_branch_id, slug=slug
            )
    except Exception:  # noqa: BLE001 — search staleness must never break a merge
        logger.exception("Failed to reindex search after merging branch %s", branch_id)
    await _announce_merge(
        session,
        slug=slug,
        project_id=project.id,
        branch_id=branch.id,
        branch_name=branch.name,
        author_id=branch.created_by,
        actor_id=user_id,
    )
    return await _to_detail(session, branch)


async def _announce_merge(
    session: AsyncSession,
    *,
    slug: str,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    branch_name: str,
    author_id: uuid.UUID | None,
    actor_id: uuid.UUID,
) -> None:
    """``branch_merged`` (#259) to the author, the reviewers and the branch's watchers.

    Best-effort and in a session of its own, for the reason the search reindex
    above gives: the merge is committed, and a failure on the request's session
    would 500 it or expire what the caller still holds. ``notify`` drops the
    actor, non-members and whoever muted the branch.
    """
    try:
        from tripl.services import notification_service

        async with AsyncSession(session.bind, expire_on_commit=False) as notify_session:
            reviewers = set(
                (
                    await notify_session.scalars(
                        select(PlanBranchReviewer.user_id).where(
                            PlanBranchReviewer.branch_id == branch_id
                        )
                    )
                ).all()
            )
            actor = await notify_session.get(User, actor_id)
            who = (actor.name or actor.email) if actor is not None else "Someone"
            await notification_service.notify(
                notify_session,
                project_id=project_id,
                kind="branch_merged",
                entity_type=subscription_service.BRANCH,
                entity_id=branch_id,
                title=f"{who} merged branch {branch_name} into main",
                url=await project_link(notify_session, project_id, f"/branches/{branch_id}"),
                actor_user_id=actor_id,
                user_ids={*reviewers, *([author_id] if author_id is not None else [])},
                watchers_of=[(subscription_service.BRANCH, branch_id)],
            )
            await notify_session.commit()
    except Exception:  # noqa: BLE001 — a notification must never break a merge
        logger.exception("Failed to notify about merged branch %s", branch_id)
