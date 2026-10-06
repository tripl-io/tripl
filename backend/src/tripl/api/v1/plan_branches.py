import uuid

from fastapi import APIRouter, HTTPException

from tripl.api.deps import EditorUserDep, SessionDep
from tripl.schemas.plan_branch import (
    BranchCommentCreate,
    BranchCommentResponse,
    BranchConflictsResponse,
    BranchRevertRequest,
    BranchReviewerCreate,
    BranchReviewerResponse,
    BranchTransferItem,
    BranchTransferRequest,
    BranchTransferResult,
    BranchTransitionRequest,
    MergedEventPreview,
    PlanBranchCreate,
    PlanBranchDetailResponse,
    PlanBranchDiff,
    PlanBranchList,
    PlanBranchResponse,
    ResolutionBatchCreate,
    ResolutionBatchResponse,
    ResolutionCreate,
    ResolutionResponse,
    UpdateFromMainPreview,
    UpdateFromMainRequest,
    UpdateFromMainResult,
)
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import (
    audit_service,
    plan_branch_conflicts,
    plan_branch_merge_preview_service,
    plan_branch_merge_service,
    plan_branch_revert_service,
    plan_branch_service,
    plan_branch_transfer_service,
    plan_branch_update_service,
)

router = APIRouter(prefix="/projects/{slug}/branches", tags=["plan-branches"])


@router.get("", response_model=PlanBranchList)
async def list_branches(
    session: SessionDep,
    slug: str,
    include_diff_counts: bool = False,
) -> PlanBranchList:
    """List a project's branches.

    ``include_diff_counts`` fills ``ahead`` / ``behind_base`` for each open
    feature branch (draft, ready_for_review, changes_requested, approved) from a
    single shared main snapshot, so a branches list does not need one
    ``/branches/{id}/diff`` call per row. Merged and closed branches keep both
    null, like main. It is opt-in because it costs one plan snapshot per open
    branch plus one for main; leave it off when you only need the branch rows.
    """
    return await plan_branch_service.list_branches(
        session, slug, include_diff_counts=include_diff_counts
    )


@router.post("", response_model=PlanBranchResponse, status_code=201)
async def create_branch(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    data: PlanBranchCreate,
) -> PlanBranchResponse:
    branch = await plan_branch_service.create_branch(session, slug, data, user_id=current_user.id)
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.create",
        target_type="plan_branch",
        target_id=branch.id,
        target_name=branch.name,
        project_slug=slug,
        payload={"name": branch.name},
    )
    return branch


@router.get("/{branch_id}", response_model=PlanBranchDetailResponse)
async def get_branch(
    session: SessionDep, slug: str, branch_id: uuid.UUID
) -> PlanBranchDetailResponse:
    return await plan_branch_service.get_branch(session, slug, branch_id)


@router.delete("/{branch_id}", status_code=204)
async def delete_branch(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
) -> None:
    existing = await plan_branch_service.get_branch(session, slug, branch_id)
    await plan_branch_service.delete_branch(session, slug, branch_id)
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.delete",
        target_type="plan_branch",
        target_id=branch_id,
        target_name=existing.name,
        project_slug=slug,
    )


@router.post("/{branch_id}/transition", response_model=PlanBranchDetailResponse)
async def transition_branch(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    data: BranchTransitionRequest,
) -> PlanBranchDetailResponse:
    detail = await plan_branch_service.transition_branch(
        session, slug, branch_id, data.action, user_id=current_user.id
    )
    await audit_service.record(
        session,
        user=current_user,
        action=f"plan_branch.{data.action}",
        target_type="plan_branch",
        target_id=branch_id,
        target_name=detail.name,
        project_slug=slug,
        payload={"status": detail.status},
    )
    return detail


@router.post("/{branch_id}/reviewers", response_model=BranchReviewerResponse, status_code=201)
async def add_reviewer(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    data: BranchReviewerCreate,
) -> BranchReviewerResponse:
    reviewer = await plan_branch_service.add_reviewer(
        session, slug, branch_id, data, actor_user_id=current_user.id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.add_reviewer",
        target_type="plan_branch",
        target_id=branch_id,
        target_name="",
        project_slug=slug,
        payload={"user_id": str(data.user_id)},
    )
    return reviewer


@router.delete("/{branch_id}/reviewers/{user_id}", status_code=204)
async def remove_reviewer(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    await plan_branch_service.remove_reviewer(session, slug, branch_id, user_id)
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.remove_reviewer",
        target_type="plan_branch",
        target_id=branch_id,
        target_name="",
        project_slug=slug,
        payload={"user_id": str(user_id)},
    )


@router.get("/{branch_id}/comments", response_model=list[BranchCommentResponse])
async def list_comments(
    session: SessionDep, slug: str, branch_id: uuid.UUID
) -> list[BranchCommentResponse]:
    return await plan_branch_service.list_comments(session, slug, branch_id)


