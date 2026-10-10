"""The merge's photos arm: each landed event's attachments and their discussion.

An event's design canvas is touched only when the branch's changed from the
base, and then attachment by attachment, three-way. ``storage_key`` and
``external_url`` are reused — no blob copies. Doomed rows go by one bulk delete
that never touches storage, and must not inside this transaction: a merge
failing after it rolls the rows back, and they would come back pointing at an
object already gone. But a doomed row gets no replacement — ``_photo_identity``
includes the key, and one identity is never both doomed and added — so it can
be the key's LAST holder: a screenshot deleted on the branch leaves its blob to
main's row, and deleting that row here used to strand the object for good. Its
key is collected instead (``MergeContext.released_blobs``), and
``merge_branch`` deletes the blob after the commit unless some row still holds
it.

Nothing here commits.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.services._plan_branch_merge_state import MergeContext, MergedEvents
from tripl.services.event_photo_service import PHOTO_KIND_PHOTO
from tripl.services.plan_revision_service import _snapshot_fingerprint


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


async def apply_photos(ctx: MergeContext, events: MergedEvents) -> None:
    """Merge each changed canvas of a branch event that landed on a surviving main row."""
    session, project_id = ctx.session, ctx.project_id
    surviving_main_ids = events.surviving_main_ids
    branch_event_snapshot_by_id = events.branch_event_snapshot_by_id
    for b_ev, target, base_event in events.landings:
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
                    ctx.released_blobs.add(
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
