"""Built-in project templates (F21, GH #274).

Its own prefix, not ``/projects/templates``: that path would collide with
``GET /projects/{slug}`` for a project slugged ``templates``. Authenticated but
not project-scoped; the membership gate lets slug-less routes through.
"""

from fastapi import APIRouter

from tripl.api.deps import CurrentUserDep
from tripl.schemas.project_template import ProjectTemplateSummary
from tripl.services import project_template_service

router = APIRouter(prefix="/project-templates", tags=["project-templates"])


@router.get("", response_model=list[ProjectTemplateSummary])
async def list_project_templates(_current_user: CurrentUserDep) -> list[ProjectTemplateSummary]:
    return project_template_service.list_template_summaries()
