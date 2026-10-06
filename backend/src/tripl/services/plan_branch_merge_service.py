from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, selectinload

from tripl import cache, extensions
from tripl.core.plan_policy import PlanPolicyContext, blocking, refusal_detail
from tripl.models.event import Event
from tripl.models.event import EventStatus as _ES
from tripl.models.event import event_status_rank as _rank
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.plan_branch import BranchStatus, PlanBranch
from tripl.models.plan_branch_approval import PlanBranchApproval
from tripl.models.plan_branch_reviewer import PlanBranchReviewer
from tripl.models.plan_revision import PlanRevision, PlanRevisionKind
from tripl.models.project import Project
from tripl.models.project_tracker_config import ProjectTrackerConfig
from tripl.models.subscription import Subscription
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import (
    VariableEventValueOverride,
    copy_override_values,
)
from tripl.schemas.plan_branch import PlanBranchDetailResponse
from tripl.services import project_access, subscription_service
from tripl.services._branch_counterparts import main_counterparts
from tripl.services._branch_event_threads import move_event_threads
from tripl.services._celery_dispatch import dispatch
from tripl.services._event_reference_cleanup import drop_dangling_event_references
from tripl.services._plan_branch_locks import lock_main_plan_for_merge
from tripl.services._plan_branch_renames import pair_renames, rekey_in_place
from tripl.services._plan_branch_sides import require_complete_base
from tripl.services._plan_merge_slots import merge_slots
from tripl.services.event_photo_service import (
    PHOTO_KIND_PHOTO,
    BlobRef,
    delete_unreferenced_blobs,
)
from tripl.services.event_type_owner_service import load_owner_user_ids
from tripl.services.plan_branch_conflicts import (
    _ET_CHANGE_KEYS,
    _entity_changed,
    _field_conflicts_event_type,
    _load_resolutions,
    main_keeps,
    merge_blocking_conflicts,
)
from tripl.services.plan_branch_service import (
    _load_for_branch,
    _reject_main,
    _resolve_project,
    _to_detail,
    ensure_main_branch_id,
)
from tripl.services.plan_revision_service import (
    _snapshot_fingerprint,
    build_plan_snapshot,
    plan_snapshot_hash,
    with_snapshot_defaults,
)
from tripl.services.project_branch_settings_service import read_branch_merge_policy
from tripl.services.project_links import project_link
from tripl.services.scan_config_lookup import (
    event_type_binding_conflict_detail,
    name_format_conflict_detail,
    scan_configs_binding_event_types,
    scan_configs_blocking_field_removals,
)

logger = logging.getLogger(__name__)


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


async def _reject_removals_a_scan_names_events_by(
    session: AsyncSession,
    project_id: uuid.UUID,
    removals: Sequence[tuple[uuid.UUID, str, FieldDefinition]],
) -> None:
    """Refuse the whole merge when it would delete a field a scan names events by.

    The THIRD door to the earlier outage, after the drift-accept in
    ``schema_drift_service`` and the plan-UI delete in ``field_service``. A merge
    that drops a FieldDefinition from main is the same ``session.delete(field)``
    with the same consequence: ``generate_events`` builds its format arguments
    only from columns that still have one, so every collection then dies on "the
    event name format references unknown keys".

    **Refusing the whole merge**, with a message naming every offending field, is
    the shape chosen over two alternatives:

    * *Refuse only that deletion and report it in the merge result.* It would
      silently diverge main from the branch that was just declared merged — main
      keeps a field the branch says is gone — and every later three-way merge
      compares against a base that never describes that state. There is also
      nowhere to report it: the merge returns ``PlanBranchDetailResponse``, so
      this needs a new response field and a new UI to read it, i.e. a fourth
      shape for one warning.
    * *Surface it through the merge-conflict machinery.* ``_detect_merge_conflicts``
      is a pure three-way payload diff and ``GET /branches/{id}/conflicts`` renders
      ``ConflictEntity{name, fields:[{field, base, ours, theirs}]}``. A scan-config
      dependency has no base/ours/theirs values and is not a divergence between two
      sides at all — it is an external constraint that would hold even if both
      sides agreed. Forcing it in means either fabricating those three values or
      inventing the fourth shape anyway.

    Blocking a large merge on one field is the cost, and it is the cost every
    other gate in ``merge_branch`` already charges (insufficient approvals, a
    stale base, an unresolved field conflict). The repair is one edit to the
    scan's Event name format, and then the merge goes through untouched.
    """
    # One query per DISTINCT event type rather than one per removed field: a
    # merge deleting twenty fields would otherwise issue twenty SELECTs with the
    # transaction already open. The batching lives in scan_config_lookup so this
    # door still does not assemble the predicate itself.
    naming = await scan_configs_blocking_field_removals(
        session,
        project_id=project_id,
        removals=[(event_type_id, field.name) for event_type_id, _, field in removals],
    )
    blocked: list[str] = []
    for main_event_type_id, event_type_name, field in removals:
        configs = naming.get((main_event_type_id, field.name))
        if configs:
            blocked.append(
                name_format_conflict_detail(
                    field_name=field.name,
                    configs=configs,
                    lead=(
                        "Cannot merge this branch: merging deletes "
                        f"'{event_type_name}.{field.name}' from main."
                    ),
                    then="merge the branch",
                )
            )
    if blocked:
        raise HTTPException(status_code=409, detail=" ".join(blocked))


# A name parked here exists only between the two flushes in
# ``_rename_main_variables``, inside the merge's own transaction. The prefix is
# deliberately outside what ``VariableCreate`` admits (``^[a-z][a-z0-9_]*$``),
# so a value that ever escaped the transaction would be unmistakable rather than
# look like a variable someone named badly.
_RENAME_STAGING_PREFIX = "__merge_rename_"


async def _rename_main_variables(
    session: AsyncSession,
    main_var_by_name: dict[str, Variable],
    renames: dict[str, str],
) -> None:
    """Write the branch's new names onto main's rows, cycles included.

    ``pair_renames`` can hand back a permutation — a plain two-variable swap, or
    a longer rotation — and then at least one row is moving onto a name another
    main row still holds. ``uq_variable_project_name`` is UNIQUE and NOT
    DEFERRABLE — ``Variable`` declares it as a plain ``UniqueConstraint``
    (``models/variable.py``), which is the immediate form — so there is no order
    of the UPDATEs that avoids a duplicate existing between two of them: only a
    third value does. Park every mover on one, flush that, then write the real
    names.

    ``4e5f60718293`` is the migration that gave the constraint its current
    ``(project_id, branch_id, name)`` shape, not ``d4f5e6a7b8c9``: that later
    revision only re-asserts both variable constraints ``IF NOT EXISTS`` for
    drifted environments, and on a database built by running the chain in order
    it creates nothing at all — its own downgrade comment says so.

    Parking ALL of them rather than only the ones that look blocked is what
    makes the second pass safe in any order, which matters because the order is
    SQLAlchemy's and not ours — the pending names go out on whichever flush
    comes first, and that is usually an unrelated autoflush further down.

    ``main_var_by_name`` is read here before ``rekey_in_place`` moves it, so its
    keys are still the OLD names and its membership is still main's pre-rename
    name set — which is exactly the question "is this destination occupied?".
    """
    movers = [(main_var_by_name[old_name], new_name) for old_name, new_name in renames.items()]
    if any(new_name in main_var_by_name for new_name in renames.values()):
        for variable, _new_name in movers:
            variable.name = f"{_RENAME_STAGING_PREFIX}{uuid.uuid4().hex}"
        await session.flush()
    for variable, new_name in movers:
        variable.name = new_name


# Nothing in it is main-specific — it renames whichever rows it is handed — so
# "Update from main" moves a branch's variables through the same parking pass
# when main renamed them, cycles included.
rename_variables_with_parking = _rename_main_variables


