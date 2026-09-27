"""Built-in project templates (F21, GH #274), in display order."""

from __future__ import annotations

from tripl.services.project_templates.b2b_saas import TEMPLATE as B2B_SAAS
from tripl.services.project_templates.ecommerce import TEMPLATE as ECOMMERCE
from tripl.services.project_templates.mobile_games import TEMPLATE as MOBILE_GAMES
from tripl.services.project_templates.model import ProjectTemplate
from tripl.services.project_templates.subscriptions import TEMPLATE as SUBSCRIPTIONS

TEMPLATES: tuple[ProjectTemplate, ...] = (ECOMMERCE, SUBSCRIPTIONS, MOBILE_GAMES, B2B_SAAS)

_BY_ID: dict[str, ProjectTemplate] = {template.id: template for template in TEMPLATES}
if len(_BY_ID) != len(TEMPLATES):  # pragma: no cover - import-time guard
    raise RuntimeError("duplicate project template id")


def get_template(template_id: str) -> ProjectTemplate | None:
    return _BY_ID.get(template_id)


def list_templates() -> tuple[ProjectTemplate, ...]:
    return TEMPLATES


__all__ = ["TEMPLATES", "ProjectTemplate", "get_template", "list_templates"]
