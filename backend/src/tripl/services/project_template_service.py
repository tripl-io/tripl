"""Seed a new project's starter plan from a built-in template (F21, GH #274).

The template's plan lands on a reviewable DRAFT branch; the project's main
branch stays empty until someone merges it through the ordinary review flow.

``apply_template`` never commits. It runs inside ``project_service.create_project``'s
single transaction, so a failure anywhere in the seed rolls back the whole
project and nothing half-made is left behind. That is also why it does not call
``plan_branch_service.create_branch``: that service commits the request session
and retries in fresh REPEATABLE READ sessions, which a project that does not
exist yet (uncommitted) cannot survive. The non-committing branch shape here is
the one ``services/demo/builders/branches.py`` uses. No race exists: main is
empty and the project is invisible to every other transaction until the commit,
so the base snapshot is empty and every seeded row reads as "added" in the
branch diff.

Deliberately NOT created: metric definitions, fact tables, data sources, scan
configs, alert destinations and alert rules. A metric needs a data source or a
scan to count anything (and ``MetricDefinition`` has no branch, so it could not
be reviewed), and an alert rule needs a destination the workspace configures
itself. Templates carry those as suggestions, listed in the branch description.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event import Event, EventStatus
from tripl.models.event_change import create_event_change
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.plan_revision import PlanRevision, PlanRevisionKind
from tripl.models.variable import Variable
from tripl.schemas.project_template import (
    ProjectTemplateAlertSuggestionOut,
    ProjectTemplateCounts,
    ProjectTemplateMetricSuggestionOut,
    ProjectTemplateSummary,
)
from tripl.services import notification_announce, subscription_service
from tripl.services.plan_revision_service import build_plan_snapshot
from tripl.services.project_templates import get_template, list_templates
from tripl.services.project_templates.model import ProjectTemplate
from tripl.services.search_service import reindex_project_branch

UNKNOWN_TEMPLATE_DETAIL = "Unknown project template"


def summarize(template: ProjectTemplate) -> ProjectTemplateSummary:
    counts = template.counts()
    return ProjectTemplateSummary(
        id=template.id,
        version=template.version,
        name=template.name,
        description=template.description,
        branch_name=template.branch_name,
        counts=ProjectTemplateCounts(
            event_types=counts.event_types,
            fields=counts.fields,
            events=counts.events,
            variables=counts.variables,
            metric_suggestions=counts.metric_suggestions,
            alert_suggestions=counts.alert_suggestions,
        ),
        event_type_names=[et.name for et in template.event_types],
        metric_suggestions=[
            ProjectTemplateMetricSuggestionOut(
                name=m.name,
                display_name=m.display_name,
                description=m.description,
                kind=m.kind,
                composition=m.composition,
                numerator_event=m.numerator_event,
                denominator_event=m.denominator_event,
                needs=m.needs,
            )
            for m in template.metric_suggestions
        ],
        alert_suggestions=[
            ProjectTemplateAlertSuggestionOut(name=a.name, description=a.description, needs=a.needs)
            for a in template.alert_suggestions
        ],
    )


def list_template_summaries() -> list[ProjectTemplateSummary]:
    return [summarize(template) for template in list_templates()]


def resolve_template(template_id: str) -> ProjectTemplate:
    template = get_template(template_id)
    if template is None:
        raise HTTPException(status_code=422, detail=UNKNOWN_TEMPLATE_DETAIL)
    return template


def render_branch_description(template: ProjectTemplate) -> str:
    """The template branch's Markdown description: provenance plus what to set up next."""
    counts = template.counts()
    lines = [
        f"Starter plan from the **{template.name}** project template (version {template.version}).",
        "",
        f"Adds {counts.event_types} event types, {counts.fields} fields, "
        f"{counts.variables} variables and {counts.events} draft events. "
        "Review, edit and merge this branch to make them the project's plan; "
        "main stays empty until then.",
        "",
        "### Starter metrics (not created: need a data source or scan)",
        "",
        "Set up after merging and connecting a data source:",
        "",
    ]
    for metric in template.metric_suggestions:
        lines.append(f"- [ ] **{metric.display_name}** (`{metric.name}`): {metric.description}")
    lines += [
        "",
        "### Starter alert rules (not created: need an alert destination)",
        "",
        "Set up after merging and connecting an alert destination:",
        "",
    ]
    for alert in template.alert_suggestions:
        lines.append(f"- [ ] `{alert.name}`: {alert.description}")
    return "\n".join(lines) + "\n"