async def _load_variables(
    session: AsyncSession, project_id: uuid.UUID, branch_id: uuid.UUID
) -> list[Variable]:
    """A branch's variables without their observed-value contexts.

    ``Variable.value_contexts`` is ``lazy="selectin"``, and nothing in the merge
    reads it: a bare select would pull every context row and its
    FieldDefinition on each of the three loads, inside the open merge
    transaction — the cost already removed from ``build_plan_snapshot``. A
    deleted variable still cascades its contexts; the ORM loads them at delete
    time.
    """
    rows = await session.execute(
        select(Variable)
        .where(Variable.project_id == project_id, Variable.branch_id == branch_id)
        .options(lazyload(Variable.value_contexts))
    )
    return list(rows.scalars().all())


def _plain(value: object) -> object:
    """An enum column's value as the snapshot stores it (a plain string)."""
    return None if value is None else str(value)


def _photo_identity(photo: EventPhoto) -> tuple[object, ...]:
    """What makes two attachment rows the same attachment across a branch.

    Branch creation copies every one of these verbatim — ``storage_key``
    included, since no blob is duplicated — so a photo and its branch twin agree
    on all of them and on nothing else: the ids differ by construction, and
    ``created_at`` is when the copy was made. ``sort_order`` is deliberately
    absent, so that re-ordering a canvas on a branch is a move rather than a
    delete-and-replace. The key is fingerprinted the way the snapshot records
    it, so a row and the merge base's entry for it compare equal.
    """
    return (
        _plain(photo.kind),
        photo.original_filename,
        photo.content_type,
        photo.size_bytes,
        photo.external_url,
        _plain(photo.storage_backend),
        _snapshot_fingerprint(photo.storage_key),
    )


def _snapshot_photo_identity(photo: dict[str, Any]) -> tuple[object, ...]:
    """``_photo_identity`` for a photo entry of a plan snapshot."""
    return (
        photo.get("kind"),
        photo.get("original_filename"),
        photo.get("content_type"),
        photo.get("size_bytes"),
        photo.get("external_url"),
        photo.get("storage_backend"),
        photo.get("storage_key_fingerprint"),
    )


def _three_way_count(*, base: int, ours: int, theirs: int) -> int:
    """How many copies of one attachment main keeps after the merge.

    The same three-way rule every other attribute follows, applied to a count:
    a side that left the count where the base had it defers to the other side.
    When both moved it the same way that is one change, not two; when they moved
    it differently (conflict detection normally stops that first) the branch's
    delta is applied on top of main's.
    """
    if theirs == base:
        return ours
    if ours in (base, theirs):
        return theirs
    return max(0, ours + theirs - base)


def _split_identity_rows[M, B](
    main_rows: Sequence[M], branch_rows: Sequence[B], keep: int
) -> tuple[list[tuple[M, B]], list[B], list[M]]:
    """Main's and the branch's rows of one attachment, sorted into what happens.

    Returns ``(pairs, added, doomed)``: main rows kept beside the branch row
    whose discussion merges into them, branch rows copied onto main as new
    attachments, and main rows deleted — so that main ends with exactly
    ``keep``. Additions are reserved first: when both sides added copies,
    pairing a main copy with a branch copy would spend a branch row that is
    one of the branch's additions.
    """
    kept_main = list(main_rows[:keep])
    doomed = list(main_rows[keep:])
    n_added = min(max(0, keep - len(kept_main)), len(branch_rows))
    n_paired = min(len(kept_main), len(branch_rows) - n_added)
    pairs = list(zip(kept_main[:n_paired], branch_rows[:n_paired], strict=True))
    return pairs, list(branch_rows[n_paired : n_paired + n_added]), doomed


def _base_thread_for(
    branch_photo: EventPhoto, base_rows: Sequence[dict[str, Any]]
) -> tuple[set[tuple[object, ...]], bool]:
    """The base comment keys a branch photo's thread is compared against.

    Usually one base entry has the photo's identity. When the same attachment
    is on the event more than once, the entry in the branch photo's slot is
    its own; failing that, the union of every copy's thread answers "was this
    comment here at the cut?" but not "which copy was it on?" — so the second
    value says whether a missing comment may be read as a branch deletion.
    """
    same_slot = [p for p in base_rows if p.get("sort_order") == branch_photo.sort_order]
    attributed = same_slot if len(same_slot) == 1 else list(base_rows)
    keys: set[tuple[object, ...]] = set()
    for base_photo in attributed:
        keys |= _snapshot_comment_keys(base_photo.get("comments") or [])
    return keys, len(attributed) == 1


def _snapshot_comment_keys(threads: Sequence[Any]) -> set[tuple[object, ...]]:
    """The ``_comment_thread_in_order`` keys of a snapshot photo's comments."""
    keys: set[tuple[object, ...]] = set()

    def walk(nodes: Sequence[Any], parent_key: tuple[object, ...]) -> None:
        seen: dict[tuple[object, ...], int] = {}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            body_key = (parent_key, node.get("user_fingerprint"), node.get("body_fingerprint"))
            occurrence = seen.get(body_key, 0)
            seen[body_key] = occurrence + 1
            key = (*body_key, occurrence)
            keys.add(key)
            walk(node.get("replies") or [], key)

    walk(threads, ())
    return keys


def _comment_thread_in_order(
    rows: Sequence[EventPhotoComment],
) -> list[tuple[EventPhotoComment, tuple[object, ...]]]:
    """The thread parent-first, each row paired with a content key.

    The key answers "is this the same comment?" the way the plan snapshot does —
    by who wrote it and what it says (fingerprinted exactly as the snapshot
    fingerprints them, so a key can be looked up in the merge base), plus where
    it hangs — with an occurrence number so that saying the same thing twice
    under one parent stays two comments. Parent-first order lets a caller insert
    a reply after the comment it answers, which is what the self-FK needs.
    """
    by_parent: dict[uuid.UUID | None, list[EventPhotoComment]] = {}
    known = {row.id for row in rows}
    for row in rows:
        # A parent outside this photo's thread cannot be reached by the walk, so
        # treat its child as top-level rather than dropping it silently.
        parent_id = row.parent_id if row.parent_id in known else None
        by_parent.setdefault(parent_id, []).append(row)

    ordered: list[tuple[EventPhotoComment, tuple[object, ...]]] = []
    canonical_by_id: dict[uuid.UUID, str] = {}

    def canonical(row: EventPhotoComment) -> str:
        # The row's subtree in the snapshot's own shape and order. Numbering
        # identical siblings in THIS order (not created_at: a branch copy's
        # rows share one) gives each the occurrence the merge base gave it, so
        # a reply cannot swap parents between the two and read as deleted.
        if row.id not in canonical_by_id:
            node = {
                "user_fingerprint": _snapshot_fingerprint(row.user_id),
                "body_fingerprint": _snapshot_fingerprint(row.body),
                "replies": [
                    json.loads(canonical(child))
                    for child in sorted(by_parent.get(row.id, []), key=canonical)
                ],
            }
            canonical_by_id[row.id] = json.dumps(node, sort_keys=True, separators=(",", ":"))
        return canonical_by_id[row.id]

    def walk(parent_id: uuid.UUID | None, parent_key: tuple[object, ...]) -> None:
        seen: dict[tuple[object, ...], int] = {}
        siblings = sorted(
            by_parent.get(parent_id, []),
            key=lambda item: (canonical(item), item.created_at, item.id),
        )
        for row in siblings:
            body_key = (
                parent_key,
                _snapshot_fingerprint(row.user_id),
                _snapshot_fingerprint(row.body),
            )
            occurrence = seen.get(body_key, 0)
            seen[body_key] = occurrence + 1
            key = (*body_key, occurrence)
            ordered.append((row, key))
            walk(row.id, key)

    walk(None, ())
    return ordered


def _comments_deleted_on_branch(
    target_thread: list[tuple[EventPhotoComment, tuple[object, ...]]],
    *,
    base_keys: set[tuple[object, ...]],
    source_keys: set[tuple[object, ...]],
) -> list[uuid.UUID]:
    """Target comments the branch deleted: in the base, gone from the branch.

    A comment main has since answered is kept — deleting it would cascade the
    reply main wrote after the cut, which the branch never saw.
    """
    key_by_id = {row.id: key for row, key in target_thread}
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for row, _key in target_thread:
        if row.parent_id is not None and row.parent_id in key_by_id:
            children.setdefault(row.parent_id, []).append(row.id)

    def answered_on_main(row_id: uuid.UUID) -> bool:
        return any(
            key_by_id[child] not in base_keys or answered_on_main(child)
            for child in children.get(row_id, [])
        )

    return [
        row.id
        for row, key in target_thread
        if key in base_keys and key not in source_keys and not answered_on_main(row.id)
    ]


