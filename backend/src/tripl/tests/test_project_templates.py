"""Built-in project templates are valid, deterministic data (F21, GH #274).

Pure: no database. Every template is checked against the model's own
``validate()`` and against the real Pydantic create schemas, so a template can
never seed a row the editor itself would refuse.
"""

import importlib
import uuid

import pytest

from tripl.schemas.event import EventCreate, EventFieldValueIn
from tripl.schemas.event_type import EventTypeCreate
from tripl.schemas.field_definition import FieldDefinitionCreate
from tripl.schemas.plan_branch import PlanBranchCreate
from tripl.schemas.project import ProjectCreate
from tripl.schemas.variable import VariableCreate
from tripl.services import project_template_service
from tripl.services.project_templates import TEMPLATES, get_template, list_templates
from tripl.services.project_templates.model import ProjectTemplate

_IDS = [t.id for t in TEMPLATES]
# Templates are generic industry content: no hosts, URLs, e-mail addresses or
# internal ticket keys that would tie them to a real organisation.
_FORBIDDEN = ("http://", "https://", "@", ".com", "jira", "tripl-")


def _all_text(template: ProjectTemplate) -> str:
    return repr(template).lower()


def test_display_order_and_ids() -> None:
    assert _IDS == ["ecommerce", "subscriptions", "mobile_games", "b2b_saas"]
    assert list(list_templates()) == list(TEMPLATES)
    assert get_template("ecommerce") is TEMPLATES[0]
    assert get_template("nope") is None


def test_ids_and_branch_names_are_unique() -> None:
    assert len(set(_IDS)) == len(_IDS)
    branch_names = [t.branch_name for t in TEMPLATES]
    assert len(set(branch_names)) == len(branch_names)


@pytest.mark.parametrize("template", TEMPLATES, ids=_IDS)
def test_template_validates(template: ProjectTemplate) -> None:
    assert template.validate() == []


@pytest.mark.parametrize("template", TEMPLATES, ids=_IDS)
def test_template_has_realistic_size(template: ProjectTemplate) -> None:
    counts = template.counts()
    assert counts.event_types >= 3
    assert counts.events >= 7
    assert counts.variables >= 3
    assert 3 <= counts.metric_suggestions <= 5
    assert 2 <= counts.alert_suggestions <= 3


@pytest.mark.parametrize("template", TEMPLATES, ids=_IDS)
def test_every_event_type_is_used(template: ProjectTemplate) -> None:
    used = {ev.event_type for ev in template.events}
    assert {et.name for et in template.event_types} <= used


@pytest.mark.parametrize("template_id", _IDS)
def test_templates_are_deterministic(template_id: str) -> None:
    # Each template module is named after its id.
    module = importlib.import_module(f"tripl.services.project_templates.{template_id}")
    reloaded = importlib.reload(module).TEMPLATE
    assert reloaded == get_template(template_id)
    assert project_template_service.render_branch_description(
        reloaded
    ) == project_template_service.render_branch_description(reloaded)
    first = project_template_service.list_template_summaries()
    second = project_template_service.list_template_summaries()
    assert first == second


@pytest.mark.parametrize("template", TEMPLATES, ids=_IDS)
def test_template_rows_pass_the_real_create_schemas(template: ProjectTemplate) -> None:
    PlanBranchCreate(name=template.branch_name)
    ProjectCreate(name="x", slug="x", template_id=template.id)
    fake_type_id = uuid.uuid4()
    for et in template.event_types:
        EventTypeCreate(
            name=et.name,
            display_name=et.display_name,
            description=et.description,
            color=et.color,
            field_definitions=[
                FieldDefinitionCreate(
                    name=fd.name,
                    display_name=fd.display_name,
                    field_type=fd.field_type,
                    is_required=fd.is_required,
                    enum_options=list(fd.enum_options) if fd.enum_options is not None else None,
                    description=fd.description,
                )
                for fd in et.fields
            ],
        )
    for var in template.variables:
        VariableCreate(
            name=var.name,
            variable_type=var.variable_type.value,
            description=var.description,
            allowed_values=list(var.allowed_values),
        )
    for ev in template.events:
        created = EventCreate(
            event_type_id=fake_type_id,
            name=ev.name,
            title=ev.title,
            description=ev.description,
            tags=list(ev.tags),
            field_values=[
                EventFieldValueIn(field_definition_id=uuid.uuid4(), value=value)
                for _name, value in ev.field_values
            ],
        )
        # Tags are stored exactly as the template spells them.
        assert created.tags == list(ev.tags)


