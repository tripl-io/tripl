import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import (
    BranchIdDep,
    CurrentUserDep,
    EditorUserDep,
    OwnerUserDep,
    ProjectRoleDep,
    SessionDep,
    can_mutate_project,
)
from tripl.config import settings
from tripl.models.project import Project
from tripl.models.user import User
from tripl.schemas.project import (
    AnomalyResetCounts,
    DemoCancelResponse,
    DetectionResetPeriod,
    DriftResetCounts,
    ProjectCreate,
    ProjectCreateResponse,
    ProjectResponse,
    ProjectUpdate,
    VariableRetirementCounts,
    VariableRetirementRequest,
)
from tripl.services import (
    audit_service,
    demo_service,
    detection_reset_service,
    project_access,
    project_lookup,
    project_service,
    project_template_service,
    variable_retirement_service,
)

router = APIRouter(prefix="/projects", tags=["projects"])


def _is_project_manager(
    project_role: project_access.ProjectRole | None, user: User, project: Project
) -> bool:
    """Project role ``owner`` in this project, or the user who created it.

    Project role ``owner`` means an owner or admin of the project's OWN
    organization (:mod:`tripl.services.project_access`). Narrower than editing
    the project's contents (an ``editor`` member may do that): renaming and
    re-slugging a project, resetting or deleting a demo, and managing its
    members stay with its creator and the organization's owners and admins.
    Deleting a real (non-demo) project is narrower still: it is org
    owner/admin only (``OwnerUserDep`` on ``DELETE /projects/{slug}``), so a
    creator cannot delete one. A non-member never reaches this check; the
    membership gate has already answered 404.
    """
    return project_role == project_access.OWNER or project.created_by_user_id == user.id


def _require_demo_manager(
    project_role: project_access.ProjectRole | None, user: User, project: Project
) -> None:
    """Allow a demo's creator (any editor) or an org owner/admin to manage it.

    Resolves the create/delete permission mismatch: editors may create a demo,
    so a creator may also reset or delete the demo they made, while the
    organization's owners and admins may manage any demo. Real projects stay
    org owner/admin-only for deletion.
    """
    if _is_project_manager(project_role, user, project):
        return
    raise HTTPException(
        status_code=403,
        detail="Only the demo creator or an owner can manage this demo",
    )


def _require_project_manager(
    project_role: project_access.ProjectRole | None, user: User, project: Project
) -> None:
    """Guard project-identity edits (name, slug, retention policy).

    Being an ``editor`` member lets a user edit the tracking plan, not the
    project's identity (tripl-jfm3.19), so the manager set is: the owners and
    admins of the project's organization, plus whoever created the project.
    Projects created before creators were recorded have no creator and are
    therefore org owner/admin-managed.
    """
    if _is_project_manager(project_role, user, project):
        return
    raise HTTPException(
        status_code=403,
        detail="Only the project creator or an owner can edit this project",
    )


async def _record_lifecycle(
    session: AsyncSession,
    user: User,
    action: str,
    *,
    project_id: uuid.UUID,
    name: str,
    slug: str,
    payload: dict[str, object] | None = None,
) -> None:
    """One audit row for a project's OWN life: created, renamed, reset, destroyed.

    Everything the audit log tracks lives inside a project, and the project
    itself was the one object with no record of its own — an owner could destroy
    a workspace whole, with every event, variable, metric and alert rule in it,
    and the log held nothing about who did it (tripl-wkwv.19). That is the shape
    tripl-wkwv.10 fixed for events, one level up, and worse here: a deletion is
    irreversible and takes every per-project surface with it, so there is no
    second place left to look.

    ``project_slug`` rather than ``project``: it resolves the id by lookup, so
    the SAME call is correct before and after the row's subject exists. A delete
    row therefore carries the slug and a NULL project id — which is exactly what
    a project that no longer exists should look like — while a create or rename
    row carries both.

    The name and slug are repeated into the payload deliberately. After a delete
    the id resolves to nothing, so the row has to be readable on its own; this is
    the same reason ``event.bulk_delete`` files names rather than ids alone.

    Takes the three values rather than the project: a delete row is written after
    its subject is gone, and reading attributes off a deleted ORM instance is a
    detail of session state, not something an audit row should depend on.
    """
    await audit_service.record(
        session,
        user=user,
        action=action,
        target_type="project",
        target_id=project_id,
        target_name=name,
        project_slug=slug,
        payload={"slug": slug, "name": name, **(payload or {})},
    )