@router.post("/{branch_id}/comments", response_model=BranchCommentResponse, status_code=201)
async def create_comment(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    data: BranchCommentCreate,
) -> BranchCommentResponse:
    comment = await plan_branch_service.create_comment(
        session, slug, branch_id, data, user_id=current_user.id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.comment_create",
        target_type="plan_branch",
        target_id=branch_id,
        project_slug=slug,
        payload={"comment_id": str(comment.id)},
    )
    return comment


@router.delete("/{branch_id}/comments/{comment_id}", status_code=204)
async def delete_comment(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    comment_id: uuid.UUID,
) -> None:
    await plan_branch_service.delete_comment(session, slug, branch_id, comment_id, current_user)
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.comment_delete",
        target_type="plan_branch",
        target_id=branch_id,
        project_slug=slug,
        payload={"comment_id": str(comment_id)},
    )


@router.get("/{branch_id}/diff", response_model=PlanBranchDiff)
async def diff_branch(session: SessionDep, slug: str, branch_id: uuid.UUID) -> PlanBranchDiff:
    return await plan_branch_service.diff_branch(session, slug, branch_id)


@router.get("/{branch_id}/merge-preview/event", response_model=MergedEventPreview)
async def merge_preview_event(
    session: SessionDep,
    slug: str,
    branch_id: uuid.UUID,
    event_id: uuid.UUID | None = None,
    # FreeTextFilter: both are matched against snapshot names, never bound
    # into SQL, but a NUL in either reaches the 404 message.
    event_type: FreeTextFilter | None = None,
    event_name: FreeTextFilter | None = None,
) -> MergedEventPreview:
    """One event as main will hold it after this branch merges ("As merged").

    Name the event by ``event_id`` (the branch's, the base's or main's id) or
    by ``event_type`` plus ``event_name`` on any side, not both. Read-only.
    """
    by_key = event_type is not None or event_name is not None
    if (event_id is None) == (not by_key) or (by_key and not (event_type and event_name)):
        raise HTTPException(
            status_code=422,
            detail="Give either event_id, or both event_type and event_name.",
        )
    return await plan_branch_merge_preview_service.merge_preview_event(
        session,
        slug,
        branch_id,
        event_id=event_id,
        event_type=event_type,
        event_name=event_name,
    )


@router.post("/{branch_id}/revert", response_model=PlanBranchDiff)
async def revert_branch_change(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    data: BranchRevertRequest,
) -> PlanBranchDiff:
    diff = await plan_branch_revert_service.revert_change(session, slug, branch_id, data)
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.revert",
        target_type="plan_branch",
        target_id=branch_id,
        target_name=data.name,
        project_slug=slug,
        payload={
            "entity_type": data.entity_type,
            "name": data.name,
            "parent": data.parent,
            "field": data.field,
        },
    )
    return diff


@router.post("/{branch_id}/transfer", response_model=BranchTransferResult)
async def transfer_branch_changes(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    data: BranchTransferRequest,
) -> BranchTransferResult:
    """Move or copy rows of this branch's diff onto another open branch.

    ``move`` applies them on the target and undoes them here; ``copy`` only
    applies them. What a row needs comes along (``carried``, with
    ``needed_by``); a row the target already says is ``skipped``.
    ``dry_run`` makes every write and rolls it back; with it,
    ``target_branch_id`` may be null to preview against a branch cut from main
    now. Refusals write nothing: 400 for a housekeeping row, main, the branch
    itself as target, or a deleted event type, field or meta field
    (``removed_not_transferable``); 422 for a null target without ``dry_run``
    (request validation); 409 for a merged or closed target, a move off a
    merged or closed branch (a copy off a closed one is allowed),
    ``transfer_base_mismatch`` (cut from different main content; the message
    names the branch to update), ``transfer_conflicts`` (every refused row at
    once), ``transfer_constraint_violation`` and ``transfer_retry`` (a
    concurrent write aborted the transaction; worth one more try).
    """
    outcome = await plan_branch_transfer_service.transfer_changes(
        session, slug, branch_id, data, user_id=current_user.id
    )
    result = outcome.result
    if not result.dry_run and result.target_branch_id is not None:
        payload = {
            "mode": result.mode,
            "source_branch_id": str(branch_id),
            "target_branch_id": str(result.target_branch_id),
            "entries": [_audit_item(item) for item in result.applied],
            "carried": [_audit_item(item) for item in result.carried],
            "skipped": [_audit_item(item) for item in result.skipped],
        }
        # Both rows in one commit: the first is left pending, the second lands it.
        await audit_service.record(
            session,
            user=current_user,
            action="plan_branch.transfer_out",
            target_type="plan_branch",
            target_id=branch_id,
            target_name=outcome.source_name,
            project_slug=slug,
            payload={**payload, "other_branch_name": result.target_branch_name},
            commit=False,
        )
        await audit_service.record(
            session,
            user=current_user,
            action="plan_branch.transfer_in",
            target_type="plan_branch",
            target_id=result.target_branch_id,
            target_name=result.target_branch_name or "",
            project_slug=slug,
            payload={**payload, "other_branch_name": outcome.source_name},
        )
    return result


