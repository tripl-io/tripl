"""Project templates as data (F21, GH #274).

A template is a frozen, deterministic description of a starter tracking plan:
event types with their fields, variables, and draft example events. Nothing
here holds a database id, a clock or a random value, so two imports produce
equal templates and the same template always seeds the same branch.

Templates also carry starter metric and alert-rule SUGGESTIONS. Those are
informational only: a metric needs a data source or a scan to count anything,
and an alert rule needs a destination the workspace configures itself, so the
seeder never creates either (see ``project_template_service``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from tripl.models.domain_enums import FieldDefinitionType
from tripl.models.variable import VariableType
from tripl.schemas.plan_branch import BRANCH_NAME_MAX, BRANCH_NAME_RE

SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
VARIABLE_REF_RE = re.compile(r"\$\{([^{}]*)\}")

# Mirrors ``MetricKind`` and the event_composition ``composition`` values of
# ``MetricDefinition`` so a suggestion maps onto POST metric-definitions.
MetricSuggestionKind = Literal["event_composition", "fact", "sql"]
MetricSuggestionComposition = Literal["single", "ratio"]
MetricSuggestionNeeds = Literal["scan", "data_source"]
AlertWatch = Literal["events", "schema_drifts", "metrics", "source_freshness"]
AlertSuggestionNeeds = Literal["alert_destination"]


@dataclass(frozen=True, slots=True)
class TemplateField:
    name: str
    display_name: str
    field_type: FieldDefinitionType
    is_required: bool = False
    enum_options: tuple[str, ...] | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class TemplateEventType:
    name: str
    display_name: str
    description: str
    color: str
    fields: tuple[TemplateField, ...]

    def field(self, name: str) -> TemplateField | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True, slots=True)
class TemplateVariable:
    name: str
    variable_type: VariableType
    description: str
    allowed_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateEvent:
    event_type: str
    name: str
    title: str
    description: str
    tags: tuple[str, ...] = ()
    # (field_name, value). A value is a literal or a ``${variable}`` reference.
    field_values: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateMetricSuggestion:
    name: str
    display_name: str
    description: str
    kind: MetricSuggestionKind
    # Only for ``event_composition``: ``single`` counts ``numerator_event``,
    # ``ratio`` divides it by ``denominator_event``. ``fact`` and ``sql``
    # suggestions read a data source and reference no plan event.
    composition: MetricSuggestionComposition | None = None
    numerator_event: str | None = None
    denominator_event: str | None = None
    needs: MetricSuggestionNeeds = "scan"


@dataclass(frozen=True, slots=True)
class TemplateAlertSuggestion:
    name: str
    description: str
    watches: tuple[AlertWatch, ...]
    needs: AlertSuggestionNeeds = "alert_destination"


@dataclass(frozen=True, slots=True)
class TemplateCounts:
    event_types: int
    fields: int
    events: int
    variables: int
    metric_suggestions: int
    alert_suggestions: int


@dataclass(frozen=True, slots=True)
class ProjectTemplate:
    id: str
    version: int
    name: str
    description: str
    branch_name: str
    event_types: tuple[TemplateEventType, ...]
    variables: tuple[TemplateVariable, ...]
    events: tuple[TemplateEvent, ...]
    metric_suggestions: tuple[TemplateMetricSuggestion, ...] = field(default=())
    alert_suggestions: tuple[TemplateAlertSuggestion, ...] = field(default=())

    def event_type(self, name: str) -> TemplateEventType | None:
        return next((et for et in self.event_types if et.name == name), None)

    def counts(self) -> TemplateCounts:
        return TemplateCounts(
            event_types=len(self.event_types),
            fields=sum(len(et.fields) for et in self.event_types),
            events=len(self.events),
            variables=len(self.variables),
            metric_suggestions=len(self.metric_suggestions),
            alert_suggestions=len(self.alert_suggestions),
        )

    def validate(self) -> list[str]:
        """Every reason this template would seed a broken plan; ``[]`` when sound."""
        problems: list[str] = []
        problems.extend(self._validate_identity())
        problems.extend(self._validate_event_types())
        problems.extend(self._validate_variables())
        problems.extend(self._validate_events())
        problems.extend(self._validate_suggestions())
        return problems

    # ── validation helpers ──────────────────────────────────────────────────

    def _validate_identity(self) -> list[str]:
        problems: list[str] = []
        if not SNAKE_CASE_RE.match(self.id):
            problems.append(f"template id {self.id!r} is not snake_case")
        if self.version < 1:
            problems.append("template version must be >= 1")
        if not self.name.strip():
            problems.append("template name is empty")
        if len(self.branch_name) > BRANCH_NAME_MAX:
            problems.append(f"branch name longer than {BRANCH_NAME_MAX} characters")
        if not BRANCH_NAME_RE.fullmatch(self.branch_name):
            problems.append(f"branch name {self.branch_name!r} is not a valid branch ref")
        if self.branch_name.lower() == "main":
            problems.append("branch name 'main' is reserved")
        return problems

    def _validate_event_types(self) -> list[str]:
        problems: list[str] = []
        problems.extend(_duplicates("event type", [et.name for et in self.event_types]))
        for et in self.event_types:
            if not SNAKE_CASE_RE.match(et.name):
                problems.append(f"event type {et.name!r} is not snake_case")
            if not COLOR_RE.match(et.color):
                problems.append(f"event type {et.name!r} color {et.color!r} is not #rrggbb")
            problems.extend(_duplicates(f"field of {et.name!r}", [f.name for f in et.fields]))
            for fd in et.fields:
                if not SNAKE_CASE_RE.match(fd.name):
                    problems.append(f"field {et.name}.{fd.name} is not snake_case")
                is_enum = fd.field_type == FieldDefinitionType.enum
                if is_enum and not fd.enum_options:
                    problems.append(f"enum field {et.name}.{fd.name} has no options")
                if not is_enum and fd.enum_options is not None:
                    problems.append(f"non-enum field {et.name}.{fd.name} has enum options")
        return problems

    def _validate_variables(self) -> list[str]:
        problems: list[str] = []
        problems.extend(_duplicates("variable", [v.name for v in self.variables]))
        for var in self.variables:
            if not SNAKE_CASE_RE.match(var.name):
                problems.append(f"variable {var.name!r} is not snake_case")
        return problems

    def _validate_events(self) -> list[str]:
        problems: list[str] = []
        variable_names = {v.name for v in self.variables}
        for et in self.event_types:
            names = [ev.name for ev in self.events if ev.event_type == et.name]
            problems.extend(_duplicates(f"event of {et.name!r}", names))
        for ev in self.events:
            if not SNAKE_CASE_RE.match(ev.name):
                problems.append(f"event {ev.name!r} is not snake_case")
            ev_type = self.event_type(ev.event_type)
            if ev_type is None:
                problems.append(f"event {ev.name!r} references unknown type {ev.event_type!r}")
                continue
            problems.extend(
                _duplicates(f"field value of {ev.name!r}", [k for k, _ in ev.field_values])
            )
            problems.extend(_duplicates(f"tag of {ev.name!r}", list(ev.tags)))
            problems.extend(
                f"event {ev.name!r} tag {tag!r} is not lower-case"
                for tag in ev.tags
                if tag != tag.strip().lower() or not tag
            )
            given = {k for k, _ in ev.field_values}
            for fd in ev_type.fields:
                if fd.is_required and fd.name not in given:
                    problems.append(f"event {ev.name!r} misses required field {fd.name!r}")
            for field_name, value in ev.field_values:
                problems.extend(_check_value(ev.name, ev_type, field_name, value, variable_names))
        return problems

    def _validate_suggestions(self) -> list[str]:
        problems: list[str] = []
        event_names = {ev.name for ev in self.events}
        problems.extend(_duplicates("metric suggestion", [m.name for m in self.metric_suggestions]))
        problems.extend(_duplicates("alert suggestion", [a.name for a in self.alert_suggestions]))
        for metric in self.metric_suggestions:
            if not SNAKE_CASE_RE.match(metric.name):
                problems.append(f"metric suggestion {metric.name!r} is not snake_case")
            problems.extend(_check_metric_shape(metric))
            referenced = [
                e for e in (metric.numerator_event, metric.denominator_event) if e is not None
            ]
            for event_name in referenced:
                if event_name not in event_names:
                    problems.append(
                        f"metric suggestion {metric.name!r} references unknown event {event_name!r}"
                    )
        for alert in self.alert_suggestions:
            if not SNAKE_CASE_RE.match(alert.name):
                problems.append(f"alert suggestion {alert.name!r} is not snake_case")
            if not alert.watches:
                problems.append(f"alert suggestion {alert.name!r} watches nothing")
        return problems


def _check_metric_shape(metric: TemplateMetricSuggestion) -> list[str]:
    name = metric.name
    if metric.kind != "event_composition":
        if metric.composition is not None:
            return [f"{metric.kind} metric {name!r} has a composition"]
        if metric.numerator_event is not None or metric.denominator_event is not None:
            return [f"{metric.kind} metric {name!r} references plan events"]
        if metric.needs != "data_source":
            return [f"{metric.kind} metric {name!r} must need a data source"]
        return []
    if metric.composition is None:
        return [f"event_composition metric {name!r} has no composition"]
    if metric.numerator_event is None:
        return [f"event_composition metric {name!r} has no numerator event"]
    if metric.composition == "ratio" and metric.denominator_event is None:
        return [f"ratio metric {name!r} has no denominator"]
    if metric.composition == "single" and metric.denominator_event is not None:
        return [f"single metric {name!r} has a denominator"]
    return []


def _duplicates(what: str, names: list[str]) -> list[str]:
    seen: set[str] = set()
    problems: list[str] = []
    for name in names:
        if name in seen:
            problems.append(f"duplicate {what} {name!r}")
        seen.add(name)
    return problems


def _check_value(
    event_name: str,
    et: TemplateEventType,
    field_name: str,
    value: str,
    variable_names: set[str],
) -> list[str]:
    fd = et.field(field_name)
    if fd is None:
        return [f"event {event_name!r} sets unknown field {et.name}.{field_name}"]
    refs = VARIABLE_REF_RE.findall(value)
    problems = [
        f"event {event_name!r} field {field_name!r} references unknown variable {ref!r}"
        for ref in refs
        if ref not in variable_names
    ]
    if refs:
        return problems
    if fd.field_type == FieldDefinitionType.enum and value not in (fd.enum_options or ()):
        problems.append(f"event {event_name!r} field {field_name!r} value {value!r} not in options")
    if fd.field_type == FieldDefinitionType.number:
        try:
            float(value)
        except ValueError:
            problems.append(f"event {event_name!r} field {field_name!r} is not a number")
    if fd.field_type == FieldDefinitionType.boolean and value not in ("true", "false"):
        problems.append(f"event {event_name!r} field {field_name!r} is not true/false")
    return problems
