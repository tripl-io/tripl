from fastapi import APIRouter, Depends

from tripl.api.deps import get_current_user, require_project_membership
from tripl.api.v1.activity import router as activity_router
from tripl.api.v1.ai import router as ai_router
from tripl.api.v1.alerting import router as alerting_router
from tripl.api.v1.api_keys import router as api_keys_router
from tripl.api.v1.app_settings import router as app_settings_router
from tripl.api.v1.audit import router as audit_router
from tripl.api.v1.auth import router as auth_router
from tripl.api.v1.chart_annotations import router as chart_annotations_router
from tripl.api.v1.data_sources import router as data_sources_router
from tripl.api.v1.dependencies import router as dependencies_router
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
from tripl.api.v1.lifecycle import router as lifecycle_router
from tripl.api.v1.meta_fields import router as meta_fields_router
from tripl.api.v1.metrics import router as metrics_router
from tripl.api.v1.metrics_catalog import router as metrics_catalog_router
from tripl.api.v1.notifications import router as notifications_router
from tripl.api.v1.plan_branches import router as plan_branches_router
from tripl.api.v1.plan_export import router as plan_export_router
from tripl.api.v1.plan_revisions import router as plan_revisions_router
from tripl.api.v1.plan_validation import router as plan_validation_router
from tripl.api.v1.project_anomaly_settings import router as project_anomaly_settings_router
from tripl.api.v1.project_branch_settings import router as project_branch_settings_router
from tripl.api.v1.project_members import router as project_members_router
from tripl.api.v1.project_tracker_config import router as project_tracker_config_router
from tripl.api.v1.projects import router as projects_router
from tripl.api.v1.reconciliation import router as reconciliation_router
from tripl.api.v1.relations import router as relations_router
from tripl.api.v1.scans import router as scans_router
from tripl.api.v1.scans import source_freshness_router
from tripl.api.v1.search import router as search_router
from tripl.api.v1.users import router as users_router
from tripl.api.v1.variables import router as variables_router

router = APIRouter(prefix="/api/v1")
# Authentication, then project membership: every ``/projects/{slug}/...`` route
# (and ``/activity/projects/{slug}``) answers 404 to a non-member before the
# route's own dependencies or handler run. Router-level dependencies are solved
# ahead of the route's, in this order.
protected_dependencies = [Depends(get_current_user), Depends(require_project_membership)]

router.include_router(auth_router)
router.include_router(activity_router, dependencies=protected_dependencies)
router.include_router(ai_router, dependencies=protected_dependencies)
router.include_router(app_settings_router, dependencies=protected_dependencies)
router.include_router(projects_router, dependencies=protected_dependencies)
router.include_router(project_anomaly_settings_router, dependencies=protected_dependencies)
router.include_router(project_branch_settings_router, dependencies=protected_dependencies)
router.include_router(project_members_router, dependencies=protected_dependencies)
router.include_router(project_tracker_config_router, dependencies=protected_dependencies)
router.include_router(alerting_router, dependencies=protected_dependencies)
router.include_router(event_types_router, dependencies=protected_dependencies)
router.include_router(event_type_owners_router, dependencies=protected_dependencies)
router.include_router(project_event_type_owners_router, dependencies=protected_dependencies)
router.include_router(fields_router, dependencies=protected_dependencies)
router.include_router(relations_router, dependencies=protected_dependencies)
router.include_router(meta_fields_router, dependencies=protected_dependencies)
# Registered BEFORE events_router so `/projects/{slug}/events/stream` is matched
# by the SSE route and not captured by `/projects/{slug}/events/{event_id}`.
router.include_router(events_stream_router, dependencies=protected_dependencies)
# Also BEFORE events_router: `/projects/{slug}/events/duplicate-check` (GH #265).
router.include_router(duplicate_check_router, dependencies=protected_dependencies)
router.include_router(events_router, dependencies=protected_dependencies)
router.include_router(lifecycle_router, dependencies=protected_dependencies)
router.include_router(health_router, dependencies=protected_dependencies)
router.include_router(event_photos_router, dependencies=protected_dependencies)
router.include_router(event_comments_router, dependencies=protected_dependencies)
router.include_router(variables_router, dependencies=protected_dependencies)
router.include_router(data_sources_router, dependencies=protected_dependencies)
router.include_router(scans_router, dependencies=protected_dependencies)
router.include_router(source_freshness_router, dependencies=protected_dependencies)
router.include_router(search_router, dependencies=protected_dependencies)
router.include_router(metrics_router, dependencies=protected_dependencies)
router.include_router(metrics_catalog_router, dependencies=protected_dependencies)
router.include_router(fact_tables_router, dependencies=protected_dependencies)
router.include_router(chart_annotations_router, dependencies=protected_dependencies)
router.include_router(plan_branches_router, dependencies=protected_dependencies)
router.include_router(dependencies_router, dependencies=protected_dependencies)
router.include_router(implementation_tickets_router, dependencies=protected_dependencies)
router.include_router(event_implementation_tickets_router, dependencies=protected_dependencies)
router.include_router(plan_revisions_router, dependencies=protected_dependencies)
router.include_router(plan_validation_router, dependencies=protected_dependencies)
router.include_router(plan_export_router, dependencies=protected_dependencies)
router.include_router(reconciliation_router, dependencies=protected_dependencies)
router.include_router(duplicates_router, dependencies=protected_dependencies)
router.include_router(audit_router, dependencies=protected_dependencies)
router.include_router(users_router, dependencies=protected_dependencies)
router.include_router(api_keys_router, dependencies=protected_dependencies)
router.include_router(notifications_router, dependencies=protected_dependencies)