def _audit_item(item: BranchTransferItem) -> dict[str, str | None]:
    return {
        "entity_type": item.entity_type,
        "name": item.name,
        "parent": item.parent,
        "kind": item.kind,
    }


@router.get("/{branch_id}/conflicts", response_model=BranchConflictsResponse)
async def get_branch_conflicts(
    session: SessionDep, slug: str, branch_id: uuid.UUID
) -> BranchConflictsResponse:
    return await plan_branch_conflicts.get_branch_conflicts(session, slug, branch_id)


@router.get("/{branch_id}/update-from-main", response_model=UpdateFromMainPreview)
async def preview_update_from_main(
    session: SessionDep, slug: str, branch_id: uuid.UUID
) -> UpdateFromMainPreview:
    """What "Update from main" would bring onto the branch, and what overlaps.

    Read-only. ``main_hash`` is main as this preview read it; send it back as
    ``expected_main_hash`` so the update refuses (409 ``main_moved``) rather
    than apply changes nobody reviewed.
    """
    return await plan_branch_update_service.preview_update(session, slug, branch_id)


@router.post("/{branch_id}/update-from-main", response_model=UpdateFromMainResult)
async def update_from_main(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    data: UpdateFromMainRequest,
) -> UpdateFromMainResult:
    """Three-way merge main INTO the branch; the branch's base becomes main.

    Every field both sides changed needs a choice — ``ours`` takes main's
    value, ``theirs`` keeps the branch's — given inline in ``resolutions`` or
    saved earlier through ``/resolutions`` (stored choices count only when
    ``expected_main_hash`` is sent). Refusals, all 409 and all writing
    nothing: ``unresolved_conflicts`` (with the full ``conflicts`` list),
    ``update_blocked`` (the preview's ``blockers``), ``main_moved``,
    ``incomplete_base_snapshot``, ``update_constraint_violation``, and a merged
    or closed branch. A branch already level with main answers 200
    with ``updated: false``.
    """
    outcome = await plan_branch_update_service.update_from_main(
        session, slug, branch_id, data, user_id=current_user.id
    )
    result = outcome.result
    if result.updated:
        await audit_service.record(
            session,
            user=current_user,
            action="plan_branch.update_from_main",
            target_type="plan_branch",
            target_id=branch_id,
            target_name=result.branch.name,
            project_slug=slug,
            payload={
                "previous_base_revision_id": (
                    str(result.previous_base_revision_id)
                    if result.previous_base_revision_id is not None
                    else None
                ),
                "base_revision_id": (
                    str(result.base_revision_id) if result.base_revision_id is not None else None
                ),
                "applied": [count.model_dump() for count in result.applied],
                "resolutions": outcome.resolution_counts,
            },
        )
    return result


@router.post(
    "/{branch_id}/resolutions/batch", response_model=ResolutionBatchResponse, status_code=201
)
async def save_branch_resolutions(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    data: ResolutionBatchCreate,
) -> ResolutionBatchResponse:
    """Store many conflict choices at once — all of them, or none on a 422."""
    resolutions = await plan_branch_conflicts.save_resolutions(
        session, slug, branch_id, data.resolutions, user_id=current_user.id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.resolution_batch_save",
        target_type="plan_branch",
        target_id=branch_id,
        project_slug=slug,
        payload={
            "count": len(resolutions),
            "entity_count": len({(r.entity_type, r.entity_name) for r in resolutions}),
        },
    )
    return ResolutionBatchResponse(resolutions=resolutions)


@router.post("/{branch_id}/resolutions", response_model=ResolutionResponse, status_code=201)
async def save_branch_resolution(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    data: ResolutionCreate,
) -> ResolutionResponse:
    resolution = await plan_branch_conflicts.save_resolution(
        session, slug, branch_id, data, user_id=current_user.id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.resolution_save",
        target_type="plan_branch",
        target_id=branch_id,
        project_slug=slug,
        payload={"resolution_id": str(resolution.id)},
    )
    return resolution


@router.delete("/{branch_id}/resolutions/{resolution_id}", status_code=204)
async def delete_branch_resolution(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
    resolution_id: uuid.UUID,
) -> None:
    await plan_branch_conflicts.delete_resolution(session, slug, branch_id, resolution_id)
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.resolution_delete",
        target_type="plan_branch",
        target_id=branch_id,
        project_slug=slug,
        payload={"resolution_id": str(resolution_id)},
    )


@router.post("/{branch_id}/merge", response_model=PlanBranchDetailResponse)
async def merge_branch(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    branch_id: uuid.UUID,
) -> PlanBranchDetailResponse:
    detail = await plan_branch_merge_service.merge_branch(
        session, slug, branch_id, user_id=current_user.id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch.merge",
        target_type="plan_branch",
        target_id=branch_id,
        target_name=detail.name,
        project_slug=slug,
        payload={"status": detail.status},
    )
    return detail