async def _merge_photo_comments(
    session: AsyncSession,
    *,
    target_photo_id: uuid.UUID,
    source_photo_id: uuid.UUID,
    base_thread: tuple[set[tuple[object, ...]], bool],
) -> None:
    """Merge the source thread into the target's, three-way against the base.

    A merge used to hand main the branch's discussion and nothing else, because
    it replaced the photo rows outright; then it took the plain union, which
    brought back every comment main had deleted since the cut and never carried
    a branch-side deletion over. ``base_thread`` is the thread's keys as they
    stood at the cut, so each side's own change can be told apart: a comment
    new on the branch is added, one main deleted stays deleted (with any reply
    the branch wrote under it), and one the branch deleted is removed from main
    too — the last only when the base thread is known to be THIS photo's
    (``base_thread[1]``), since a deletion on a guess is not recoverable.
    """
    base_keys, deletions_allowed = base_thread
    target_rows = list(
        (
            await session.execute(
                select(EventPhotoComment).where(EventPhotoComment.photo_id == target_photo_id)
            )
        )
        .scalars()
        .all()
    )
    source_rows = list(
        (
            await session.execute(
                select(EventPhotoComment).where(EventPhotoComment.photo_id == source_photo_id)
            )
        )
        .scalars()
        .all()
    )
    target_thread = _comment_thread_in_order(target_rows)
    source_thread = _comment_thread_in_order(source_rows)
    source_key_by_id = {row.id: key for row, key in source_thread}
    doomed_ids = (
        _comments_deleted_on_branch(
            target_thread, base_keys=base_keys, source_keys=set(source_key_by_id.values())
        )
        if deletions_allowed
        else []
    )
    if doomed_ids:
        await session.execute(delete(EventPhotoComment).where(EventPhotoComment.id.in_(doomed_ids)))
    doomed = set(doomed_ids)
    target_id_by_key = {key: row.id for row, key in target_thread if row.id not in doomed}
    inserted_id_by_source_id: dict[uuid.UUID, uuid.UUID] = {}
    deleted_on_main: set[uuid.UUID] = set()
    for row, key in source_thread:
        if key in target_id_by_key:
            continue
        # On main at the cut and deleted there since: main's deletion took the
        # replies with it, so a reply the branch wrote under it goes too rather
        # than surfacing on main as a top-level comment answering nothing.
        if key in base_keys or row.parent_id in deleted_on_main:
            deleted_on_main.add(row.id)
            continue
        parent_id: uuid.UUID | None = None
        if row.parent_id is not None:
            # The parent is either a comment this call just carried over, or one
            # the target already had under the same key.
            parent_key = source_key_by_id.get(row.parent_id)
            parent_id = inserted_id_by_source_id.get(row.parent_id) or (
                target_id_by_key.get(parent_key) if parent_key is not None else None
            )
        new_id = uuid.uuid4()
        inserted_id_by_source_id[row.id] = new_id
        session.add(
            EventPhotoComment(
                id=new_id,
                photo_id=target_photo_id,
                parent_id=parent_id,
                user_id=row.user_id,
                body=row.body,
                # When it was written, not when it was merged: the thread on
                # main is ordered by created_at.
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
        # A later reply keyed under this comment must find it.
        target_id_by_key[key] = new_id
    await session.flush()


async def _blob_keys_of(session: AsyncSession, event_ids: Sequence[uuid.UUID]) -> set[BlobRef]:
    """Every uploaded blob the given events' attachments point at.

    Read BEFORE the rows go, because they go by FK cascade: ``EventPhoto``
    declares ``ondelete="CASCADE"`` on ``event_id`` and ``Event`` carries no
    ``photos`` relationship, so deleting an event takes its attachments at the
    database level with nothing in Python seeing them leave.
    The merge deletes events on two paths — a removed event type takes its
    events, and a removed event goes on its own — and neither reached the photo
    reconciliation that fills ``released_blobs``, so a screenshot on an event
    the branch deleted stayed in storage with no row left to find it by.

    Over-collecting is safe: ``delete_unreferenced_blobs`` re-checks every key
    against the rows that survived the commit and skips the ones still in use.
    """
    if not event_ids:
        return set()
    rows = await session.execute(
        select(
            EventPhoto.storage_backend, EventPhoto.storage_key, EventPhoto.storage_config_id
        ).where(
            EventPhoto.event_id.in_(event_ids),
            EventPhoto.kind == PHOTO_KIND_PHOTO,
            EventPhoto.storage_backend.is_not(None),
            EventPhoto.storage_key.is_not(None),
        )
    )
    # The query already drops NULL keys; the guard only tells the type checker so.
    return {
        (str(backend), key, config_id) for backend, key, config_id in rows.all() if key is not None
    }


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

    The discussion is not plan content: no snapshot carries it,
    so no arm above sees it, and the deep copy leaves it on main's row, where a
    branch copy reads and writes it through its twin. What sits on a BRANCH row
    is only what had no twin to go to — the thread of an event created on the
    branch, the note typed with it at creation included, or one started before
    main grew a row for that identity. The merge used to leave all of it there:
    main's new row opened with an empty thread, the branch row then read through
    to that empty twin, and deleting the merged branch took the rows with it
    through the cascade.

    None of it was ever copied from main, so all of it is the branch's own and
    there is no base to merge three ways against — unlike the photo threads
    above. The rows are MOVED, not copied: ids, replies and resolution state go
    unchanged, beside whatever thread the target row already had. The target is
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

    Returns the ``(storage_backend, storage_key)`` of every uploaded photo the
    photos arm deleted from main, for ``merge_branch`` to release once the merge
    has committed.
    """
    resolutions = resolutions or {}
    # Read before anything below writes: the twin each thread-holding branch
    # row reads its discussion through today, which the thread move at the end
    # prefers over the row the merge lands its key on.
    thread_twins = await _event_thread_twins(session, project_id=project_id, branch_id=branch_id)
    branch_origins_complete = bool(
        await session.scalar(
            select(PlanBranch.origin_ids_complete).where(PlanBranch.id == branch_id)
        )
    )
    released_blobs: set[BlobRef] = set()
    base_et_by_name: dict[str, dict[str, Any]] = {
        e["name"]: e for e in (base_payload or {}).get("event_types", [])
    }
    branch_snapshot_payload = await build_plan_snapshot(session, project_id, branch_id=branch_id)
    # --- event_types
    main_ets = list(
        (
            await session.execute(
                select(EventType)
                .where(EventType.project_id == project_id, EventType.branch_id == main_branch_id)
                .options(selectinload(EventType.field_definitions))
            )
        )
        .scalars()
        .all()
    )
    branch_ets = list(
        (
            await session.execute(
                select(EventType)
                .where(EventType.project_id == project_id, EventType.branch_id == branch_id)
                .options(selectinload(EventType.field_definitions))
            )
        )
        .scalars()
        .all()
    )
    main_et_by_name = {et.name: et for et in main_ets}
    branch_et_by_name = {et.name: et for et in branch_ets}

    for name, b_et in branch_et_by_name.items():
        m_et = main_et_by_name.get(name)
        if m_et is not None:
            # 3-way per-field merge. Falls back to branch-wins when no base
            # snapshot is available (legacy path).
            b_dict = base_et_by_name.get(name)
            for field in _ET_CHANGE_KEYS:
                choice = resolutions.get(("event_type", name, field))
                if choice == "ours":
                    continue
                if choice == "theirs":
                    setattr(m_et, field, getattr(b_et, field))
                    continue
                if main_keeps(field, b_dict, getattr(m_et, field)):
                    continue
                if b_dict is None:
                    setattr(m_et, field, getattr(b_et, field))
                    continue
                base_v = b_dict.get(field)
                theirs_v = getattr(b_et, field)
                # Branch changed this field → take it; otherwise keep main's
                # current value (which may include main-side edits).
                if theirs_v != base_v:
                    setattr(m_et, field, theirs_v)
        else:
            # The entity existed in the base but is now absent on main: that is
            # a main-only deletion. An unchanged branch must not resurrect it.
            # Divergent branch edits are rejected by conflict detection.
            if name in base_et_by_name:
                continue
            session.add(
                EventType(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    branch_id=main_branch_id,
                    name=b_et.name,
                    display_name=b_et.display_name,
                    description=b_et.description,
                    color=b_et.color,
                    order=b_et.order,
                )
            )
    removed_main_ets = [
        m_et
        for name, m_et in main_et_by_name.items()
        if name in base_et_by_name and name not in branch_et_by_name
    ]
    # The second door onto ``scan_configs.event_type_id``'s ON DELETE SET NULL,
    # after ``event_type_service.delete_event_type``: merging a branch that
    # removed an event type deletes main's copy, and every scan bound to it goes
    # on running against an empty binding, collecting nothing.
    # Refused for the reason ``_reject_removals_a_scan_names_events_by`` — the
    # field-removal guard, awaited further down this same function — gives in its
    # docstring: a merge refuses whole rather than skipping the deletion.
    if removed_main_ets:
        binding = await scan_configs_binding_event_types(
            session,
            project_id=project_id,
            event_type_ids=[m_et.id for m_et in removed_main_ets],
        )
        blocked_types = [
            event_type_binding_conflict_detail(
                configs=binding[m_et.id],
                lead=f"Cannot merge this branch: merging deletes '{m_et.name}' from main.",
                then="merge the branch",
            )
            for m_et in removed_main_ets
            if binding.get(m_et.id)
        ]
        if blocked_types:
            raise HTTPException(status_code=409, detail=" ".join(blocked_types))
    if removed_main_ets:
        # BEFORE the delete, and this placement is the substance of the fix.
        # Deleting the event type takes its events with it through the database
        # cascade at the flush below — EventType maps no ``events`` relationship,
        # so no service ever sees those rows go. Their dangling references have
        # to be cleared here or nowhere.
        #
        # There is no survivor to re-point at: these main events lose their event
        # type outright, so the rule is DROP, exactly as on the three CRUD delete
        # doors.
        doomed_event_ids = list(
            (
                await session.execute(
                    select(Event.id).where(
                        Event.event_type_id.in_([m_et.id for m_et in removed_main_ets])
                    )
                )
            )
            .scalars()
            .all()
        )
        released_blobs |= await _blob_keys_of(session, doomed_event_ids)
        await drop_dangling_event_references(
            session, project_id=project_id, event_ids=doomed_event_ids
        )
    for name, m_et in list(main_et_by_name.items()):
        if name in base_et_by_name and name not in branch_et_by_name:
            await session.delete(m_et)
            del main_et_by_name[name]
    await session.flush()

    # Re-load main event types so name→id mapping reflects new inserts.
    main_ets_after = list(
        (
            await session.execute(
                select(EventType).where(
                    EventType.project_id == project_id, EventType.branch_id == main_branch_id
                )
            )
        )
        .scalars()
        .all()
    )
    main_et_name_to_id = {et.name: et.id for et in main_ets_after}
    branch_et_id_to_name = {et.id: et.name for et in branch_ets}

    # --- field_definitions: apply only branch-side deltas from the base.
    # Main-only additions/edits therefore survive an unrelated branch merge.
    main_field_by_key: dict[tuple[str, str], uuid.UUID] = {}
    field_attrs = (
        "display_name",
        "field_type",
        "is_required",
        "enum_options",
        "description",
        "order",
        "sensitivity",
        "contract_required_max_null_rate",
        "contract_regex",
        "contract_min_value",
        "contract_max_value",
        "contract_max_bad_rate",
    )
    main_et_by_id = {event_type.id: event_type for event_type in main_ets_after}
    main_fields = list(
        (
            await session.execute(
                select(FieldDefinition).where(
                    FieldDefinition.event_type_id.in_(list(main_et_by_id))
                )
            )
        )
        .scalars()
        .all()
    )
    main_fields_by_key = {
        (main_et_by_id[field.event_type_id].name, field.name): field for field in main_fields
    }
    removals: list[tuple[uuid.UUID, str, FieldDefinition]] = []
    for et_name, b_et in branch_et_by_name.items():
        if et_name not in main_et_name_to_id:
            continue
        m_et_id = main_et_name_to_id[et_name]
        base_fields = {
            field["name"]: field
            for field in base_et_by_name.get(et_name, {}).get("field_definitions", [])
        }
        branch_fields = {field.name: field for field in b_et.field_definitions}
        for field_name, b_fd in branch_fields.items():
            key = (et_name, field_name)
            m_fd = main_fields_by_key.get(key)
            base_fd = base_fields.get(field_name)
            # Read before a missing row is created below: a field new on the
            # branch alone takes every value from the branch, its order too.
            main_existed = m_fd is not None
            if m_fd is None:
                if base_fd is not None:
                    continue
                m_fd = FieldDefinition(id=uuid.uuid4(), event_type_id=m_et_id, name=field_name)
                session.add(m_fd)
                main_fields_by_key[key] = m_fd
            for attr in field_attrs:
                if main_existed and main_keeps(attr, base_fd, getattr(m_fd, attr)):
                    continue
                branch_value = getattr(b_fd, attr)
                if base_fd is None or branch_value != base_fd.get(attr):
                    if attr == "enum_options":
                        branch_value = list(branch_value) if branch_value else None
                    setattr(m_fd, attr, branch_value)
        for field_name in set(base_fields) - set(branch_fields):
            m_fd = main_fields_by_key.pop((et_name, field_name), None)
            if m_fd is not None:
                removals.append((m_et_id, et_name, m_fd))

    # Checked here rather than from the payloads in ``merge_branch`` so there is
    # exactly one definition of "fields this merge deletes" — the list the deletes
    # are actually issued from. Nothing is committed yet, so a refusal rolls the
    # whole merge back.
    await _reject_removals_a_scan_names_events_by(session, project_id, removals)
    for _, _, m_fd in removals:
        await session.delete(m_fd)
    await session.flush()

    main_field_by_key = {key: field.id for key, field in main_fields_by_key.items()}

    branch_field_by_id = {
        fd.id: (branch_et_id_to_name[fd.event_type_id], fd.name)
        for et in branch_ets
        for fd in et.field_definitions
    }

    # --- meta_field_definitions: upsert by name (preserve ids)
    main_mfs = await _load_for_branch(session, MetaFieldDefinition, project_id, main_branch_id)
    branch_mfs = await _load_for_branch(session, MetaFieldDefinition, project_id, branch_id)
    main_mf_by_name = {mf.name: mf for mf in main_mfs}
    branch_mf_by_name = {mf.name: mf for mf in branch_mfs}
    base_mf_by_name = {mf["name"]: mf for mf in (base_payload or {}).get("meta_fields", [])}
    meta_attrs = (
        "display_name",
        "field_type",
        "is_required",
        "allow_multiple",
        "enum_options",
        "default_value",
        "link_template",
        "order",
        "sensitivity",
    )
    for name, b_mf in branch_mf_by_name.items():
        m_mf = main_mf_by_name.get(name)
        if m_mf is not None:
            base_mf = base_mf_by_name.get(name)
            for attr in meta_attrs:
                if main_keeps(attr, base_mf, getattr(m_mf, attr)):
                    continue
                branch_value = getattr(b_mf, attr)
                if base_mf is None or branch_value != base_mf.get(attr):
                    if attr == "enum_options":
                        branch_value = list(branch_value) if branch_value else None
                    setattr(m_mf, attr, branch_value)
        else:
            if name in base_mf_by_name:
                continue
            session.add(
                MetaFieldDefinition(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    branch_id=main_branch_id,
                    name=b_mf.name,
                    display_name=b_mf.display_name,
                    field_type=b_mf.field_type,
                    is_required=b_mf.is_required,
                    allow_multiple=b_mf.allow_multiple,
                    enum_options=list(b_mf.enum_options) if b_mf.enum_options else None,
                    default_value=b_mf.default_value,
                    link_template=b_mf.link_template,
                    order=b_mf.order,
                    sensitivity=b_mf.sensitivity,
                )
            )
    for name, m_mf in list(main_mf_by_name.items()):
        if name in base_mf_by_name and name not in branch_mf_by_name:
            await session.delete(m_mf)
    await session.flush()
    main_mf_name_to_id: dict[str, uuid.UUID] = {
        mf.name: mf.id
        for mf in await _load_for_branch(session, MetaFieldDefinition, project_id, main_branch_id)
    }
    branch_mf_id_to_name = {mf.id: mf.name for mf in branch_mfs}

    def main_meta_field_id(event: Event, mv: EventMetaValue) -> uuid.UUID:
        """Translate a branch meta value onto main by NAME, or refuse the merge.

        ``branch_mf_id_to_name`` holds this branch's own definitions only, so a
        value pointing at another branch's definition was an unqualified
        subscript — the same bare 500, from the same pre-refusal rows, that
        ``deep_copy_plan_to_branch`` now answers 409 for.
        ``event_service._normalize_meta_values`` refuses to write one today; no
        migration sweeps the ones already stored, and the merge is the second
        place they surface.

        Refusing rather than skipping the value: both replay arms below DELETE
        main's whole set for the event and rebuild it from the branch, so a
        skipped value is one that disappears from main with nothing said.

        ``main_mf_name_to_id`` needs no guard of its own — it is rebuilt after
        the meta-field upsert above, which gives main a definition for every
        name this branch has.
        """
        mf_name = branch_mf_id_to_name.get(mv.meta_field_definition_id)
        if mf_name is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot merge this branch: event '{event.name}' carries a meta "
                    f"value on {mv.meta_field_definition_id}, which is not a meta "
                    "field of this branch, so it has no name to carry onto main. "
                    "Edit the event to drop that value, then merge."
                ),
            )
        return main_mf_name_to_id[mf_name]

    # --- variables: upsert by name
    main_vars = await _load_variables(session, project_id, main_branch_id)
    branch_vars = await _load_variables(session, project_id, branch_id)
    main_var_by_name = {v.name: v for v in main_vars}
    branch_var_by_name = {v.name: v for v in branch_vars}
    base_var_by_name = {v["name"]: v for v in (base_payload or {}).get("variables", [])}
    # A rename is one row, not a removal plus an addition. Unpaired, the arms
    # below add a Variable carrying main's own ``source_name`` to main while the
    # delete of the row it replaces is still pending in the same flush —
    # SQLAlchemy orders a mapper's saves ahead of its deletes — so
    # ``uq_variable_project_source_name`` fails the whole merge with an
    # IntegrityError. Renaming main's row instead settles that and keeps the
    # variable's id, which every ``variable_values`` row hangs off.
    #
    # A swap or a rotation is several renames at once, so the moves have to be
    # applied as the permutation they are: the names through a parking value
    # (``_rename_main_variables``) and the lookups all-at-once
    # (``rekey_in_place``). Doing either one pair at a time re-creates the very
    # collision the pairing removes.
    var_renames = {
        old_key[0]: new_key[0]
        for old_key, new_key in pair_renames(
            {(name,): variable.get("source_name") for name, variable in base_var_by_name.items()},
            {(name,): variable.source_name for name, variable in main_var_by_name.items()},
            {(name,): variable.source_name for name, variable in branch_var_by_name.items()},
            vacate_removed=True,
        ).items()
    }
    # A move onto a name a non-moving main row holds is only proposed when the
    # branch deleted that row. It has to go FIRST and be flushed on
    # its own: SQLAlchemy orders a mapper's saves ahead of its deletes, so left
    # to the removal loop below it would still hold the name and the
    # ``source_name`` slot when the renamed row's UPDATE goes out.
    displaced = [
        main_var_by_name.pop(new_name)
        for new_name in var_renames.values()
        if new_name in main_var_by_name and new_name not in var_renames
    ]
    if displaced:
        for occupant in displaced:
            await session.delete(occupant)
        await session.flush()
    if var_renames:
        await _rename_main_variables(session, main_var_by_name, var_renames)
        rekey_in_place(main_var_by_name, var_renames)
        # The base entry has to move with it. Every comparison downstream reads
        # ``base_var_by_name.get(name)`` and falls back to branch-wins when the
        # entry is missing, so a base left under the old name would silently
        # overwrite main-only edits on the row that was renamed.
        rekey_in_place(base_var_by_name, var_renames)
    variable_attrs = (
        "source_name",
        "variable_type",
        "description",
        "allowed_values",
        "bindings",
        "excluded_from_scans",
        "json_schema",
    )
    for name, b_v in branch_var_by_name.items():
        m_v = main_var_by_name.get(name)
        if m_v is not None:
            base_var = base_var_by_name.get(name)
            # ``variable_type`` and ``json_schema`` are taken attribute by
            # attribute like the rest; they cannot come from different sides,
            # because the conflict check compares them as one value and refuses
            # the merge when both sides changed it (``comparable_field``).
            for attr in variable_attrs:
                branch_value = getattr(b_v, attr)
                if base_var is None or branch_value != base_var.get(attr):
                    if attr in ("allowed_values", "bindings"):
                        branch_value = list(branch_value or [])
                    elif attr == "json_schema":
                        branch_value = copy.deepcopy(branch_value)
                    setattr(m_v, attr, branch_value)
        else:
            if name in base_var_by_name:
                continue
            session.add(
                Variable(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    branch_id=main_branch_id,
                    name=b_v.name,
                    source_name=b_v.source_name,
                    variable_type=b_v.variable_type,
                    description=b_v.description,
                    allowed_values=list(b_v.allowed_values or []),
                    bindings=list(b_v.bindings or []),
                    json_schema=copy.deepcopy(b_v.json_schema),
                    excluded_from_scans=b_v.excluded_from_scans,
                )
            )

    # Removals go out AFTER the writes above, in the same flush, and that is
    # deliberate rather than left over. SQLAlchemy runs a mapper's saves ahead of
    # its deletes, so a name or a ``source_name`` still held by a row on its way
    # out is NOT free for the row taking it — and when the two arms disagree
    # about one identity, the flush raises and ``_commit_merged_plan`` turns that
    # into a 409 that loses nothing.
    #
    # Deleting first would make that collision succeed instead, which is worse
    # than it sounds. Base and main both hold ``a``/S1 and ``b``/S2; the branch
    # deletes ``b`` and renames ``a`` to ``b``. ``pair_renames`` proposes a -> b
    # and then drops it, because main's own ``b`` is not itself moving away — so
    # no rename is applied. Removing first would then delete main's ``a`` with
    # its ``variable_values``, ``variable_value_drifts`` and
    # ``variable_event_value_overrides``, and the upsert would write S1 onto
    # main's surviving ``b``: the row the user KEPT gone with all its observed
    # values and drift triage, the row the user DELETED left wearing the kept
    # row's scan identity, and the next scan matching warehouse data onto the
    # wrong history. Nothing in ``build_plan_snapshot`` would show it.
    #
    # The one unambiguous version of that shape — the branch deleted ``b``, whose
    # identity S2 no branch row carries any more, and ``b`` still wears S2 on
    # main as it did at the cut — is now paired, and its occupant deleted and
    # flushed ahead of the rename above. Every other merge that
    # wants both the deletion and the move onto the freed name is still
    # ambiguous, and 409 asking the user to rename the clashing entity is the
    # honest answer. Cycles do NOT rely on this order: the parking
    # pass in ``_rename_main_variables`` is what makes a swap or a rotation work,
    # and it operates on names before either arm runs.
    for name, m_v in list(main_var_by_name.items()):
        if name in base_var_by_name and name not in branch_var_by_name:
            await session.delete(m_v)

    # --- events: upsert by (event_type_name, name); preserve ids + remap children
    main_events = list(
        (
            await session.execute(
                select(Event)
                .where(Event.project_id == project_id, Event.branch_id == main_branch_id)
                .options(
                    selectinload(Event.field_values),
                    selectinload(Event.meta_values),
                    selectinload(Event.tags),
                )
            )
        )
        .scalars()
        .all()
    )
    branch_events = list(
        (
            await session.execute(
                select(Event)
                .where(Event.project_id == project_id, Event.branch_id == branch_id)
                .options(
                    selectinload(Event.field_values),
                    selectinload(Event.meta_values),
                    selectinload(Event.tags),
                )
            )
        )
        .scalars()
        .all()
    )
    main_et_id_to_name = {et.id: et.name for et in main_ets_after}
    # Events whose parent event_type was just removed are orphans on main; in
    # Postgres they'd cascade-delete via the FK, SQLite (test env) doesn't, so
    # we delete them explicitly to keep the in-memory lookup consistent.
    for e in main_events:
        if e.event_type_id not in main_et_id_to_name:
            await session.delete(e)
    await session.flush()
    main_events = [e for e in main_events if e.event_type_id in main_et_id_to_name]
    # The subscript below was unguarded, and it is the FIRST place a branch event
    # parented by another branch's type lands — the pre-refusal row shape
    # ``event_service`` now blocks, with no migration sweeping the ones already
    # stored. A KeyError here is the same bare 500 on the merge,
    # naming nothing, that ``deep_copy_plan_to_branch`` now answers 409 for.
    #
    # Deliberately NOT the deletion main's events get a few lines above: there
    # the type is missing because THIS merge removed it, so its events are doomed
    # by the merge itself. Here the type is simply on another branch, and
    # dropping the event would carry onto main a deletion nobody made.
    mis_parented = [e for e in branch_events if e.event_type_id not in branch_et_id_to_name]
    if mis_parented:
        raise HTTPException(
            status_code=409,
            detail=" ".join(
                f"Cannot merge this branch: event '{e.name}' is parented by "
                f"{e.event_type_id}, which is not an event type of this branch, so it "
                "has no type on main to land under. Delete the event, then merge."
                for e in mis_parented
            ),
        )

    def main_event_key(event: Event) -> tuple[str, str]:
        return (main_et_id_to_name[event.event_type_id], event.name)

    def branch_event_key(event: Event) -> tuple[str, str]:
        return (branch_et_id_to_name[event.event_type_id], event.name)

    def base_event_key(event: dict[str, Any]) -> tuple[str, str]:
        return (event["event_type_name"], event["name"])

    branch_event_snapshot_by_id: dict[str, dict[str, Any]] = {
        event["id"]: event for event in branch_snapshot_payload.get("events", [])
    }
    # Row by row, not key by key. Main's rows pair with the
    # base by their own ids, which the base recorded; the branch's by the main
    # row each copy was made from. Keyed by (type, name) instead, two namesakes
    # collapsed to one on every side: deleting one of them on the branch left
    # both on main, and each copy's edits landed on whichever main namesake the
    # dict kept. The natural key still pairs the rows the ids
    # do not place — see ``merge_slots``.
    #
    # A slot whose branch copy moved to another name is a rename, and main's row
    # takes the new name in place. Event has no ``display_name`` — its machine
    # name is the one on screen, so renaming an event is routine editing — and
    # replacing the row would take its ``variable_values``, their drift rows and
    # its ``event_changes`` with it through the FK cascade, and leave the
    # ``event_metrics`` series holding a NULL ``event_id`` (that FK is SET NULL).
    # Only the name can differ: a copy under another TYPE is a removal plus an
    # addition, as the natural-key merge always read it. Events need no parking
    # pass: nothing is unique on the name, and ``uq_event_scan_identity`` is on
    # ``(event_type_id, source_name)``, which a name-only write never touches.
    event_slots = merge_slots(
        list((base_payload or {}).get("events", [])),
        main_events,
        branch_events,
        base_key=base_event_key,
        main_key=main_event_key,
        branch_key=branch_event_key,
        main_ref=lambda event: str(event.id),
        branch_ref=lambda event: str(event.origin_id or event.id),
        follows_rename=lambda old_key, new_key: old_key[0] == new_key[0],
        branch_origins_complete=branch_origins_complete,
        identities=(
            lambda event: event.get("source_name"),
            lambda event: event.source_name,
            lambda event: event.source_name,
        ),
    )
    # (branch row, the main row it lands on, its base state) for every branch
    # row the merge carries onto main, created or matched. Every arm below that
    # follows a branch row onto main — the successor pointer, the photos, the
    # discussion and the variable overrides — reads it from here, so none of
    # them can pick a different namesake than the attribute writes did.
    landings: list[tuple[Event, Event, dict[str, Any] | None]] = []
    doomed_main_events: list[Event] = []
    event_attrs = (
        "source_name",
        "title",
        "description",
        "sunset_at",
        "order",
        "owner_id",
        "reviewed",
        "metric_breakdown_columns",
        "required_presence_threshold",
    )
    for slot in event_slots.slots:
        m_ev, b_ev, base_event = slot.main, slot.branch, slot.base
        if m_ev is None:
            # Main deleted the row after the cut (or never had it): main's
            # deletion stands, and divergent branch edits are the conflict
            # scan's to refuse.
            continue
        if b_ev is None:
            # The branch deleted THIS row — the very main namesake its copy
            # came from, not whichever shares the key.
            if base_event is not None and slot.branch_known:
                doomed_main_events.append(m_ev)
            continue
        if (
            base_event is not None
            and branch_event_key(b_ev) != base_event_key(base_event)
            and main_event_key(m_ev) == base_event_key(base_event)
        ):
            m_ev.name = b_ev.name
        landings.append((b_ev, m_ev, base_event))
        branch_event_snapshot = branch_event_snapshot_by_id[str(b_ev.id)]
        for attr in event_attrs:
            if main_keeps(attr, base_event, getattr(m_ev, attr)):
                continue
            if base_event is None or branch_event_snapshot.get(attr) != base_event.get(attr):
                branch_value = getattr(b_ev, attr)
                if attr == "metric_breakdown_columns":
                    branch_value = list(branch_value or [])
                setattr(m_ev, attr, branch_value)
        if base_event is None or branch_event_snapshot.get("status") != base_event.get("status"):
            b_status = _ES(b_ev.status)
            m_status = _ES(m_ev.status)
            if b_status != _ES.archived:
                m_ev.status = b_status if _rank(b_status) >= _rank(m_status) else m_status

        if base_event is None or branch_event_snapshot.get("field_values") != base_event.get(
            "field_values"
        ):
            await session.execute(
                delete(EventFieldValue).where(EventFieldValue.event_id == m_ev.id)
            )
            for fv in b_ev.field_values:
                bf_et, bf_name = branch_field_by_id[fv.field_definition_id]
                session.add(
                    EventFieldValue(
                        id=uuid.uuid4(),
                        event_id=m_ev.id,
                        field_definition_id=main_field_by_key[(bf_et, bf_name)],
                        value=fv.value,
                        is_authored=fv.is_authored,
                    )
                )
        if base_event is None or branch_event_snapshot.get("meta_values") != base_event.get(
            "meta_values"
        ):
            await session.execute(delete(EventMetaValue).where(EventMetaValue.event_id == m_ev.id))
            for mv in b_ev.meta_values:
                session.add(
                    EventMetaValue(
                        id=uuid.uuid4(),
                        event_id=m_ev.id,
                        meta_field_definition_id=main_meta_field_id(b_ev, mv),
                        value=mv.value,
                    )
                )
        if base_event is None or branch_event_snapshot.get("tags") != base_event.get("tags"):
            await session.execute(delete(EventTag).where(EventTag.event_id == m_ev.id))
            for tag in b_ev.tags:
                session.add(EventTag(id=uuid.uuid4(), event_id=m_ev.id, name=tag.name))

    for b_ev in event_slots.created:
        et_name = branch_et_id_to_name[b_ev.event_type_id]
        if et_name not in main_et_name_to_id:
            continue
        new_ev_id = uuid.uuid4()
        created_event = Event(
            id=new_ev_id,
            project_id=project_id,
            branch_id=main_branch_id,
            event_type_id=main_et_name_to_id[et_name],
            name=b_ev.name,
            title=b_ev.title,
            source_name=b_ev.source_name,
            description=b_ev.description,
            order=b_ev.order,
            status=b_ev.status,
            sunset_at=b_ev.sunset_at,
            last_seen_at=b_ev.last_seen_at,
            metric_breakdown_columns=list(b_ev.metric_breakdown_columns or []),
            required_presence_threshold=b_ev.required_presence_threshold,
            owner_id=b_ev.owner_id,
            reviewed=b_ev.reviewed,
            # superseded_by_event_id is deliberately absent: the value on
            # the branch row is a BRANCH event id, meaningless on main.
            # The translating pass after the flush below sets it.
        )
        session.add(created_event)
        landings.append((b_ev, created_event, None))
        for fv in b_ev.field_values:
            bf_et, bf_name = branch_field_by_id[fv.field_definition_id]
            session.add(
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=new_ev_id,
                    field_definition_id=main_field_by_key[(bf_et, bf_name)],
                    value=fv.value,
                    is_authored=fv.is_authored,
                )
            )
        for mv in b_ev.meta_values:
            session.add(
                EventMetaValue(
                    id=uuid.uuid4(),
                    event_id=new_ev_id,
                    meta_field_definition_id=main_meta_field_id(b_ev, mv),
                    value=mv.value,
                )
            )
        for tag in b_ev.tags:
            session.add(EventTag(id=uuid.uuid4(), event_id=new_ev_id, name=tag.name))
    main_target_by_branch_id: dict[uuid.UUID, Event] = {
        b_ev.id: target for b_ev, target, _ in landings
    }
    # Collect, clear, then delete. These are main events the branch removed on
    # purpose, so again there is no survivor and the rule is DROP.
    if doomed_main_events:
        doomed_main_event_ids = [m_ev.id for m_ev in doomed_main_events]
        released_blobs |= await _blob_keys_of(session, doomed_main_event_ids)
        await drop_dangling_event_references(
            session,
            project_id=project_id,
            event_ids=doomed_main_event_ids,
        )
    for m_ev in doomed_main_events:
        await session.delete(m_ev)
    await session.flush()

    # The successor pointer, translated rather than copied. `event_attrs` above
    # copies raw ORM values, which for this column would write a BRANCH event id
    # onto a main row; the snapshot carries the successor as a natural key for
    # exactly that reason, so the branch's own pointer is re-resolved against
    # main here — through the landings, so a successor with a namesake resolves
    # to the main row ITS copy landed on rather than the last row under the key.
    # It runs after the flush because a successor may be an
    # event this very merge created, and because the FK is immediate.
    for b_ev, target, base_event in landings:
        snapshot = branch_event_snapshot_by_id.get(str(b_ev.id), {})
        # Untouched on the branch: leave main's own answer alone, the same
        # three-way rule every attribute above follows.
        if base_event is not None and snapshot.get("superseded_by") == base_event.get(
            "superseded_by"
        ):
            continue
        successor = (
            main_target_by_branch_id.get(b_ev.superseded_by_event_id)
            if b_ev.superseded_by_event_id is not None
            else None
        )
        # A successor the merge cannot place on main clears the pointer rather
        # than leaving a branch id behind: "replaced by something that is not
        # here" is not a fact worth keeping.
        target.superseded_by_event_id = successor.id if successor is not None else None

    # --- photos + comments: replace only when the branch's design canvas
    # changed from the base. storage_key/external_url is reused — no blob copies.
    # Doomed rows go by one bulk delete that never touches storage, and must not
    # inside this transaction: a merge failing after it rolls the rows back,
    # and they would come back pointing at an object already gone. But a doomed
    # row gets no replacement — ``_photo_identity`` includes the key, and one
    # identity is never both doomed and added — so it can be the key's LAST
    # holder: a screenshot deleted on the branch leaves its blob to main's row,
    # and deleting that row here used to strand the object
    # for good. Its key is collected instead, and ``merge_branch`` deletes the
    # blob after the commit unless some row still holds it.
    main_events_after = list(
        (
            await session.execute(
                select(Event).where(
                    Event.project_id == project_id, Event.branch_id == main_branch_id
                )
            )
        )
        .scalars()
        .all()
    )
    surviving_main_ids = {e.id for e in main_events_after}

    for b_ev, target, base_event in landings:
        main_ev_id = target.id
        if main_ev_id not in surviving_main_ids:
            continue
        branch_event_snapshot = branch_event_snapshot_by_id[str(b_ev.id)]
        if base_event is not None and branch_event_snapshot.get("photos") == base_event.get(
            "photos"
        ):
            continue
        branch_photos = list(
            (
                await session.execute(
                    select(EventPhoto)
                    .where(EventPhoto.event_id == b_ev.id)
                    .order_by(EventPhoto.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        main_photos = list(
            (
                await session.execute(
                    select(EventPhoto)
                    .where(EventPhoto.event_id == main_ev_id)
                    .order_by(EventPhoto.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        # Pair the two sides up by what each attachment IS, rather than
        # replacing main's whole canvas, and decide every
        # attachment three-way against the base. The gate above compares the
        # raw subtree, comments included, so this block also runs when the
        # branch only talked about a photo — and then the branch's photo SET
        # equals the base's, which must leave main's own additions, deletions
        # and moves after the cut exactly as they are.
        base_photos_by_identity: dict[tuple[object, ...], list[dict[str, Any]]] = {}
        for base_photo in (base_event or {}).get("photos") or []:
            if isinstance(base_photo, dict):
                base_photos_by_identity.setdefault(_snapshot_photo_identity(base_photo), []).append(
                    base_photo
                )
        branch_by_identity: dict[tuple[object, ...], list[EventPhoto]] = {}
        for bp in branch_photos:
            branch_by_identity.setdefault(_photo_identity(bp), []).append(bp)
        main_by_identity: dict[tuple[object, ...], list[EventPhoto]] = {}
        for m_ph in main_photos:
            main_by_identity.setdefault(_photo_identity(m_ph), []).append(m_ph)

        kept_pairs: list[tuple[EventPhoto, EventPhoto]] = []
        added_pairs: list[tuple[uuid.UUID, EventPhoto]] = []
        doomed_photo_ids: list[uuid.UUID] = []
        base_thread_by_source_id: dict[uuid.UUID, tuple[set[tuple[object, ...]], bool]] = {}
        for identity in {**branch_by_identity, **main_by_identity}:
            b_rows = branch_by_identity.get(identity, [])
            m_rows = main_by_identity.get(identity, [])
            base_rows = base_photos_by_identity.get(identity, [])
            keep = _three_way_count(base=len(base_rows), ours=len(m_rows), theirs=len(b_rows))
            pairs, added, doomed_rows = _split_identity_rows(m_rows, b_rows, keep)
            doomed_photo_ids.extend(m_ph.id for m_ph in doomed_rows)
            for m_ph in doomed_rows:
                if m_ph.kind == PHOTO_KIND_PHOTO and m_ph.storage_backend and m_ph.storage_key:
                    released_blobs.add(
                        (str(m_ph.storage_backend), m_ph.storage_key, m_ph.storage_config_id)
                    )
            base_sort_orders = {base_photo.get("sort_order") for base_photo in base_rows}
            for main_photo, bp in pairs:
                # Position is left out of the identity so that re-ordering a
                # canvas does not read as "different attachment"; it is
                # carried over only when the branch is the side that moved it.
                if bp.sort_order not in base_sort_orders:
                    main_photo.sort_order = bp.sort_order
            kept_pairs.extend(pairs)
            added_pairs.extend((uuid.uuid4(), bp) for bp in added)
            for bp in b_rows:
                base_thread_by_source_id[bp.id] = _base_thread_for(bp, base_rows)

        if doomed_photo_ids:
            await session.execute(delete(EventPhoto).where(EventPhoto.id.in_(doomed_photo_ids)))
            await session.flush()

        for new_ph_id, bp in added_pairs:
            session.add(
                EventPhoto(
                    id=new_ph_id,
                    project_id=project_id,
                    event_id=main_ev_id,
                    uploaded_by_user_id=bp.uploaded_by_user_id,
                    original_filename=bp.original_filename,
                    content_type=bp.content_type,
                    size_bytes=bp.size_bytes,
                    kind=bp.kind,
                    external_url=bp.external_url,
                    storage_backend=bp.storage_backend,
                    storage_key=bp.storage_key,
                    storage_org_id=bp.storage_org_id,
                    storage_config_id=bp.storage_config_id,
                    sort_order=bp.sort_order,
                )
            )
        await session.flush()

        for target_photo_id, source_photo in [
            *((main_photo.id, bp) for main_photo, bp in kept_pairs),
            *((new_ph_id, bp) for new_ph_id, bp in added_pairs),
        ]:
            await _merge_photo_comments(
                session,
                target_photo_id=target_photo_id,
                source_photo_id=source_photo.id,
                base_thread=base_thread_by_source_id.get(source_photo.id, (set(), False)),
            )
        await session.flush()

    # --- the event's own discussion: not plan content, so ungated by any diff.
    # The twin the branch row reads through today when the merge kept it, else
    # the main row the branch row itself landed on. A row that landed nowhere —
    # main deleted it, or it is one of several namesakes nothing tells apart —
    # keeps its thread, as one whose event main deleted always has. The landing
    # is the slot the attribute writes used, so a thread about one namesake can
    # no longer move onto the other.
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

    # --- variable event value overrides: replace only for variables whose
    # branch-side override map changed from the base. Each override follows its
    # branch event to the main row that event landed on, not to whichever main
    # row shares the event's (type, name).
    main_vars_after = await _load_variables(session, project_id, main_branch_id)
    main_var_name_to_id = {v.name: v.id for v in main_vars_after}
    branch_overrides = await _load_for_branch(
        session, VariableEventValueOverride, project_id, branch_id
    )
    branch_overrides_by_var: dict[uuid.UUID, list[VariableEventValueOverride]] = {}
    for override in branch_overrides:
        branch_overrides_by_var.setdefault(override.variable_id, []).append(override)
    branch_var_snapshot_by_name = {
        variable["name"]: variable for variable in branch_snapshot_payload.get("variables", [])
    }
    for branch_var in branch_vars:
        name = branch_var.name
        base_var = base_var_by_name.get(name)
        branch_var_snapshot = branch_var_snapshot_by_name[name]
        if base_var is not None and branch_var_snapshot.get(
            "event_value_overrides"
        ) == base_var.get("event_value_overrides"):
            continue
        main_var_id = main_var_name_to_id.get(name)
        if main_var_id is None:
            continue
        await session.execute(
            delete(VariableEventValueOverride).where(
                VariableEventValueOverride.project_id == project_id,
                VariableEventValueOverride.branch_id == main_branch_id,
                VariableEventValueOverride.variable_id == main_var_id,
            )
        )
        for override in branch_overrides_by_var.get(branch_var.id, []):
            landed = main_target_by_branch_id.get(override.event_id)
            if landed is None or landed.id not in surviving_main_ids:
                continue
            session.add(
                VariableEventValueOverride(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    branch_id=main_branch_id,
                    variable_id=main_var_id,
                    event_id=landed.id,
                    values=copy_override_values(override.values),
                    required=override.required,
                )
            )
    await session.flush()

    # --- relations: three-way apply, row by row. Paired like the events above:
    # main's by their own ids, the branch's by origin id, the natural key only
    # for rows neither places. Nothing makes a relation's four names unique
    # either.
    branch_relations = await _load_for_branch(session, EventTypeRelation, project_id, branch_id)
    branch_fd_id_to_key = {
        fd.id: (branch_et_id_to_name[fd.event_type_id], fd.name)
        for et in branch_ets
        for fd in et.field_definitions
    }
    main_relations = await _load_for_branch(session, EventTypeRelation, project_id, main_branch_id)
    main_et_id_to_name_after = {et_id: name for name, et_id in main_et_name_to_id.items()}
    main_fd_id_to_key = {field_id: key for key, field_id in main_field_by_key.items()}

    def relation_key(
        relation: EventTypeRelation,
        et_names: dict[uuid.UUID, str],
        field_keys: dict[uuid.UUID, tuple[str, str]],
    ) -> tuple[str, str, str, str]:
        source_field = field_keys[relation.source_field_id]
        target_field = field_keys[relation.target_field_id]
        return (
            et_names[relation.source_event_type_id],
            source_field[1],
            et_names[relation.target_event_type_id],
            target_field[1],
        )

    relation_slots = merge_slots(
        list((base_payload or {}).get("relations", [])),
        [
            relation
            for relation in main_relations
            # ``relation_key`` indexes both maps directly, so a relation naming
            # an end that is not on this side was a KeyError — a 500 on the
            # merge with nothing saying which relation.
            # ``relation_service.create_relation`` now refuses to write one, but
            # rows stored before that refusal have no migration sweeping them,
            # and the merge is where they surface. Main needs the event-type half
            # as much as the branch does: ``main_et_id_to_name_after`` covers
            # MAIN's types only, and the four ids were never checked against
            # each other, so a main relation can hold a branch copy's type id
            # beside a main field id.
            if relation.source_field_id in main_fd_id_to_key
            and relation.target_field_id in main_fd_id_to_key
            and relation.source_event_type_id in main_et_id_to_name_after
            and relation.target_event_type_id in main_et_id_to_name_after
        ],
        [
            relation
            for relation in branch_relations
            # Guarded the same way main's are above, and for the same reason.
            if relation.source_field_id in branch_fd_id_to_key
            and relation.target_field_id in branch_fd_id_to_key
            and relation.source_event_type_id in branch_et_id_to_name
            and relation.target_event_type_id in branch_et_id_to_name
        ],
        base_key=lambda relation: (
            relation["source_event_type_name"],
            relation["source_field_name"],
            relation["target_event_type_name"],
            relation["target_field_name"],
        ),
        main_key=lambda relation: relation_key(
            relation, main_et_id_to_name_after, main_fd_id_to_key
        ),
        branch_key=lambda relation: relation_key(
            relation, branch_et_id_to_name, branch_fd_id_to_key
        ),
        main_ref=lambda relation: str(relation.id),
        branch_ref=lambda relation: str(relation.origin_id or relation.id),
        # A relation whose ends moved is carried as a removal plus an addition,
        # as the natural-key merge always carried it.
        follows_rename=lambda old_key, new_key: False,
        branch_origins_complete=branch_origins_complete,
    )
    for relation_slot in relation_slots.slots:
        m_rel, b_rel, base_relation = (
            relation_slot.main,
            relation_slot.branch,
            relation_slot.base,
        )
        if m_rel is None:
            continue
        if b_rel is None:
            if base_relation is not None and relation_slot.branch_known:
                await session.delete(m_rel)
            continue
        if base_relation is None or b_rel.relation_type != base_relation.get("relation_type"):
            m_rel.relation_type = b_rel.relation_type
        if base_relation is None or b_rel.description != base_relation.get("description"):
            m_rel.description = b_rel.description
    for b_rel in relation_slots.created:
        src_et_name, _src_field_name, tgt_et_name, _tgt_field_name = relation_key(
            b_rel, branch_et_id_to_name, branch_fd_id_to_key
        )
        session.add(
            EventTypeRelation(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=main_branch_id,
                source_event_type_id=main_et_name_to_id[src_et_name],
                target_event_type_id=main_et_name_to_id[tgt_et_name],
                source_field_id=main_field_by_key[branch_fd_id_to_key[b_rel.source_field_id]],
                target_field_id=main_field_by_key[branch_fd_id_to_key[b_rel.target_field_id]],
                relation_type=b_rel.relation_type,
                description=b_rel.description,
            )
        )
    return frozenset(released_blobs)


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
    ``_apply_merge`` says why that is the better failure) — or one we have not
    modelled. It is still the user's merge that cannot proceed, and 409 says
    that; the constraint's own text stays in the log, where an operator can
    read it against the request id, rather than in a response body that would
    leak the schema.
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
