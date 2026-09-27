"""Dependency graph and impact analysis routes (GH #257).

All three are reads: no write gate, so a viewer member may call them, and the
membership gate mounted in ``api.v1.router`` answers 404 to a non-member.
``POST /impact`` is a POST only to carry the change set (listed with the other
read-like POSTs in ``tests/test_rbac.READ_LIKE_MUTATING_PATHS``). Nothing here
refuses a change: the answers are warnings for the UI's confirm dialogs.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from tripl.api.deps import BranchIdDep, SessionDep
from tripl.schemas.dependency import DependenciesResponse, ImpactRequest, ImpactResponse
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import dependency_service
from tripl.services.project_lookup import resolve_project_id

router = APIRouter(prefix="/projects/{slug}", tags=["dependencies"])


@router.get("/dependencies", response_model=DependenciesResponse)
async def get_dependencies(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    entity: Annotated[
        # The NUL guard every string query parameter carries (test_text_filters);
        # the value is parsed to a kind and a UUID before anything binds it.
        FreeTextFilter,
        Query(
            description=(
                "The entity to explain, as '<kind>:<uuid>' with kind one of event, "
                "event_type, field, variable, metric, fact_table, alert_rule, relation."
            ),
            max_length=64,
        ),
    ],
    depth: Annotated[int, Query(ge=1, le=2)] = 1,
) -> DependenciesResponse:
    """Upstream and downstream edges of one entity, on ``?branch=`` or main.

    An id that no longer resolves answers ``entity.exists = false`` with any
    project-wide rows that still name it, rather than 404.
    """
    try:
        ref = dependency_service.parse_entity_ref(entity)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    project_id = await resolve_project_id(session, slug)
    return await dependency_service.dependencies_response(
        session,
        project_id=project_id,
        slug=slug,
        branch_id=branch_id,
        entity=ref,
        depth=depth,
    )


@router.post("/impact", response_model=ImpactResponse)
async def post_impact(
    session: SessionDep,
    slug: str,
    data: ImpactRequest,
    branch_id: BranchIdDep,
) -> ImpactResponse:
    """What each planned delete / deprecate / rename would affect. Changes nothing.

    Always one hop: ``depth`` in the body is accepted for compatibility and
    ignored (use ``GET /dependencies?depth=2`` to walk further).
    """
    project_id = await resolve_project_id(session, slug)
    return await dependency_service.impact_response(
        session,
        project_id=project_id,
        slug=slug,
        branch_id=branch_id,
        changes=data.changes,
    )


@router.get("/branches/{branch_id}/impact", response_model=ImpactResponse)
async def get_branch_impact(session: SessionDep, slug: str, branch_id: uuid.UUID) -> ImpactResponse:
    """The downstream objects a working branch's changes touch, from its diff."""
    return await dependency_service.branch_impact_response(session, slug=slug, branch_id=branch_id)
