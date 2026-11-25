"""Plan validation route (GH #261, F08): ``tripl check`` asks the plan here.

A read: no write gate, so a viewer member and a ``read``-scope API key (the one
a CI job should hold) may call it, and the membership gate mounted in
``api.v1.router`` answers 404 to a non-member. It is a POST only to carry the
batch (listed with the other read-like POSTs in
``tests/test_rbac.READ_LIKE_MUTATING_PATHS``). ``?branch=`` validates against a
plan branch instead of main; a merged or closed branch is still readable.
``?revision=`` validates against a stored plan revision instead of the live
plan, so a pipeline gives the same verdicts whenever it runs.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from tripl.api.deps import BranchIdDep, SessionDep
from tripl.schemas.plan_validation import PlanValidationRequest, PlanValidationResponse
from tripl.services import plan_validation_service
from tripl.services.project_lookup import resolve_project_id

router = APIRouter(prefix="/projects/{slug}", tags=["plan-validation"])


@router.post("/plan/validate", response_model=PlanValidationResponse)
async def validate_plan(
    session: SessionDep,
    slug: str,
    data: PlanValidationRequest,
    branch_id: BranchIdDep,
    revision: Annotated[
        uuid.UUID | None,
        Query(
            description=(
                "Validate against this stored plan revision (`GET /revisions`) "
                "instead of the live plan. Not with `branch`."
            )
        ),
    ] = None,
) -> PlanValidationResponse:
    """One verdict per tracking call: known event, allowed values, contracts.

    Up to 5000 items per request. A ``null`` field value means "set at runtime"
    and is never an error. Changes nothing. ``plan`` in the answer names the
    revision that judged the calls, or is empty for the live plan.
    """
    if revision is not None and branch_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Pass revision or branch, not both: a revision is already one fixed plan.",
        )
    project_id = await resolve_project_id(session, slug)
    return await plan_validation_service.validate_plan(
        session, project_id=project_id, branch_id=branch_id, data=data, revision_id=revision
    )