async def _for_caller(
    session: AsyncSession, request: Request, user: User, projects: list[ProjectResponse]
) -> list[ProjectResponse]:
    """``projects`` with ``can_mutate`` and ``my_role`` for this caller (see ProjectResponse)."""
    return await project_service.with_can_mutate(
        session, projects, user, lambda scope: can_mutate_project(request, user, scope)
    )


async def _one_for_caller(
    session: AsyncSession, request: Request, user: User, project: ProjectResponse
) -> ProjectResponse:
    (annotated,) = await _for_caller(session, request, user, [project])
    return annotated


def _require_demo_enabled() -> None:
    """Enforce the master rollback switch on demo PROVISIONING paths only.

    Both create and reset call this: a reset re-seeds a demo from scratch, so it
    provisions one too. Demo DELETE does not, so a workspace can always remove a
    demo it already has. Real project create/scan/delete never call this, so
    toggling the flag leaves every non-demo surface untouched.
    """
    if not settings.demo_enabled:
        raise HTTPException(status_code=403, detail="Demo provisioning is disabled")


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    session: SessionDep, request: Request, current_user: CurrentUserDep
) -> list[ProjectResponse]:
    # Only the projects the caller is a member of (every project of the org for
    # an org owner/admin).
    projects = await project_service.list_projects(session, current_user)
    return await _for_caller(session, request, current_user, projects)


@router.post(
    "",
    response_model=ProjectCreateResponse,
    status_code=201,
)
async def create_project(
    session: SessionDep, request: Request, current_user: EditorUserDep, data: ProjectCreate
) -> ProjectCreateResponse:
    # Record the creator so the editor who made a project keeps control of it
    # (see _require_project_manager) without needing an owner for every rename.
    project = await project_service.create_project(session, data, created_by=current_user.id)
    await _record_lifecycle(
        session,
        current_user,
        "project.create",
        project_id=project.id,
        name=project.name,
        slug=project.slug,
        payload=data.model_dump(),
    )
    if project.template_branch_id is not None and data.template_id is not None:
        # The template's draft branch is a branch a person opened, so it files
        # the same row POST /branches does (F21, GH #274).
        branch_name = project_template_service.resolve_template(data.template_id).branch_name
        await audit_service.record(
            session,
            user=current_user,
            action="plan_branch.create",
            target_type="plan_branch",
            target_id=project.template_branch_id,
            target_name=branch_name,
            project_slug=project.slug,
            payload={"name": branch_name, "template_id": data.template_id},
        )
    annotated = await _one_for_caller(session, request, current_user, project)
    return ProjectCreateResponse.model_validate(
        {**annotated.model_dump(), "template_branch_id": project.template_branch_id}
    )


@router.post(
    "/demo",
    response_model=ProjectResponse,
    status_code=201,
)
async def create_demo_project(
    session: SessionDep, request: Request, current_user: EditorUserDep
) -> ProjectResponse:
    _require_demo_enabled()
    project = await demo_service.create_demo_project(session, created_by=current_user.id)
    # A demo is a project, and generating one is a person's decision — so it files
    # the same action a hand-made project does. The recipe's own backfilled rows
    # are the ones marked ``demo_seed``; this one is not, because it describes
    # something a user really did.
    await _record_lifecycle(
        session,
        current_user,
        "project.create",
        project_id=project.id,
        name=project.name,
        slug=project.slug,
        payload={"is_demo": True},
    )
    return await _one_for_caller(session, request, current_user, project)


@router.post("/demo/cancel", response_model=DemoCancelResponse)
async def cancel_demo_provisioning(
    session: SessionDep, current_user: EditorUserDep
) -> DemoCancelResponse:
    """Abandon this user's in-flight demo provision, if one is still seeding.

    Deliberately ungated by the kill switch: it only ever removes work. Declared
    before ``/demo/{slug}/reset`` so the literal path is never shadowed.
    """
    return await demo_service.request_demo_cancel(session, created_by=current_user.id)