async def apply_template(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    slug: str,
    main_branch_id: uuid.UUID,
    template: ProjectTemplate,
    created_by: uuid.UUID | None,
) -> PlanBranch:
    """Open the template's draft branch and seed its plan. Flushes, never commits."""
    base_payload = await build_plan_snapshot(session, project_id, branch_id=main_branch_id)
    base_revision = PlanRevision(
        project_id=project_id,
        created_by=created_by,
        summary=f"Base snapshot for branch '{template.branch_name}'",
        kind=PlanRevisionKind.branch_base.value,
        payload=base_payload,
    )
    session.add(base_revision)
    await session.flush()

    branch = PlanBranch(
        project_id=project_id,
        name=template.branch_name,
        kind=BranchKind.working.value,
        status=BranchStatus.draft.value,
        description=render_branch_description(template),
        base_revision_id=base_revision.id,
        created_by=created_by,
    )
    session.add(branch)
    await session.flush()
    # The revision is written before the branch exists, so its link back is
    # stamped once the branch has an id (PL-21), as create_branch does.
    base_revision.branch_id = branch.id

    type_ids, field_ids = await _add_event_types(session, project_id, branch.id, template)
    _add_variables(session, project_id, branch.id, template)
    await _add_events(
        session,
        project_id=project_id,
        branch_id=branch.id,
        template=template,
        type_ids=type_ids,
        field_ids=field_ids,
        created_by=created_by,
    )

    await notification_announce.best_effort(
        session,
        "subscribe template branch author",
        lambda: subscription_service.subscribe(
            session,
            user_id=created_by,
            project_id=project_id,
            entity_type=subscription_service.BRANCH,
            entity_id=branch.id,
            reason="author",
        ),
    )
    await session.flush()
    # No embeddings: a queued worker would read the branch before the commit.
    await reindex_project_branch(
        session,
        project_id=project_id,
        branch_id=branch.id,
        slug=slug,
        schedule_embeddings=False,
        commit=False,
    )
    return branch


async def _add_event_types(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    template: ProjectTemplate,
) -> tuple[dict[str, uuid.UUID], dict[tuple[str, str], uuid.UUID]]:
    """Types and fields; returns ``type -> id`` and ``(type, field) -> id``."""
    type_rows: list[tuple[str, EventType]] = []
    for order, spec in enumerate(template.event_types):
        row = EventType(
            project_id=project_id,
            branch_id=branch_id,
            name=spec.name,
            display_name=spec.display_name,
            description=spec.description,
            color=spec.color,
            order=order,
        )
        session.add(row)
        type_rows.append((spec.name, row))
    await session.flush()

    field_rows: list[tuple[str, str, FieldDefinition]] = []
    for (type_name, type_row), spec in zip(type_rows, template.event_types, strict=True):
        for order, fd in enumerate(spec.fields):
            field_row = FieldDefinition(
                event_type_id=type_row.id,
                name=fd.name,
                display_name=fd.display_name,
                field_type=fd.field_type.value,
                is_required=fd.is_required,
                enum_options=list(fd.enum_options) if fd.enum_options is not None else None,
                description=fd.description,
                order=order,
            )
            session.add(field_row)
            field_rows.append((type_name, fd.name, field_row))
    await session.flush()

    type_ids = {name: row.id for name, row in type_rows}
    field_ids = {(type_name, field_name): row.id for type_name, field_name, row in field_rows}
    return type_ids, field_ids


def _add_variables(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    template: ProjectTemplate,
) -> None:
    for spec in template.variables:
        session.add(
            Variable(
                project_id=project_id,
                branch_id=branch_id,
                name=spec.name,
                variable_type=spec.variable_type.value,
                description=spec.description,
                allowed_values=list(spec.allowed_values),
            )
        )


async def _add_events(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    template: ProjectTemplate,
    type_ids: dict[str, uuid.UUID],
    field_ids: dict[tuple[str, str], uuid.UUID],
    created_by: uuid.UUID | None,
) -> None:
    rows: list[tuple[int, Event]] = []
    for order, spec in enumerate(template.events):
        row = Event(
            project_id=project_id,
            branch_id=branch_id,
            event_type_id=type_ids[spec.event_type],
            name=spec.name,
            title=spec.title,
            description=spec.description,
            order=order,
            status=EventStatus.draft.value,
            metric_breakdown_columns=[],
        )
        session.add(row)
        rows.append((order, row))
    await session.flush()

    for order, row in rows:
        spec = template.events[order]
        for field_name, value in spec.field_values:
            session.add(
                EventFieldValue(
                    event_id=row.id,
                    field_definition_id=field_ids[(spec.event_type, field_name)],
                    value=value,
                    # Authored, exactly as if typed in the event editor, so a
                    # later scan never rewrites a documented ``${variable}``.
                    is_authored=True,
                )
            )
        for tag in spec.tags:
            session.add(EventTag(event_id=row.id, name=tag))
        session.add(
            create_event_change(
                event_id=row.id,
                user_id=created_by,
                field="created",
                old_value=None,
                new_value=spec.name,
            )
        )
    await session.flush()
