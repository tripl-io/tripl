from typing import Any

from fastapi import APIRouter, Depends, params

from tripl import extensions
from tripl.api.deps import (
    get_current_user,
    refuse_writes_on_public_demo,
    require_project_membership,
)
from tripl.api.v1.activity import router as activity_router
from tripl.api.v1.ai import router as ai_router
from tripl.api.v1.alerting import router as alerting_router
from tripl.api.v1.api_keys import router as api_keys_router
from tripl.api.v1.app_settings import router as app_settings_router
from tripl.api.v1.auth import router as auth_router
from tripl.api.v1.auth_google import router as auth_google_router
from tripl.api.v1.auth_oidc import router as auth_oidc_router
from tripl.api.v1.chart_annotations import router as chart_annotations_router
from tripl.api.v1.data_sources import router as data_sources_router
from tripl.api.v1.dependencies import router as dependencies_router
from tripl.api.v1.docs import router as docs_router
from tripl.api.v1.duplicates import check_router as duplicate_check_router
from tripl.api.v1.duplicates import router as duplicates_router
from tripl.api.v1.event_comments import router as event_comments_router
from tripl.api.v1.event_photos import router as event_photos_router
from tripl.api.v1.event_type_owners import project_router as project_event_type_owners_router
from tripl.api.v1.event_type_owners import router as event_type_owners_router
from tripl.api.v1.event_types import router as event_types_router
from tripl.api.v1.events import router as events_router
from tripl.api.v1.events_stream import router as events_stream_router
from tripl.api.v1.fact_tables import router as fact_tables_router
from tripl.api.v1.fields import router as fields_router
from tripl.api.v1.health import router as health_router
from tripl.api.v1.implementation_tickets import (
    event_router as event_implementation_tickets_router,
)
from tripl.api.v1.implementation_tickets import router as implementation_tickets_router
from tripl.api.v1.incident_summaries import router as incident_summaries_router
from tripl.api.v1.lifecycle import router as lifecycle_router
from tripl.api.v1.meta_fields import router as meta_fields_router
from tripl.api.v1.metrics import router as metrics_router
from tripl.api.v1.metrics_catalog import router as metrics_catalog_router
from tripl.api.v1.notifications import router as notifications_router
from tripl.api.v1.org_groups import router as org_groups_router
from tripl.api.v1.org_settings import router as org_settings_router
from tripl.api.v1.orgs import router as orgs_router
from tripl.api.v1.plan_branches import router as plan_branches_router
from tripl.api.v1.plan_export import router as plan_export_router
from tripl.api.v1.plan_revisions import router as plan_revisions_router
from tripl.api.v1.plan_validation import router as plan_validation_router
from tripl.api.v1.planned_events import router as planned_events_router
from tripl.api.v1.platform_settings import router as platform_settings_router
from tripl.api.v1.project_anomaly_settings import router as project_anomaly_settings_router
from tripl.api.v1.project_audit import router as project_audit_router
from tripl.api.v1.project_branch_settings import router as project_branch_settings_router
from tripl.api.v1.project_members import router as project_members_router
from tripl.api.v1.project_templates import router as project_templates_router
from tripl.api.v1.project_tracker_config import router as project_tracker_config_router
from tripl.api.v1.projects import router as projects_router
from tripl.api.v1.reconciliation import router as reconciliation_router
from tripl.api.v1.relations import router as relations_router
from tripl.api.v1.scans import router as scans_router
from tripl.api.v1.scans import source_freshness_router
from tripl.api.v1.search import router as search_router
from tripl.api.v1.users import router as users_router
from tripl.api.v1.variables import properties_router
from tripl.api.v1.variables import router as variables_router
from tripl.schemas.errors import AUTH_ERROR_RESPONSES, PROJECT_ERROR_RESPONSES

router = APIRouter(prefix="/api/v1")
# Authentication, then project membership: every ``/projects/{slug}/...`` route
# (and ``/activity/projects/{slug}``) answers 404 to a non-member before the
# route's own dependencies or handler run. Router-level dependencies are solved
# ahead of the route's, in this order.
protected_dependencies = [Depends(get_current_user), Depends(require_project_membership)]


def _no_outbound(what: str) -> params.Depends:
    """Every write here configures traffic out of the instance: off on a public demo."""
    dependency: params.Depends = Depends(refuse_writes_on_public_demo(what))
    return dependency


