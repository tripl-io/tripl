"""Plan branch lifecycle: main-branch resolution, branch CRUD, deep-copy.

A project's tracking plan lives on its single ``kind="main"`` PlanBranch. Creating
a working branch deep-copies every design-time entity from main into the new branch
(fresh ids, FK remap) so edits are isolated. ``main`` is a real row (never NULL) so
the ``(project_id, branch_id, name)`` unique constraints are enforced on the live plan.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.plan_branch_approval import PlanBranchApproval
from tripl.models.plan_branch_comment import PlanBranchComment
from tripl.models.plan_branch_merge_resolution import PlanBranchMergeResolution
from tripl.models.plan_branch_reviewer import PlanBranchReviewer
from tripl.models.plan_revision import PlanRevision, PlanRevisionKind
from tripl.models.project import Project
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue
from tripl.schemas.plan_branch import (
    BranchCommentCreate,
    BranchCommentResponse,
    BranchReviewerCreate,
    BranchReviewerResponse,
    BranchTransitionAction,
    PlanBranchCreate,
    PlanBranchDetailResponse,
    PlanBranchDiff,
    PlanBranchList,
    PlanBranchResponse,
    PlanDiffRename,
)
from tripl.services import (
    notification_announce,
    notification_service,
    subscription_service,
)
from tripl.services._event_reference_cleanup import drop_dangling_event_references
from tripl.services._plan_branch_renames import snapshot_rename_pairs
from tripl.services._plan_diff_housekeeping import mark_housekeeping, reviewable
from tripl.services._plan_diff_warnings import attach_identity_warnings
from tripl.services.mentions import excerpt
from tripl.services.plan_revision_service import (
    build_plan_snapshot,
    compute_plan_diff_entries,
    plan_snapshot_hash,
)
from tripl.services.project_access import member_role
from tripl.services.project_branch_settings_service import read_branch_merge_policy
from tripl.services.project_member_service import NOT_A_MEMBER_DETAIL

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import IsolationLevel

MAIN_BRANCH_NAME = "main"

# action -> (allowed source states, target state)
_TRANSITIONS: dict[str, tuple[set[str], str]] = {
    "submit": (
        {BranchStatus.draft.value, BranchStatus.changes_requested.value},
        BranchStatus.ready_for_review.value,
    ),
    "request_changes": (
        {BranchStatus.ready_for_review.value, BranchStatus.approved.value},
        BranchStatus.changes_requested.value,
    ),
    # approve is also legal from "approved" so further reviewers can stack
    # approvals toward the project's min_approvals quota — the first approve
    # flips the status, later ones only add PlanBranchApproval rows.
    "approve": (
        {BranchStatus.ready_for_review.value, BranchStatus.approved.value},
        BranchStatus.approved.value,
    ),
    "reopen": (
        {
            BranchStatus.approved.value,
            BranchStatus.changes_requested.value,
            BranchStatus.closed.value,
        },
        BranchStatus.draft.value,
    ),
    "close": (
        {
            BranchStatus.draft.value,
            BranchStatus.ready_for_review.value,
            BranchStatus.changes_requested.value,
            BranchStatus.approved.value,
        },
        BranchStatus.closed.value,
    ),
}
# Transitions that invalidate prior approvals (fresh review needed).
_APPROVAL_CLEARING_ACTIONS = {"submit", "request_changes", "reopen"}

# The branches ``include_diff_counts`` counts: every status a review can still
# move. Merged and closed are settled, so they are left out — see
# ``_diff_counts_for_branches`` for why (tripl-0zpq.152).
_DIFF_COUNTED_STATUSES = frozenset(
    {
        BranchStatus.draft.value,
        BranchStatus.ready_for_review.value,
        BranchStatus.changes_requested.value,
        BranchStatus.approved.value,
    }
)

# The isolation under which every read of one transaction sees ONE committed
# state, per dialect (tripl-0zpq.153). Postgres needs it: its default READ
# COMMITTED gives each statement a fresh snapshot. SQLite, the test database, is
# deliberately absent. SQLAlchemy has no REPEATABLE READ for it, pysqlite does not
# honour its SERIALIZABLE for reads (it emits no BEGIN before a SELECT), and the
# suite runs every session on one connection, where no other writer can
# interleave.
_CONSISTENT_READ_ISOLATION: dict[str, IsolationLevel] = {"postgresql": "REPEATABLE READ"}

# The SQLSTATE of a transaction the database aborted as unserializable. Under
# the level above, ``create_branch`` retries it rather than answering 500 —
# see the comment there for when it happens (tripl-0zpq.153).
_SERIALIZATION_FAILURE_SQLSTATE = "40001"
_UNIQUE_VIOLATION_SQLSTATE = "23505"
_BRANCH_NAME_CONSTRAINT = "uq_plan_branch_project_name"
# Attempts ``create_branch`` makes before it asks the client to retry. Each one
# takes a fresh snapshot, and one that already sees the conflicting commit
# cannot collide with it, so a second attempt normally lands; the third covers
# two conflicting commits arriving back to back.
_CREATE_BRANCH_ATTEMPTS = 3


async def ensure_main_branch_id(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID:
    """Return the project's main branch id, creating it on first use.

    Resolved lazily (rather than only at project creation) so every code path —
    including projects seeded directly via the ORM in tests — gets a valid
    ``branch_id`` for the live plan. Flushes (not commits) so it joins the
    caller's transaction.
    """
    existing = await session.scalar(
        select(PlanBranch.id).where(
            PlanBranch.project_id == project_id,
            PlanBranch.kind == BranchKind.main.value,
        )
    )
    if existing is not None:
        return existing
    branch = PlanBranch(
        project_id=project_id,
        name=MAIN_BRANCH_NAME,
        kind=BranchKind.main.value,
        status=BranchStatus.merged.value,
    )
    session.add(branch)
    await session.flush()
    return branch.id


async def resolve_branch_id(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id_override: uuid.UUID | None = None,
) -> uuid.UUID:
    """Resolve which branch a service call should act on.

    Service code passes the editor's optional override through (``None`` =
    operate on main). Defence in depth: even though the API dep already
    validates the override against the slug, the service re-checks ownership so
    non-HTTP callers (workers, tests) can't slip into the wrong project.
    """
    if branch_id_override is None:
        return await ensure_main_branch_id(session, project_id)
    branch = await session.get(PlanBranch, branch_id_override)
    if branch is None or branch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Branch not found")
    return branch.id


async def _resolve_project(session: AsyncSession, slug: str) -> Project:
    """Slug -> Project entity. Kept for ``plan_branch_conflicts`` and
    ``plan_branch_merge_service``, which import it; every caller in THIS module
    uses :func:`_resolve_project_id` instead."""
    project = await session.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _resolve_project_id(session: AsyncSession, slug: str) -> uuid.UUID:
    """Resolve a slug to a project id without materialising the Project entity.

    Every branch endpoint only ever needed ``project.id``, but selecting the
    entity used to drag the project's whole plan in with it (tripl-jfm3.54), so
    ``GET /branches`` cost 559 ms on the largest project against 84 ms on the
    smallest. Selecting the single indexed column keeps it flat.
    """
    project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
    if project_id is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project_id


async def _load_for_branch(
    session: AsyncSession,
    model: Any,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
) -> list[Any]:
    """SELECT all rows of ``model`` for ``(project_id, branch_id)`` as a list.

    Compact form of the ``select(...).where(project_id, branch_id)`` pattern
    used throughout deep-copy and merge. Joined-loaded selects (those that
    need ``selectinload``) stay inline because they want options on the
    statement that vary per call.
    """
    rows = await session.execute(
        select(model).where(model.project_id == project_id, model.branch_id == branch_id)
    )
    return list(rows.scalars().all())


def _to_response(branch: PlanBranch) -> PlanBranchResponse:
    return PlanBranchResponse.model_validate(branch)


async def _diff_counts_for_branches(
    session: AsyncSession,
    project_id: uuid.UUID,
    main_branch_id: uuid.UUID,
    branches: list[PlanBranch],
) -> dict[uuid.UUID, tuple[int, bool]]:
    """ahead/behind for every OPEN feature branch, off ONE main snapshot.

    Two callers ask for these: ``tripl plan branches`` and the Branches tab,
    whose row badges read them instead of firing one ``/branches/{id}/diff``
    per row (PLAN-3). ``ahead`` is the reviewable total of ``diff_branch``'s
    ``summary`` PAIRED the way the Changes list pairs it: each rename the merge
    will apply (``snapshot_rename_pairs``, the list ``diff_branch`` returns as
    ``renames``) is ONE change, not its removal plus its addition. Without that,
    a branch whose only change is a rename read "2 ahead" in the list and one
    change in the branch's own view (tripl-amnn). A legacy branch with no base
    snapshot has no pairing, as in ``diff_branch``.

    Per-row diffs are what this saves: a diff is two plan snapshots, a snapshot
    is essentially the whole cost of the call (measured 507 ms main + 157 ms
    branch of a 484 ms diff on a 1300-variable project), and every row rebuilt
    main's snapshot again. Here main is built once and each counted branch
    once — N+1 snapshots instead of 2N, in one HTTP call.

    Only open branches are counted (tripl-0zpq.152). A merge deletes only
    main-side rows and closing deletes nothing, so a merged or closed branch
    keeps its whole deep copy for good, and counting every non-main row cost one
    snapshot per branch in the project's HISTORY. That crossed the CLI's 10 s
    default timeout once a few dozen had piled up, for numbers that meant
    nothing: a merged branch has landed, and its "behind base" only says main
    moved on since. Those rows keep ``ahead`` / ``behind_base`` at ``None``, the
    same "not computed" the main row carries. A reopened branch is a draft again
    and is counted from then on.
    """
    feature_branches = [
        b
        for b in branches
        if b.kind != BranchKind.main.value and b.status in _DIFF_COUNTED_STATUSES
    ]
    if not feature_branches:
        return {}

    main_snapshot = await build_plan_snapshot(session, project_id, branch_id=main_branch_id)
    counts: dict[uuid.UUID, tuple[int, bool]] = {}
    for branch in feature_branches:
        branch_snapshot = await build_plan_snapshot(session, project_id, branch_id=branch.id)
        base_payload: dict[str, Any] | None = None
        if branch.base_revision_id is not None:
            base_revision = await session.get(PlanRevision, branch.base_revision_id)
            if base_revision is not None:
                base_payload = base_revision.payload or {}
        # The badge counts what the reviewer will read, so housekeeping
        # entries are left out of it exactly as ``diff_branch`` leaves them
        # out of ``summary`` (tripl-kjhi.12).
        if base_payload is None:
            # Legacy branch with no base snapshot: same fallback as diff_branch —
            # it cannot tell branch-authored changes from later main changes.
            legacy_entries = compute_plan_diff_entries(main_snapshot, branch_snapshot)
            mark_housekeeping(legacy_entries, main_payload=main_snapshot)
            counts[branch.id] = (len(reviewable(legacy_entries)), False)
            continue
        ahead_entries = compute_plan_diff_entries(
            base_payload,
            branch_snapshot,
            key_collisions_from=main_snapshot,
            origins_complete=branch.origin_ids_complete,
        )
        behind_entries = compute_plan_diff_entries(
            base_payload, main_snapshot, origins_complete=True
        )
        mark_housekeeping(ahead_entries, behind_entries=behind_entries, main_payload=main_snapshot)
        # Each pair is one added + one removed reviewable entry, so this cannot
        # go below zero; ``max`` only guards a pairing that ever disagrees.
        renamed = len(snapshot_rename_pairs(base_payload, main_snapshot, branch_snapshot))
        ahead = max(len(reviewable(ahead_entries)) - renamed, 0)
        counts[branch.id] = (ahead, len(behind_entries) > 0)
    return counts


async def list_branches(
    session: AsyncSession,
    slug: str,
    *,
    include_diff_counts: bool = False,
) -> PlanBranchList:
    project_id = await _resolve_project_id(session, slug)
    main_branch_id = await ensure_main_branch_id(session, project_id)
    await session.commit()
    rows = (
        (
            await session.execute(
                select(PlanBranch)
                .where(PlanBranch.project_id == project_id)
                # main first, then most-recent working branches.
                .order_by(PlanBranch.kind.desc(), PlanBranch.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    total = (
        await session.execute(
            select(func.count(PlanBranch.id)).where(PlanBranch.project_id == project_id)
        )
    ).scalar_one()
    items = [_to_response(b) for b in rows]
    if include_diff_counts:
        counts = await _diff_counts_for_branches(session, project_id, main_branch_id, list(rows))
        items = [
            item.model_copy(update={"ahead": counts[item.id][0], "behind_base": counts[item.id][1]})
            if item.id in counts
            else item
            for item in items
        ]
    return PlanBranchList(items=items, total=total)


async def _get_branch(
    session: AsyncSession, project_id: uuid.UUID, branch_id: uuid.UUID
) -> PlanBranch:
    branch = await session.get(PlanBranch, branch_id)
    if branch is None or branch.project_id != project_id:
        raise HTTPException(status_code=404, detail="Branch not found")
    return branch


async def _load_reviewers(session: AsyncSession, branch_id: uuid.UUID) -> list[PlanBranchReviewer]:
    return list(
        (
            await session.execute(
                select(PlanBranchReviewer)
                .where(PlanBranchReviewer.branch_id == branch_id)
                .order_by(PlanBranchReviewer.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _load_approvals(session: AsyncSession, branch_id: uuid.UUID) -> list[PlanBranchApproval]:
    return list(
        (
            await session.execute(
                select(PlanBranchApproval)
                .where(PlanBranchApproval.branch_id == branch_id)
                .order_by(PlanBranchApproval.approved_at)
            )
        )
        .scalars()
        .all()
    )


async def _to_detail(session: AsyncSession, branch: PlanBranch) -> PlanBranchDetailResponse:
    reviewers = await _load_reviewers(session, branch.id)
    approvals = await _load_approvals(session, branch.id)
    base = _to_response(branch)
    # Staleness has to travel with the approval, because it is the only thing
    # the merge gate actually counts (plan_branch_merge_service.
    # _load_fresh_approver_ids). While this flag did not exist the UI could do
    # no better than count rows — so a branch whose single approval had gone
    # stale showed a green "Approvals 1/1" and then failed to merge with
    # current=0, and no screen in the product explained the contradiction.
    #
    # The snapshot build is the expensive part of this call, so it is skipped
    # entirely when there is nothing to compare: an unapproved branch cannot
    # have a stale approval.
    current_hash: str | None = None
    if approvals:
        current_hash = plan_snapshot_hash(
            await build_plan_snapshot(session, branch.project_id, branch_id=branch.id)
        )
    return PlanBranchDetailResponse(
        **base.model_dump(),
        reviewers=[BranchReviewerResponse.model_validate(r) for r in reviewers],
        approvals=[
            {
                "user_id": a.user_id,
                "approved_at": a.approved_at,
                # A NULL plan_hash (approved before f8a9b0c1d2e3) never equals a
                # digest, so legacy rows report stale — exactly how the merge
                # gate scores them.
                "stale": a.plan_hash != current_hash,
            }
            for a in approvals
        ],
    )


async def get_branch(
    session: AsyncSession, slug: str, branch_id: uuid.UUID
) -> PlanBranchDetailResponse:
    project_id = await _resolve_project_id(session, slug)
    branch = await _get_branch(session, project_id, branch_id)
    return await _to_detail(session, branch)


async def deep_copy_plan_to_branch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    source_branch_id: uuid.UUID,
    target_branch_id: uuid.UUID,
) -> None:
    """Copy every design-time entity from source branch into target branch.

    New ids are minted up front, so almost no FK remap has to wait for the
    database. The two exceptions each cost a flush. Photo comments go in after
    their photos, because no ORM relationship orders that insert (see below).
    ``superseded_by_event_id`` is remapped only once the copied events exist,
    because the unit of work will not order two rows of one table by a raw FK
    column.
    Child tables (field_definitions, event_field_values, event_meta_values,
    event_tags) inherit their branch from the parent and carry no branch_id.

    Flushes but never commits, so it runs inside a caller-owned transaction —
    ``create_branch`` below and the demo seeder's branches builder both reuse
    it. There are two flushes: the photo-ordering one (only when the source has
    photos), which writes every row queued so far, and the final one once every
    row is queued. Either also writes whatever the caller left pending, which
    the first SELECT here already does under autoflush, so a caller must not
    hold unflushable objects across this call. The ``superseded_by_event_id``
    remap set after the final flush stays pending until the caller's next flush
    or commit (tripl-0zpq.156).
    """
    event_types = (
        (
            await session.execute(
                select(EventType)
                .where(
                    EventType.project_id == project_id,
                    EventType.branch_id == source_branch_id,
                )
                .options(selectinload(EventType.field_definitions))
            )
        )
        .scalars()
        .all()
    )
    et_map: dict[uuid.UUID, uuid.UUID] = {}
    fd_map: dict[uuid.UUID, uuid.UUID] = {}
    new_objs: list[object] = []
    for et in event_types:
        new_et_id = uuid.uuid4()
        et_map[et.id] = new_et_id
        new_objs.append(
            EventType(
                id=new_et_id,
                project_id=project_id,
                branch_id=target_branch_id,
                name=et.name,
                display_name=et.display_name,
                description=et.description,
                color=et.color,
                order=et.order,
            )
        )
        for fd in et.field_definitions:
            new_fd_id = uuid.uuid4()
            fd_map[fd.id] = new_fd_id
            new_objs.append(
                FieldDefinition(
                    id=new_fd_id,
                    event_type_id=new_et_id,
                    name=fd.name,
                    display_name=fd.display_name,
                    field_type=fd.field_type,
                    is_required=fd.is_required,
                    enum_options=list(fd.enum_options) if fd.enum_options else None,
                    description=fd.description,
                    order=fd.order,
                    sensitivity=fd.sensitivity,
                    contract_required_max_null_rate=fd.contract_required_max_null_rate,
                    contract_regex=fd.contract_regex,
                    contract_min_value=fd.contract_min_value,
                    contract_max_value=fd.contract_max_value,
                    contract_max_bad_rate=fd.contract_max_bad_rate or 0.0,
                )
            )

    meta_fields = await _load_for_branch(session, MetaFieldDefinition, project_id, source_branch_id)
    mf_map: dict[uuid.UUID, uuid.UUID] = {}
    for mf in meta_fields:
        new_mf_id = uuid.uuid4()
        mf_map[mf.id] = new_mf_id
        new_objs.append(
            MetaFieldDefinition(
                id=new_mf_id,
                project_id=project_id,
                branch_id=target_branch_id,
                name=mf.name,
                display_name=mf.display_name,
                field_type=mf.field_type,
                is_required=mf.is_required,
                allow_multiple=mf.allow_multiple,
                enum_options=list(mf.enum_options) if mf.enum_options else None,
                default_value=mf.default_value,
                link_template=mf.link_template,
                order=mf.order,
                sensitivity=mf.sensitivity,
            )
        )

    variables = await _load_for_branch(session, Variable, project_id, source_branch_id)
    var_map: dict[uuid.UUID, uuid.UUID] = {}
    for var in variables:
        new_var_id = uuid.uuid4()
        var_map[var.id] = new_var_id
        new_objs.append(
            Variable(
                id=new_var_id,
                project_id=project_id,
                branch_id=target_branch_id,
                name=var.name,
                source_name=var.source_name,
                variable_type=var.variable_type,
                description=var.description,
                allowed_values=list(var.allowed_values or []),
                bindings=list(var.bindings or []),
                excluded_from_scans=var.excluded_from_scans,
            )
        )

    events = (
        (
            await session.execute(
                select(Event)
                .where(Event.project_id == project_id, Event.branch_id == source_branch_id)
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
    # Built in full BEFORE the loop, not filled in during it. Every other id map
    # here can be built as it goes because it maps a DIFFERENT entity, already
    # copied; this one maps events to events, and `superseded_by_event_id` can
    # point forward — a successor is often authored before the event it
    # replaces is retired. The photo-comment self-FK gets away with a one-pass
    # map only because its SELECT is ordered by created_at, and this one is
    # unordered. Filling as we went would silently NULL every forward pointer.
    event_id_map: dict[uuid.UUID, uuid.UUID] = {ev.id: uuid.uuid4() for ev in events}
    new_event_by_old_id: dict[uuid.UUID, Event] = {}
    for ev in events:
        new_ev_id = event_id_map[ev.id]
        # The same refusal the relation loop below makes, for the same class of
        # row and for the same reason. ``event_service`` now scopes an event's
        # type and its meta values to the branch the event is written on, but
        # rows stored before those checks exist and no migration sweeps them
        # (tripl-0zpq.123) — and all three lookups below were unqualified
        # subscripts, i.e. a KeyError surfacing as a bare 500 on "create a
        # branch", naming nothing. 409 for the same reason the relation guard
        # gives: copying the event without its dangling child would make the new
        # branch read as a deletion nobody made.
        #
        # A field value cannot dangle on its own — ``_validate_field_values``
        # loads the definitions from the event's OWN type — so it dangles
        # exactly when the type does. It is listed anyway, so the message names
        # every id the operator has to deal with rather than the first one.
        dangling = [
            str(ref)
            for ref, known in (
                (ev.event_type_id, et_map),
                *((fv.field_definition_id, fd_map) for fv in ev.field_values),
                *((mv.meta_field_definition_id, mf_map) for mv in ev.meta_values),
            )
            if ref not in known
        ]
        if dangling:
            remedy = (
                # ``EventUpdate`` carries no ``event_type_id``: a mis-parented
                # event cannot be re-pointed through any route, so the only
                # repair left is removing it.
                "Delete the event, then create the branch."
                if ev.event_type_id not in et_map
                # Only values dangle, and both doors that write them take a full
                # replacement set, so leaving the bad one out of a PATCH is the
                # whole repair.
                else "Edit the event to drop those values, then create the branch."
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Event '{ev.name}' ({ev.id}) cannot be copied to a branch: it "
                    f"points at {', '.join(dangling)}, which is not part of the plan "
                    f"being copied. {remedy}"
                ),
            )
        new_event = Event(
            id=new_ev_id,
            project_id=project_id,
            branch_id=target_branch_id,
            event_type_id=et_map[ev.event_type_id],
            name=ev.name,
            title=ev.title,
            source_name=ev.source_name,
            description=ev.description,
            order=ev.order,
            status=ev.status,
            sunset_at=ev.sunset_at,
            last_seen_at=ev.last_seen_at,
            metric_breakdown_columns=list(ev.metric_breakdown_columns or []),
            owner_id=ev.owner_id,
            reviewed=ev.reviewed,
            # Which main row this copy is, so rows sharing a (type, name) can be
            # told apart by the diff, the merge, a revert and the discussion
            # twin (tripl-0zpq.292). A copy of a copy keeps the first origin.
            origin_id=ev.origin_id or ev.id,
            # superseded_by_event_id is set AFTER the flush below: the FK is
            # immediate on Postgres and, with no mapped relationship, the
            # unit of work will not order two rows of the same table by a
            # raw FK column.
        )
        new_event_by_old_id[ev.id] = new_event
        new_objs.append(new_event)
        for fv in ev.field_values:
            new_objs.append(
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=new_ev_id,
                    field_definition_id=fd_map[fv.field_definition_id],
                    value=fv.value,
                    is_authored=fv.is_authored,
                )
            )
        for mv in ev.meta_values:
            new_objs.append(
                EventMetaValue(
                    id=uuid.uuid4(),
                    event_id=new_ev_id,
                    meta_field_definition_id=mf_map[mv.meta_field_definition_id],
                    value=mv.value,
                )
            )
        for tag in ev.tags:
            new_objs.append(EventTag(id=uuid.uuid4(), event_id=new_ev_id, name=tag.name))

    if event_id_map and var_map:
        variable_values = (
            (
                await session.execute(
                    select(VariableValue).where(
                        VariableValue.project_id == project_id,
                        VariableValue.branch_id == source_branch_id,
                        VariableValue.event_id.in_(list(event_id_map.keys())),
                    )
                )
            )
            .scalars()
            .all()
        )
        for value_context in variable_values:
            variable_id = var_map.get(value_context.variable_id)
            event_id = event_id_map.get(value_context.event_id)
            field_definition_id = fd_map.get(value_context.field_definition_id)
            if variable_id is None or event_id is None or field_definition_id is None:
                continue
            new_objs.append(
                VariableValue(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    branch_id=target_branch_id,
                    variable_id=variable_id,
                    event_id=event_id,
                    field_definition_id=field_definition_id,
                    source_column=value_context.source_column,
                    value_kind=value_context.value_kind,
                    observed_count=value_context.observed_count,
                    values=list(value_context.values or []),
                )
            )

        overrides = (
            (
                await session.execute(
                    select(VariableEventValueOverride).where(
                        VariableEventValueOverride.project_id == project_id,
                        VariableEventValueOverride.branch_id == source_branch_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for override in overrides:
            override_variable_id = var_map.get(override.variable_id)
            override_event_id = event_id_map.get(override.event_id)
            if override_variable_id is None or override_event_id is None:
                continue
            new_objs.append(
                VariableEventValueOverride(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    branch_id=target_branch_id,
                    variable_id=override_variable_id,
                    event_id=override_event_id,
                    values=list(override.values or []),
                )
            )

    # Photos + threaded comments. Reuse storage_key/external_url so blobs aren't
    # duplicated: branch and main rows reference the same underlying object.
    if event_id_map:
        source_event_ids = list(event_id_map.keys())
        photos = (
            (
                await session.execute(
                    select(EventPhoto)
                    .where(EventPhoto.event_id.in_(source_event_ids))
                    .order_by(EventPhoto.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        photo_id_map: dict[uuid.UUID, uuid.UUID] = {}
        for ph in photos:
            new_ph_id = uuid.uuid4()
            photo_id_map[ph.id] = new_ph_id
            new_objs.append(
                EventPhoto(
                    id=new_ph_id,
                    project_id=ph.project_id,
                    event_id=event_id_map[ph.event_id],
                    uploaded_by_user_id=ph.uploaded_by_user_id,
                    original_filename=ph.original_filename,
                    content_type=ph.content_type,
                    size_bytes=ph.size_bytes,
                    kind=ph.kind,
                    external_url=ph.external_url,
                    storage_backend=ph.storage_backend,
                    storage_key=ph.storage_key,
                    sort_order=ph.sort_order,
                )
            )
        if photo_id_map:
            # EventPhotoComment.photo_id references EventPhoto, but the two
            # models share no ORM relationship, so the unit of work — which
            # orders inserts by relationship, not by raw FK columns — may emit a
            # comment INSERT before its photo. Flush the queued photos first so
            # the comments below satisfy the FK. It is immediate and
            # non-deferrable on Postgres too, not only under the test PRAGMA.
            session.add_all(new_objs)
            await session.flush()
            new_objs = []
            comments = (
                (
                    await session.execute(
                        select(EventPhotoComment)
                        .where(EventPhotoComment.photo_id.in_(list(photo_id_map.keys())))
                        .order_by(EventPhotoComment.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
            comment_id_map: dict[uuid.UUID, uuid.UUID] = {}
            for c in comments:
                # The query selects on photo_id, so an event-anchored comment
                # cannot arrive here — and must not: the event discussion is one
                # conversation read through to main, never deep-copied
                # (tripl-h2sx.25).
                if c.photo_id is None:
                    continue
                new_c_id = uuid.uuid4()
                comment_id_map[c.id] = new_c_id
                new_objs.append(
                    EventPhotoComment(
                        id=new_c_id,
                        photo_id=photo_id_map[c.photo_id],
                        parent_id=(
                            comment_id_map.get(c.parent_id) if c.parent_id is not None else None
                        ),
                        user_id=c.user_id,
                        body=c.body,
                    )
                )

    relations = await _load_for_branch(session, EventTypeRelation, project_id, source_branch_id)
    for rel in relations:
        # ``relation_service.create_relation`` now refuses to write a relation
        # whose ids are not all resolvable within the branch, but rows stored
        # before that refusal exist and no migration sweeps them — and here they
        # were an unqualified ``et_map[...]`` KeyError, i.e. a 500 on "create a
        # branch" naming nothing (tripl-0zpq.128). 409 rather than a silent skip:
        # a copy that quietly drops a relation is a branch whose diff then reads
        # as a deletion nobody made, and the merge would carry that deletion onto
        # main.
        missing = [
            str(ref)
            for ref, table in (
                (rel.source_event_type_id, et_map),
                (rel.target_event_type_id, et_map),
                (rel.source_field_id, fd_map),
                (rel.target_field_id, fd_map),
            )
            if ref not in table
        ]
        if missing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Relation {rel.id} cannot be copied to a branch: it points at "
                    f"{', '.join(missing)}, which is not part of this project's plan. "
                    "Delete the relation, then create the branch."
                ),
            )
        new_objs.append(
            EventTypeRelation(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=target_branch_id,
                source_event_type_id=et_map[rel.source_event_type_id],
                target_event_type_id=et_map[rel.target_event_type_id],
                source_field_id=fd_map[rel.source_field_id],
                target_field_id=fd_map[rel.target_field_id],
                relation_type=rel.relation_type,
                description=rel.description,
                # As ``origin_id`` on the events above (tripl-0zpq.292).
                origin_id=rel.origin_id or rel.id,
            )
        )

    session.add_all(new_objs)
    # Every copied event exists now, so a pointer between two of them resolves.
    # A successor outside the copied set leaves the pointer NULL rather than
    # pointing at another branch's row — the copy is self-contained by design,
    # and nothing else in this function references anything outside it.
    await session.flush()
    for ev in events:
        if ev.superseded_by_event_id is None:
            continue
        successor_id = event_id_map.get(ev.superseded_by_event_id)
        if successor_id is not None:
            new_event_by_old_id[ev.id].superseded_by_event_id = successor_id


async def _begin_consistent_read(session: AsyncSession) -> None:
    """Open ``session``'s transaction at the level whose reads share one state.

    Postgres fixes a transaction's isolation at its first statement, so the
    level has to ride on the BEGIN and ``session`` must not have started a
    transaction yet. The request session always has — the auth dependency read
    the user through it before any service ran — and SQLAlchemy refuses to raise
    the level in place, so ``create_branch`` passes a session of its own.
    SQLAlchemy restores the engine default when the connection goes back to the
    pool, so the level ends with this transaction.
    """
    level = _CONSISTENT_READ_ISOLATION.get(session.get_bind().dialect.name)
    if level is not None:
        await session.connection(execution_options={"isolation_level": level})


def _is_serialization_failure(exc: DBAPIError) -> bool:
    """Whether the database aborted the transaction as unserializable (40001).

    SQLAlchemy's asyncpg adapter copies the server's SQLSTATE onto the error it
    wraps as both ``sqlstate`` and ``pgcode``; psycopg 3 and psycopg2 each carry
    one of the two.
    """
    codes = (getattr(exc.orig, "sqlstate", None), getattr(exc.orig, "pgcode", None))
    return _SERIALIZATION_FAILURE_SQLSTATE in codes


def _is_duplicate_branch_name(exc: IntegrityError) -> bool:
    """Whether the insert lost a race for the branch name (23505 on its index).

    The duplicate check above is a plain SELECT holding nothing, and the copy
    that follows takes the better part of a second (main's snapshot alone
    measured 507 ms on a 1300-variable project), so two requests naming the same
    branch can both pass it and the loser meets ``uq_plan_branch_project_name``
    at the flush. That is the very case the SELECT exists to answer, so it gets
    the same 409 rather than escaping as a 500. Matched on the index name too,
    so another unique violation from the copy is still a 500 the logs will show.
    """
    orig = exc.orig
    codes = (getattr(orig, "sqlstate", None), getattr(orig, "pgcode", None))
    if _UNIQUE_VIOLATION_SQLSTATE not in codes:
        return False
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if constraint is not None:
        return bool(constraint == _BRANCH_NAME_CONSTRAINT)
    return _BRANCH_NAME_CONSTRAINT in str(exc)


async def create_branch(
    session: AsyncSession,
    slug: str,
    data: PlanBranchCreate,
    *,
    user_id: uuid.UUID | None = None,
) -> PlanBranchResponse:
    project_id = await _resolve_project_id(session, slug)
    main_branch_id = await ensure_main_branch_id(session, project_id)

    dup = await session.scalar(
        select(PlanBranch.id).where(
            PlanBranch.project_id == project_id,
            PlanBranch.name == data.name,
        )
    )
    if dup is not None:
        raise HTTPException(status_code=409, detail="Branch with this name already exists")

    # The merge base and the branch copy are two separate passes over main, and
    # they must describe the SAME main (tripl-0zpq.153). Under Postgres' default
    # READ COMMITTED every SELECT sees whatever committed last, so a catalog scan
    # or another editor committing to main between the two passes (seconds
    # apart on a large plan) left the base short of rows the copy has. A
    # scan-added event then read as the author's addition. A main edit made in
    # the gap read as a branch edit, and as the three-way baseline it later
    # reported a conflict nobody made. One REPEATABLE READ transaction gives the
    # snapshot, the copy and each of the copy's own SELECTs one MVCC snapshot.
    # Building the base from the copy would not have needed the database's help,
    # but it would put BRANCH ids in the base, and a removed diff entry links to
    # main only because its id comes from the base.
    #
    # What the one snapshot costs. Under REPEATABLE READ, Postgres aborts the
    # transaction with a serialization failure (SQLSTATE 40001) when a row it
    # must lock was changed by a transaction that committed after its snapshot.
    # Every insert here checks its foreign keys by locking the parent row as the
    # snapshot saw it, so a key update or a delete of a referenced projects or
    # users row, committed after the snapshot, aborts the creation. The key
    # update is the new failure: READ COMMITTED locks the newest version of the
    # row and carries on. A delete fails there too, as a foreign-key violation.
    # A project rename is a key update, because the slug is unique. Nothing in
    # the app changes a user's email (the unique user column) or deletes a user
    # today. The base revision's insert locks the project row, and a rename from
    # then on waits for the copy, so the exposure is a rename committed between
    # the snapshot and that insert, a sub-second window.
    #
    # Nothing handled that abort, so it was a 500. Now each attempt runs in a
    # session of its own and a failed one is retried from a fresh snapshot,
    # which already sees the rename. After ``_CREATE_BRANCH_ATTEMPTS`` the client
    # gets a 409 asking it to retry. A session per attempt, rather than rolling
    # back the request's: the rollback discards every row the failed attempt
    # wrote, so nothing is inserted twice, but on the request's session it would
    # also expire every object the caller holds there, the current user the
    # router audits with included (the reindex after a merge sidesteps the same
    # trap the same way). The request's transaction is committed first. It holds
    # only reads plus a lazily created main branch, which a session on another
    # connection could not see otherwise.
    await session.commit()
    for _attempt in range(_CREATE_BRANCH_ATTEMPTS):
        async with AsyncSession(session.bind, expire_on_commit=False) as attempt_session:
            try:
                branch = await _copy_main_into_new_branch(
                    attempt_session,
                    project_id=project_id,
                    main_branch_id=main_branch_id,
                    data=data,
                    user_id=user_id,
                )
            except IntegrityError as exc:
                if not _is_duplicate_branch_name(exc):
                    raise
                raise HTTPException(
                    status_code=409, detail="Branch with this name already exists"
                ) from exc
            except DBAPIError as exc:
                if not _is_serialization_failure(exc):
                    raise
                continue
            # Committed. A failure from here on must not start another attempt,
            # which would land a second copy next to this one.
            await attempt_session.refresh(branch)
            return _to_response(branch)
    raise HTTPException(
        status_code=409,
        detail="The project changed while the branch was being created. Please try again.",
    )


async def _copy_main_into_new_branch(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    main_branch_id: uuid.UUID,
    data: PlanBranchCreate,
    user_id: uuid.UUID | None,
) -> PlanBranch:
    """One attempt at ``create_branch``: merge base, branch row and deep copy.

    All three in one consistent-read transaction, which this opens on a
    ``session`` that has none yet and commits.
    """
    await _begin_consistent_read(session)

    # Capture the main snapshot as the merge base.
    base_payload = await build_plan_snapshot(session, project_id, branch_id=main_branch_id)
    base_revision = PlanRevision(
        project_id=project_id,
        created_by=user_id,
        summary=f"Base snapshot for branch '{data.name}'",
        kind=PlanRevisionKind.branch_base.value,
        payload=base_payload,
    )
    session.add(base_revision)
    await session.flush()

    branch = PlanBranch(
        project_id=project_id,
        name=data.name,
        kind=BranchKind.working.value,
        status=BranchStatus.draft.value,
        description=data.description,
        base_revision_id=base_revision.id,
        created_by=user_id,
    )
    session.add(branch)
    await session.flush()
    # The revision is written before the branch exists, so its link back is
    # stamped here, after the branch row has an id (PL-21).
    base_revision.branch_id = branch.id

    await deep_copy_plan_to_branch(
        session,
        project_id=project_id,
        source_branch_id=main_branch_id,
        target_branch_id=branch.id,
    )
    await notification_announce.best_effort(
        session,
        "subscribe branch author",
        lambda: subscription_service.subscribe(
            session,
            user_id=user_id,
            project_id=project_id,
            entity_type=subscription_service.BRANCH,
            entity_id=branch.id,
            reason="author",
        ),
    )
    await session.commit()
    return branch


async def delete_branch(session: AsyncSession, slug: str, branch_id: uuid.UUID) -> None:
    project_id = await _resolve_project_id(session, slug)
    branch = await _get_branch(session, project_id, branch_id)
    if branch.kind == BranchKind.main.value:
        raise HTTPException(status_code=400, detail="The main branch cannot be deleted")
    # PlanBranch maps no ``events`` relationship either, so deleting a branch
    # takes every event on it through the database cascade with nothing in the
    # ORM reporting it. This is the door the issue did not name, and the only one
    # that reaches BRANCH-LOCAL events — which project-scoped rows really can
    # reference, because the alert-rule filter picker lists events on the active
    # branch. No survivor, so DROP (tripl-a64t).
    doomed_event_ids = list(
        (await session.execute(select(Event.id).where(Event.branch_id == branch.id)))
        .scalars()
        .all()
    )
    await drop_dangling_event_references(session, project_id=project_id, event_ids=doomed_event_ids)
    # A thread on a branch row whose main twin appeared later is shown on that
    # twin as well, so it is main's to keep, not the cascade's to take
    # (tripl-0zpq.289). Local import: ``_branch_counterparts`` imports this
    # module, so a top-level import here would be circular.
    from tripl.services._branch_event_threads import rescue_branch_event_threads

    await rescue_branch_event_threads(session, project_id=project_id, event_ids=doomed_event_ids)
    await session.delete(branch)
    await session.commit()


def _reject_main(branch: PlanBranch) -> None:
    if branch.kind == BranchKind.main.value:
        raise HTTPException(status_code=400, detail="The main branch has no review workflow")


async def transition_branch(
    session: AsyncSession,
    slug: str,
    branch_id: uuid.UUID,
    action: BranchTransitionAction,
    user_id: uuid.UUID,
) -> PlanBranchDetailResponse:
    project_id = await _resolve_project_id(session, slug)
    branch = await _get_branch(session, project_id, branch_id)
    _reject_main(branch)
    if branch.status == BranchStatus.merged.value:
        raise HTTPException(status_code=400, detail="Merged branches are immutable")

    allowed_from, target = _TRANSITIONS[action]
    if branch.status not in allowed_from:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} from status '{branch.status}'",
        )

    if action == "approve":
        policy = await read_branch_merge_policy(session, project_id)
        if policy.block_self_approval and branch.created_by == user_id:
            raise HTTPException(
                status_code=403,
                detail="Branch authors cannot approve their own branch",
            )

    branch.status = target

    if action in _APPROVAL_CLEARING_ACTIONS:
        await session.execute(
            delete(PlanBranchApproval).where(PlanBranchApproval.branch_id == branch.id)
        )
        # Same lifecycle as approvals: once the branch goes back into draft /
        # changes-requested / ready-for-review, prior per-field merge choices
        # don't survive — the reviewer makes a fresh round of picks against
        # the new base/ours/theirs.
        await session.execute(
            delete(PlanBranchMergeResolution).where(
                PlanBranchMergeResolution.branch_id == branch.id
            )
        )

    if action == "approve":
        # Pin the approval to the content being approved: the merge gates only
        # count approvals whose hash still matches the branch (tripl-d8v6).
        current_hash = plan_snapshot_hash(
            await build_plan_snapshot(session, project_id, branch_id=branch.id)
        )
        # Upsert: re-approving by the same user restamps the content hash.
        existing = await session.scalar(
            select(PlanBranchApproval).where(
                PlanBranchApproval.branch_id == branch.id,
                PlanBranchApproval.user_id == user_id,
            )
        )
        if existing is None:
            session.add(
                PlanBranchApproval(branch_id=branch.id, user_id=user_id, plan_hash=current_hash)
            )
        else:
            existing.plan_hash = current_hash

    if action == "submit":
        # Surface the owners of touched event types as expected reviewers up
        # front — the same set the merge gate will later require an approval
        # from. Local import: merge_service imports this module, so a top-level
        # import here would be circular.
        from tripl.services.plan_branch_merge_service import (
            assign_owner_reviewers_for_branch,
        )

        await assign_owner_reviewers_for_branch(session, project_id=project_id, branch=branch)

    # Best-effort in a savepoint: a failed notification never fails the step.
    await notification_announce.best_effort(
        session,
        f"branch {action}",
        lambda: _announce_transition(session, slug, branch, action=action, actor_id=user_id),
    )
    await session.commit()
    await session.refresh(branch)
    return await _to_detail(session, branch)


def _branch_url(slug: str, branch: PlanBranch) -> str:
    return f"/p/{slug}/branches/{branch.id}"


async def _reviewer_ids(session: AsyncSession, branch_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(PlanBranchReviewer.user_id).where(PlanBranchReviewer.branch_id == branch_id)
    )
    return set(rows.all())


async def _announce_transition(
    session: AsyncSession,
    slug: str,
    branch: PlanBranch,
    *,
    action: str,
    actor_id: uuid.UUID,
) -> None:
    """Review steps that reach people (#259): submit asks the reviewers; approve
    tells the author, the reviewers and everyone watching the branch — the same
    audience as ``branch_merged``. Other steps are quiet."""
    if action not in ("submit", "approve"):
        return
    who = await notification_announce.actor_label(session, actor_id)
    if action == "submit":
        reviewers = await _reviewer_ids(session, branch.id)
        for reviewer_id in reviewers:
            await subscription_service.subscribe(
                session,
                user_id=reviewer_id,
                project_id=branch.project_id,
                entity_type=subscription_service.BRANCH,
                entity_id=branch.id,
                reason="reviewer",
            )
        await notification_service.notify(
            session,
            project_id=branch.project_id,
            kind="branch_review_requested",
            entity_type=subscription_service.BRANCH,
            entity_id=branch.id,
            title=f"{who} asked for your review of branch {branch.name}",
            url=_branch_url(slug, branch),
            actor_user_id=actor_id,
            user_ids=reviewers,
        )
        return
    await notification_service.notify(
        session,
        project_id=branch.project_id,
        kind="branch_approved",
        entity_type=subscription_service.BRANCH,
        entity_id=branch.id,
        title=f"{who} approved branch {branch.name}",
        url=_branch_url(slug, branch),
        actor_user_id=actor_id,
        user_ids={
            *(await _reviewer_ids(session, branch.id)),
            *([branch.created_by] if branch.created_by is not None else []),
        },
        watchers_of=[(subscription_service.BRANCH, branch.id)],
    )


async def _resolve_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def add_reviewer(
    session: AsyncSession,
    slug: str,
    branch_id: uuid.UUID,
    data: BranchReviewerCreate,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> BranchReviewerResponse:
    project_id = await _resolve_project_id(session, slug)
    branch = await _get_branch(session, project_id, branch_id)
    _reject_main(branch)
    reviewer_user = await _resolve_user(session, data.user_id)
    # A reviewer who cannot see the project could never open the branch to
    # review it; the instance owner counts, as they see every project.
    if await member_role(session, reviewer_user, project_id) is None:
        raise HTTPException(status_code=422, detail=NOT_A_MEMBER_DETAIL)
    existing = await session.scalar(
        select(PlanBranchReviewer).where(
            PlanBranchReviewer.branch_id == branch.id,
            PlanBranchReviewer.user_id == data.user_id,
        )
    )
    if existing is not None:
        return BranchReviewerResponse.model_validate(existing)
    reviewer = PlanBranchReviewer(branch_id=branch.id, user_id=data.user_id)
    session.add(reviewer)

    async def announce_reviewer() -> None:
        await subscription_service.subscribe(
            session,
            user_id=data.user_id,
            project_id=project_id,
            entity_type=subscription_service.BRANCH,
            entity_id=branch.id,
            reason="reviewer",
        )
        if branch.status in (BranchStatus.ready_for_review.value, BranchStatus.approved.value):
            # Added after the branch went up for review: ask now, not on the next submit.
            who = await notification_announce.actor_label(session, actor_user_id)
            await notification_service.notify(
                session,
                project_id=project_id,
                kind="branch_review_requested",
                entity_type=subscription_service.BRANCH,
                entity_id=branch.id,
                title=f"{who} asked for your review of branch {branch.name}",
                url=_branch_url(slug, branch),
                actor_user_id=actor_user_id,
                user_ids=[data.user_id],
            )

    # Best-effort in a savepoint: a failed notification never fails the add.
    await notification_announce.best_effort(session, "add reviewer", announce_reviewer)
    await session.commit()
    await session.refresh(reviewer)
    return BranchReviewerResponse.model_validate(reviewer)


async def remove_reviewer(
    session: AsyncSession, slug: str, branch_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    project_id = await _resolve_project_id(session, slug)
    branch = await _get_branch(session, project_id, branch_id)
    _reject_main(branch)
    reviewer = await session.scalar(
        select(PlanBranchReviewer).where(
            PlanBranchReviewer.branch_id == branch.id,
            PlanBranchReviewer.user_id == user_id,
        )
    )
    if reviewer is None:
        raise HTTPException(status_code=404, detail="Reviewer not assigned")
    await session.delete(reviewer)
    await session.commit()


async def list_comments(
    session: AsyncSession, slug: str, branch_id: uuid.UUID
) -> list[BranchCommentResponse]:
    project_id = await _resolve_project_id(session, slug)
    branch = await _get_branch(session, project_id, branch_id)
    rows = (
        (
            await session.execute(
                select(PlanBranchComment)
                .where(PlanBranchComment.branch_id == branch.id)
                .order_by(PlanBranchComment.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [BranchCommentResponse.model_validate(c) for c in rows]


async def create_comment(
    session: AsyncSession,
    slug: str,
    branch_id: uuid.UUID,
    data: BranchCommentCreate,
    user_id: uuid.UUID,
) -> BranchCommentResponse:
    project_id = await _resolve_project_id(session, slug)
    branch = await _get_branch(session, project_id, branch_id)
    if data.parent_id is not None:
        parent = await session.get(PlanBranchComment, data.parent_id)
        if parent is None or parent.branch_id != branch.id:
            raise HTTPException(status_code=404, detail="Parent comment not found")
    comment = PlanBranchComment(
        branch_id=branch.id,
        parent_id=data.parent_id,
        user_id=user_id,
        body=data.body,
    )
    session.add(comment)
    await session.flush()
    await _announce_branch_comment(session, slug, branch, comment)
    await session.commit()
    await session.refresh(comment)
    return BranchCommentResponse.model_validate(comment)


async def _announce_branch_comment(
    session: AsyncSession, slug: str, branch: PlanBranch, comment: PlanBranchComment
) -> None:
    """Subscribe the commenter; @mentions first, then the branch's watchers (#259).

    Same two-pass shape as an event comment: whoever the mention reached is
    excluded from the ``comment`` / ``reply`` pass, so one comment is one row.
    Each step is best-effort in a savepoint: it never fails the comment.
    """

    async def subscribe_commenter() -> None:
        await subscription_service.subscribe(
            session,
            user_id=comment.user_id,
            project_id=branch.project_id,
            entity_type=subscription_service.BRANCH,
            entity_id=branch.id,
            reason="commenter",
        )

    async def notify_watchers() -> None:
        who = await notification_announce.actor_label(session, comment.user_id)
        common: notification_service.NotifyCommon = {
            "project_id": branch.project_id,
            "entity_type": subscription_service.BRANCH,
            "entity_id": branch.id,
            "url": _branch_url(slug, branch),
            "body": excerpt(comment.body),
            "actor_user_id": comment.user_id,
        }
        reached = await notification_announce.announce_mentions(
            session,
            body=comment.body,
            title=f"{who} mentioned you on branch {branch.name}",
            common=common,
        )
        is_reply = comment.parent_id is not None
        await notification_service.notify(
            session,
            kind="reply" if is_reply else "comment",
            title=f"{who} {'replied' if is_reply else 'commented'} on branch {branch.name}",
            watchers_of=[(subscription_service.BRANCH, branch.id)],
            exclude_user_ids=reached,
            **common,
        )

    await notification_announce.best_effort(session, "subscribe commenter", subscribe_commenter)
    await notification_announce.best_effort(session, "branch comment", notify_watchers)


async def delete_comment(
    session: AsyncSession, slug: str, branch_id: uuid.UUID, comment_id: uuid.UUID, user: User
) -> None:
    project_id = await _resolve_project_id(session, slug)
    branch = await _get_branch(session, project_id, branch_id)
    comment = await session.get(PlanBranchComment, comment_id)
    if comment is None or comment.branch_id != branch.id:
        raise HTTPException(status_code=404, detail="Comment not found")
    # Same rule as the event and photo threads: an editor removes their own
    # words, an owner moderates, and an orphaned comment is left to owners.
    if user.role != "owner" and (comment.user_id is None or comment.user_id != user.id):
        raise HTTPException(
            status_code=403,
            detail="Only the comment's author or an owner can delete it",
        )
    await session.delete(comment)
    await session.commit()


def _summary_counts(entries: list[Any]) -> dict[str, int]:
    """Per-kind tally of the author's changes; housekeeping counted apart."""
    out = {"added": 0, "removed": 0, "changed": 0, "housekeeping": 0}
    for entry in entries:
        if entry.housekeeping is not None:
            out["housekeeping"] += 1
        else:
            out[entry.kind] += 1
    return out


async def diff_branch(session: AsyncSession, slug: str, branch_id: uuid.UUID) -> PlanBranchDiff:
    project_id = await _resolve_project_id(session, slug)
    branch = await _get_branch(session, project_id, branch_id)
    _reject_main(branch)
    main_branch_id = await ensure_main_branch_id(session, project_id)

    main_snapshot = await build_plan_snapshot(session, project_id, branch_id=main_branch_id)
    branch_snapshot = await build_plan_snapshot(session, project_id, branch_id=branch.id)
    entries: list[Any] = []
    renames: list[PlanDiffRename] = []
    behind_base = False
    if branch.base_revision_id is not None:
        base_revision = await session.get(PlanRevision, branch.base_revision_id)
        if base_revision is not None:
            base_payload = base_revision.payload or {}
            # Visible entries are changes authored on the branch, not changes
            # that landed on main after the branch was opened.
            entries = compute_plan_diff_entries(
                base_payload,
                branch_snapshot,
                key_collisions_from=main_snapshot,
                origins_complete=branch.origin_ids_complete,
            )
            behind_entries = compute_plan_diff_entries(
                base_payload, main_snapshot, origins_complete=True
            )
            behind_base = len(behind_entries) > 0
            mark_housekeeping(entries, behind_entries=behind_entries, main_payload=main_snapshot)
            # The MERGE's own pairing, read through the same function the merge
            # applies, so a rename reads as one change instead of a deletion
            # beside an unrelated addition. A second implementation here would
            # be free to drift from what the merge then actually does
            # (tripl-amnn).
            renames = snapshot_rename_pairs(base_payload, main_snapshot, branch_snapshot)
    else:
        # Legacy working branches without a base snapshot cannot distinguish
        # branch-authored changes from later main changes — and with no third
        # side to read, the pairing has nothing to say either.
        entries = compute_plan_diff_entries(main_snapshot, branch_snapshot)
        mark_housekeeping(entries, main_payload=main_snapshot)

    await attach_identity_warnings(
        session,
        project_id=project_id,
        branch_id=branch.id,
        entries=entries,
        branch_snapshot=branch_snapshot,
    )
    return PlanBranchDiff(
        entries=entries,
        summary=_summary_counts(entries),
        behind_base=behind_base,
        renames=renames,
    )
