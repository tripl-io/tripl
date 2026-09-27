"""Project members: who can see a project, and who can change it.

``GET`` is open to every member (a non-member never gets here: the project
access dependency answers 404 for the whole ``/projects/{slug}/...`` surface).
Adding, re-roling and removing members is for the instance owner and the
project's creator, and only from a browser session: membership is access
control, so a leaked API key must not be able to grant itself (or anyone else)
a project.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request, status

from tripl.api.deps import CurrentUserDep, EditorUserDep, SessionDep
from tripl.schemas.project_member import (
    ProjectMemberCreate,
    ProjectMemberResponse,
    ProjectMemberUpdate,
)
from tripl.services import audit_service, project_member_service
from tripl.services.project_lookup import resolve_project

router = APIRouter(prefix="/projects/{slug}/members", tags=["project-members"])


def _require_session_auth(request: Request) -> None:
    if getattr(request.state, "api_key_scope", None) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project membership management requires a user session",
        )


@router.get("", response_model=list[ProjectMemberResponse])
async def list_members(
    session: SessionDep, _current_user: CurrentUserDep, slug: str
) -> list[ProjectMemberResponse]:
    project = await resolve_project(session, slug)
    return await project_member_service.list_members(session, project.id)


@router.post("", response_model=ProjectMemberResponse, status_code=201)
async def add_member(
    request: Request,
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    data: ProjectMemberCreate,
) -> ProjectMemberResponse:
    _require_session_auth(request)
    project = await resolve_project(session, slug)
    project_member_service.require_member_manager(current_user, project)
    member = await project_member_service.add_member(
        session, project, user_id=data.user_id, role=data.role, added_by=current_user.id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="project.member_add",
        target_type="project_member",
        target_id=member.user_id,
        target_name=member.email,
        project=project,
        payload={"user_id": str(member.user_id), "role": member.role.value},
    )
    return member


@router.patch("/{user_id}", response_model=ProjectMemberResponse)
async def update_member(
    request: Request,
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    user_id: uuid.UUID,
    data: ProjectMemberUpdate,
) -> ProjectMemberResponse:
    _require_session_auth(request)
    project = await resolve_project(session, slug)
    project_member_service.require_member_manager(current_user, project)
    member, previous = await project_member_service.update_member(
        session, project, user_id=user_id, role=data.role
    )
    await audit_service.record(
        session,
        user=current_user,
        action="project.member_update",
        target_type="project_member",
        target_id=member.user_id,
        target_name=member.email,
        project=project,
        payload={
            "user_id": str(member.user_id),
            "role": member.role.value,
            "previous_role": previous,
        },
    )
    return member


@router.delete("/{user_id}", status_code=204)
async def remove_member(
    request: Request,
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    user_id: uuid.UUID,
) -> None:
    _require_session_auth(request)
    project = await resolve_project(session, slug)
    project_member_service.require_member_manager(current_user, project)
    removed = await project_member_service.remove_member(session, project, user_id=user_id)
    await audit_service.record(
        session,
        user=current_user,
        action="project.member_remove",
        target_type="project_member",
        target_id=removed.user_id,
        target_name=removed.email,
        project=project,
        payload={"user_id": str(removed.user_id), "role": removed.role.value},
    )
