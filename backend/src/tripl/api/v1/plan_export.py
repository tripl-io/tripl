"""Plan export route (GH #262, F09): the plan as JSON Schema or a codegen model.

A read: no write gate, so a viewer member and a ``read``-scope API key (what a
CI job running ``tripl codegen --check`` should hold) may call it, and the
membership gate mounted in ``api.v1.router`` answers 404 to a non-member.
``?branch=`` exports a plan branch instead of main; a merged or closed branch
is still readable. Archived events are left out, deprecated ones are flagged.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from tripl.api.deps import BranchIdDep, SessionDep
from tripl.schemas.plan_export import (
    PlanExportCodegenModel,
    PlanExportFormat,
    PlanExportJsonSchemaBundle,
)
from tripl.services import plan_export_service
from tripl.services.project_lookup import resolve_project_id

router = APIRouter(prefix="/projects/{slug}", tags=["plan-export"])


@router.get(
    "/plan/export",
    response_model=PlanExportJsonSchemaBundle | PlanExportCodegenModel,
)
async def export_plan(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    export_format: Annotated[
        PlanExportFormat,
        Query(
            alias="format",
            description=(
                "``jsonschema``: one JSON Schema (draft 2020-12) per event. "
                "``codegen_model``: the plan shaped for ``tripl codegen``."
            ),
        ),
    ] = "jsonschema",
) -> PlanExportJsonSchemaBundle | PlanExportCodegenModel:
    """The plan of main (or ``?branch=``) as a schema bundle or a codegen model.

    Deterministic: the same plan exports byte-identical, with the plan
    revision, the branch and a content hash. Changes nothing.
    """
    project_id = await resolve_project_id(session, slug)
    return await plan_export_service.export_plan(
        session, project_id=project_id, branch_id=branch_id, export_format=export_format
    )