@router.post("/demo/{slug}/reset", response_model=ProjectResponse)
async def reset_demo_project(
    session: SessionDep,
    request: Request,
    current_user: EditorUserDep,
    project_role: ProjectRoleDep,
    slug: str,
) -> ProjectResponse:
    """Re-seed a demo in place. Restricted to the demo's creator or an owner."""
    # Reset re-provisions the demo from scratch, so it IS a provisioning path and
    # the kill switch has to gate it too — otherwise flipping the switch off still
    # left a full re-seed one click away (tripl-2su6.16). Delete deliberately
    # stays ungated, so a workspace can never be stuck with a demo it can't remove.
    _require_demo_enabled()
    project = await project_lookup.resolve_project(session, slug)
    if not project.is_demo:
        raise HTTPException(status_code=404, detail="Demo project not found")
    _require_demo_manager(project_role, current_user, project)
    replacement = await demo_service.reset_demo_project(session, slug, created_by=current_user.id)
    # Recorded against the REPLACEMENT, and after it exists, so the row survives:
    # the reset drops the old demo's audit rows by its id (tripl-wkwv.16), and a
    # row filed against the project being destroyed would go with them. It is also
    # the only thing that explains why the trail below it starts fresh.
    await _record_lifecycle(
        session,
        current_user,
        "project.reset",
        project_id=replacement.id,
        name=replacement.name,
        slug=replacement.slug,
        payload={"is_demo": True},
    )
    return await _one_for_caller(session, request, current_user, replacement)


@router.delete("/demo/{slug}", status_code=204)
async def delete_demo_project(
    session: SessionDep, current_user: EditorUserDep, project_role: ProjectRoleDep, slug: str
) -> None:
    """Delete a demo and its owned synthetic warehouse. Creator or owner only."""
    project = await project_lookup.resolve_project(session, slug)
    if not project.is_demo:
        raise HTTPException(status_code=404, detail="Demo project not found")
    _require_demo_manager(project_role, current_user, project)
    # Captured before the delete: afterwards the row is the only thing that knows
    # what the id pointed at.
    project_id, name = project.id, project.name
    await project_service.delete_project(session, slug)
    await _record_lifecycle(
        session,
        current_user,
        "project.delete",
        project_id=project_id,
        name=name,
        slug=slug,
        payload={"is_demo": True},
    )


@router.get("/{slug}", response_model=ProjectResponse)
async def get_project(
    session: SessionDep,
    request: Request,
    current_user: CurrentUserDep,
    slug: str,
    branch_id: BranchIdDep,
) -> ProjectResponse:
    project = await project_service.get_project(session, slug, branch_id=branch_id)
    return await _one_for_caller(session, request, current_user, project)


@router.patch("/{slug}", response_model=ProjectResponse)
async def update_project(
    session: SessionDep,
    request: Request,
    current_user: EditorUserDep,
    project_role: ProjectRoleDep,
    slug: str,
    data: ProjectUpdate,
) -> ProjectResponse:
    project = await project_lookup.resolve_project(session, slug)
    _require_project_manager(project_role, current_user, project)
    updated = await project_service.update_project(session, slug, data)
    # Named as it stands AFTER the edit, like every other update row, and filed
    # under the NEW slug — the only slug this project answers to from here on.
    #
    # Which leaves the rows written BEFORE it carrying the old one. That is why
    # ``audit_service.list_entries`` resolves a slug to a project and filters on
    # the id rather than matching the label (tripl-wkwv.18): every row on both
    # sides of a rename carries the same ``project_id``, so the trail stays whole
    # as long as the reader asks by project rather than by name.
    await _record_lifecycle(
        session,
        current_user,
        "project.update",
        project_id=updated.id,
        name=updated.name,
        slug=updated.slug,
        payload=data.model_dump(exclude_unset=True),
    )
    return await _one_for_caller(session, request, current_user, updated)