def _include_protected(
    child: APIRouter,
    *,
    project_scoped: bool = True,
    outbound: str | None = None,
) -> None:
    """Mount ``child`` behind ``protected_dependencies`` and document what they answer.

    Every protected route may answer 401 and 403 with an ``ErrorResponse`` body;
    a ``project_scoped`` router (any route under ``/projects/{slug}``) also the
    404 a missing project or a non-member gets. ``outbound`` adds the
    public-demo refusal of :func:`_no_outbound`.
    """
    dependencies = list(protected_dependencies)
    if outbound is not None:
        dependencies.append(_no_outbound(outbound))
    responses: dict[int | str, dict[str, Any]] = (
        PROJECT_ERROR_RESPONSES if project_scoped else AUTH_ERROR_RESPONSES
    )
    router.include_router(child, dependencies=dependencies, responses=responses)


# Extensions' routers come first, so a core route with a path parameter never
# claims one of theirs. Theirs are organization- or platform-scoped, so they
# document the 401/403 of signing in and not the project 404.
for _extension in extensions.extensions():
    for _mounted in _extension.api_routers():
        if _mounted.protected:
            _include_protected(_mounted.router, project_scoped=False, outbound=_mounted.outbound)
        elif _mounted.outbound is not None:
            router.include_router(_mounted.router, dependencies=[_no_outbound(_mounted.outbound)])
        else:
            router.include_router(_mounted.router)

router.include_router(auth_router)
router.include_router(auth_google_router)
router.include_router(auth_oidc_router)
# Mixed: ``/activity`` is the caller's own feed, ``/activity/projects/{slug}`` a project's.
_include_protected(activity_router)
_include_protected(ai_router)
_include_protected(app_settings_router, project_scoped=False)
# ``/projects`` lists and creates; everything else under it names a project.
_include_protected(projects_router)
_include_protected(project_anomaly_settings_router)
_include_protected(project_branch_settings_router)
_include_protected(project_members_router)
_include_protected(project_templates_router, project_scoped=False)
_include_protected(project_tracker_config_router, outbound="file tickets in an issue tracker")
_include_protected(alerting_router)
_include_protected(incident_summaries_router)
_include_protected(event_types_router)
_include_protected(event_type_owners_router)
_include_protected(project_event_type_owners_router)
_include_protected(fields_router)
_include_protected(relations_router)
_include_protected(meta_fields_router)
# Registered BEFORE events_router so `/projects/{slug}/events/stream` is matched
# by the SSE route and not captured by `/projects/{slug}/events/{event_id}`.
_include_protected(events_stream_router)
# Also BEFORE events_router: `/projects/{slug}/events/duplicate-check` (GH #265).
_include_protected(duplicate_check_router)
_include_protected(events_router)
_include_protected(lifecycle_router)
_include_protected(health_router)
_include_protected(event_photos_router)
_include_protected(event_comments_router)
_include_protected(properties_router)
_include_protected(variables_router)
_include_protected(data_sources_router, project_scoped=False)
_include_protected(scans_router)
_include_protected(source_freshness_router)
_include_protected(search_router)
_include_protected(metrics_router)
_include_protected(metrics_catalog_router)
_include_protected(fact_tables_router)
_include_protected(docs_router)
_include_protected(chart_annotations_router)
_include_protected(planned_events_router)
_include_protected(plan_branches_router)
_include_protected(dependencies_router)
_include_protected(implementation_tickets_router)
_include_protected(event_implementation_tickets_router)
_include_protected(plan_revisions_router)
_include_protected(plan_validation_router)
_include_protected(plan_export_router)
_include_protected(reconciliation_router)
_include_protected(duplicates_router)
_include_protected(project_audit_router)
_include_protected(users_router, project_scoped=False)
_include_protected(api_keys_router, project_scoped=False)
_include_protected(notifications_router, project_scoped=False)
# Organization management (F20 PR6): real ``/orgs`` routes, never rewritten.
_include_protected(orgs_router, project_scoped=False)
_include_protected(
    org_settings_router, project_scoped=False, outbound="change organization settings"
)
_include_protected(org_groups_router, project_scoped=False)
_include_protected(platform_settings_router, project_scoped=False)