@pytest.mark.parametrize("template", TEMPLATES, ids=_IDS)
def test_template_content_is_generic(template: ProjectTemplate) -> None:
    text = _all_text(template)
    assert text.isascii()
    for needle in _FORBIDDEN:
        assert needle not in text


@pytest.mark.parametrize("template", TEMPLATES, ids=_IDS)
def test_branch_description_lists_every_suggestion(template: ProjectTemplate) -> None:
    description = project_template_service.render_branch_description(template)
    assert template.name in description
    assert f"version {template.version}" in description
    assert "not created: need a data source or scan" in description
    assert "not created: need an alert destination" in description
    for metric in template.metric_suggestions:
        assert metric.name in description
        assert metric.display_name in description
    for alert in template.alert_suggestions:
        assert alert.name in description


def test_summaries_match_templates() -> None:
    summaries = project_template_service.list_template_summaries()
    assert [s.id for s in summaries] == _IDS
    for summary, template in zip(summaries, TEMPLATES, strict=True):
        counts = template.counts()
        assert summary.counts.event_types == counts.event_types
        assert summary.counts.fields == counts.fields
        assert summary.counts.events == counts.events
        assert summary.counts.variables == counts.variables
        assert summary.counts.metric_suggestions == len(summary.metric_suggestions)
        assert summary.counts.alert_suggestions == len(summary.alert_suggestions)
        assert summary.event_type_names == [et.name for et in template.event_types]
        assert all(a.needs == "alert_destination" for a in summary.alert_suggestions)


def test_resolve_unknown_template_is_422() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        project_template_service.resolve_template("does-not-exist")
    assert caught.value.status_code == 422
    assert caught.value.detail == "Unknown project template"


def test_validate_reports_broken_references() -> None:
    from dataclasses import replace

    from tripl.services.project_templates.model import TemplateEvent

    base = TEMPLATES[0]
    broken = replace(
        base,
        events=(
            *base.events,
            TemplateEvent("nope_type", "orphan_event", "Orphan", ""),
            TemplateEvent(
                base.event_types[0].name,
                "bad_ref",
                "Bad",
                "",
                field_values=(("platform", "${undefined_var}"),),
            ),
        ),
    )
    problems = broken.validate()
    assert any("unknown type 'nope_type'" in p for p in problems)
    assert any("unknown variable 'undefined_var'" in p for p in problems)


def test_validate_rejects_metric_suggestions_the_metric_model_cannot_back() -> None:
    from dataclasses import replace

    from tripl.services.project_templates.model import TemplateMetricSuggestion

    base = TEMPLATES[0]
    event = base.events[0].name
    broken = replace(
        base,
        metric_suggestions=(
            TemplateMetricSuggestion(
                "fact_with_event", "F", "", "fact", numerator_event=event, needs="data_source"
            ),
            TemplateMetricSuggestion("sql_from_scan", "S", "", "sql"),
            TemplateMetricSuggestion("no_composition", "N", "", "event_composition"),
            TemplateMetricSuggestion(
                "ratio_no_denominator",
                "R",
                "",
                "event_composition",
                composition="ratio",
                numerator_event=event,
            ),
        ),
    )
    problems = broken.validate()
    assert any("fact metric 'fact_with_event' references plan events" in p for p in problems)
    assert any("sql metric 'sql_from_scan' must need a data source" in p for p in problems)
    assert any("'no_composition' has no composition" in p for p in problems)
    assert any("ratio metric 'ratio_no_denominator' has no denominator" in p for p in problems)


def test_summary_metric_kinds_match_the_metric_model() -> None:
    from tripl.models.domain_enums import MetricKind

    for summary in project_template_service.list_template_summaries():
        for metric in summary.metric_suggestions:
            assert metric.kind in {k.value for k in MetricKind}
            if metric.kind == MetricKind.event_composition.value:
                assert metric.composition in {"single", "ratio"}
                assert metric.numerator_event is not None
            else:
                assert metric.composition is None
                assert metric.numerator_event is None
                assert metric.needs == "data_source"