# ``current_user`` as a parameter rather than a route dependency: the same
# ``get_owner_user`` gate, but the handler now needs the user it resolves in
# order to say WHO deleted the project.
@router.delete("/{slug}", status_code=204)
async def delete_project(session: SessionDep, current_user: OwnerUserDep, slug: str) -> None:
    project = await project_lookup.resolve_project(session, slug)
    # Read before the delete: afterwards this row is the only thing that knows
    # what the id pointed at.
    project_id, name = project.id, project.name
    await project_service.delete_project(session, slug)
    await _record_lifecycle(
        session,
        current_user,
        "project.delete",
        project_id=project_id,
        name=name,
        slug=slug,
    )


@router.post("/{slug}/danger/reset-anomalies", response_model=AnomalyResetCounts)
async def reset_anomalies(
    session: SessionDep,
    current_user: OwnerUserDep,
    slug: str,
    period: DetectionResetPeriod,
) -> AnomalyResetCounts:
    """Owner-only: clear every anomaly (+ breakdown) in the project's period.

    Destructive and irreversible. Derived monitoring signals disappear with the
    anomalies they are computed from. ``dry_run`` only counts (ST-39).
    """
    project = await project_lookup.resolve_project(session, slug)
    counts = await detection_reset_service.reset_project_anomalies(
        session,
        project.id,
        before=period.before,
        after=period.after,
        dry_run=period.dry_run,
    )
    if not period.dry_run:
        await audit_service.record(
            session,
            user=current_user,
            action="project.reset_anomalies",
            target_type="project",
            target_id=project.id,
            target_name=project.name,
            project=project,
            payload={"before": period.before, "after": period.after, "counts": counts},
        )
    return AnomalyResetCounts(**counts)


@router.post("/{slug}/danger/reset-drifts", response_model=DriftResetCounts)
async def reset_drifts(
    session: SessionDep,
    current_user: OwnerUserDep,
    slug: str,
    period: DetectionResetPeriod,
) -> DriftResetCounts:
    """Owner-only: clear every schema + distribution drift in the project's period.

    Destructive and irreversible. ``dry_run`` only counts (ST-39).
    """
    project = await project_lookup.resolve_project(session, slug)
    counts = await detection_reset_service.reset_project_drifts(
        session,
        project.id,
        before=period.before,
        after=period.after,
        dry_run=period.dry_run,
    )
    if not period.dry_run:
        await audit_service.record(
            session,
            user=current_user,
            action="project.reset_drifts",
            target_type="project",
            target_id=project.id,
            target_name=project.name,
            project=project,
            payload={"before": period.before, "after": period.after, "counts": counts},
        )
    return DriftResetCounts(**counts)


@router.post(
    "/{slug}/danger/retire-unused-variables",
    response_model=VariableRetirementCounts,
)
async def retire_unused_variables(
    session: SessionDep,
    current_user: OwnerUserDep,
    slug: str,
    branch_id: BranchIdDep,
    data: VariableRetirementRequest,
) -> VariableRetirementCounts:
    """Owner-only: drop the variables a scan minted that nothing refers to.

    A scan creates a variable for every placeholder it discovers and has never
    retired one, so a project whose warehouse holds a JSON column keyed by
    user-typed text accumulates a row per key forever (tripl-10h4). This deletes
    only rows that a scan created, no human has edited, no event field value
    names, and that carry no observed context, drift or override — see
    ``core.variable_retirement`` for why "no observed context" alone is not
    enough to be safe.

    ``dry_run`` defaults to true, so the first call is always a preview. It
    returns the same counts the real pass would, broken down by why each
    surviving row was kept.
    """
    project = await project_lookup.resolve_project(session, slug)
    counts = await variable_retirement_service.retire_unused_variables(
        session,
        project_id=project.id,
        branch_id=branch_id,
        slug=slug,
        mode=data.mode,
        dry_run=data.dry_run,
    )
    if not data.dry_run:
        await audit_service.record(
            session,
            user=current_user,
            action="project.retire_unused_variables",
            target_type="project",
            target_id=project.id,
            target_name=project.name,
            project=project,
            payload={"mode": data.mode, "counts": counts},
        )
    return VariableRetirementCounts(**counts)
