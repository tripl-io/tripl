"""Wire shape of the built-in project templates (F21, GH #274).

Suggestions are informational: the backend never creates a metric or an alert
rule from them (a metric needs a data source or scan, an alert rule needs a
destination), so they carry what to set up, never an id.
"""

from typing import Literal

from pydantic import BaseModel


class ProjectTemplateCounts(BaseModel):
    event_types: int
    fields: int
    events: int
    variables: int
    metric_suggestions: int
    alert_suggestions: int


class ProjectTemplateMetricSuggestionOut(BaseModel):
    name: str
    display_name: str
    description: str
    # The ``MetricKind`` to create; ``composition`` (and the plan event names)
    # are set only for ``event_composition``.
    kind: Literal["event_composition", "fact", "sql"]
    composition: Literal["single", "ratio"] | None = None
    numerator_event: str | None = None
    denominator_event: str | None = None
    needs: Literal["scan", "data_source"]


class ProjectTemplateAlertSuggestionOut(BaseModel):
    name: str
    description: str
    needs: Literal["alert_destination"]


class ProjectTemplateSummary(BaseModel):
    id: str
    version: int
    name: str
    description: str
    branch_name: str
    counts: ProjectTemplateCounts
    event_type_names: list[str]
    metric_suggestions: list[ProjectTemplateMetricSuggestionOut]
    alert_suggestions: list[ProjectTemplateAlertSuggestionOut]
