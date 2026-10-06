"""The write permissions inside a project, and which one each project route needs.

A project editor may write everything in the project; an extension may take
single permissions away from one (``Extension.project_permission_check``), never
add one. This module names those permissions and maps every editor-gated
``/projects/{slug}/...`` route to exactly one of them, so the write gate
(``api.deps.require_project_mutation_access``) can ask.

The vocabulary is small on purpose, one entry per area an organization would
want to hand out separately. Owners and admins of the project's organization
hold every permission and are never asked about; viewers hold none and are
refused before.

The mapping reads the route's path template (``/branches/{branch_id}/merge``),
not the request path, and falls back to :data:`SETTINGS_MANAGE`, the widest
permission, for a route nothing below names: a new route is held to the
strictest check until someone classifies it. ``tests/test_project_permissions``
walks the live routes and fails on such a fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PLAN_EDIT = "plan.edit"
PLAN_MERGE = "plan.merge"
COMMENTS_WRITE = "comments.write"
DOCS_EDIT = "docs.edit"
METRICS_MANAGE = "metrics.manage"
ALERTS_MANAGE = "alerts.manage"
DATA_SOURCES_MANAGE = "data_sources.manage"
SETTINGS_MANAGE = "settings.manage"


@dataclass(frozen=True)
class Permission:
    key: str
    label: str
    description: str


PERMISSIONS: tuple[Permission, ...] = (
    Permission(
        PLAN_EDIT,
        "Edit the tracking plan",
        "Events, event types, fields, properties, relations, planned events, branches "
        "and their reviews, reconciliation.",
    ),
    Permission(PLAN_MERGE, "Merge branches", "Merge a branch into the plan, or revert a merge."),
    Permission(COMMENTS_WRITE, "Comment", "Comments on events, screenshots and branches."),
    Permission(DOCS_EDIT, "Edit docs", "The project's notes, folders, translations and sharing."),
    Permission(
        METRICS_MANAGE, "Manage metrics", "The metrics catalog, its previews and chart annotations."
    ),
    Permission(
        ALERTS_MANAGE,
        "Manage alerts",
        "Alert destinations and rules, the alert inbox, monitors, anomaly signals and settings.",
    ),
    Permission(
        DATA_SOURCES_MANAGE,
        "Run data scans",
        "Run or cancel a scan, the project's fact tables, and reading a warehouse's tables "
        "and columns through the project.",
    ),
    Permission(
        SETTINGS_MANAGE,
        "Manage the project",
        "The project's own settings, its members, the search index and demo resets.",
    ),
)

PERMISSION_KEYS: frozenset[str] = frozenset(p.key for p in PERMISSIONS)

_SEGMENT = r"(?:/|$)"

#: ``(pattern over the path after "{slug}", permission)``; the first match wins.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), permission)
    for pattern, permission in (
        (r"^/branches/\{branch_id\}/(?:merge|revert)$", PLAN_MERGE),
        (r"/comments" + _SEGMENT, COMMENTS_WRITE),
        (r"^/docs" + _SEGMENT, DOCS_EDIT),
        (
            r"^/(?:alert-destinations|alert-deliveries|alert-inbox|monitors|anomalies|signals"
            r"|anomaly-settings)" + _SEGMENT,
            ALERTS_MANAGE,
        ),
        (r"^/(?:metrics|annotations)" + _SEGMENT, METRICS_MANAGE),
        (r"^/(?:scans|fact-tables)" + _SEGMENT, DATA_SOURCES_MANAGE),
        (
            r"^/(?:events|event-types|properties|variables|meta-fields|relations"
            r"|planned-events|duplicates|reconciliation|revisions|branches|ai|plan)" + _SEGMENT,
            PLAN_EDIT,
        ),
        (r"^/(?:members|search|reset)" + _SEGMENT, SETTINGS_MANAGE),
        (r"^/?$", SETTINGS_MANAGE),
    )
)


def classify(route_path: str) -> str | None:
    """The permission a project route's path template needs; ``None`` when unclassified.

    ``route_path`` is the full template (``/api/v1/projects/{slug}/events``);
    the part after ``{slug}`` decides. A path without ``{slug}`` is ``None``.
    """
    _, sep, rest = route_path.partition("{slug}")
    if not sep:
        return None
    for pattern, permission in _RULES:
        if pattern.search(rest):
            return permission
    return None


def permission_for(route_path: str) -> str:
    """:func:`classify`, falling back to :data:`SETTINGS_MANAGE` (the widest)."""
    return classify(route_path) or SETTINGS_MANAGE
