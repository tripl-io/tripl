"""Responses of ``GET /projects/{slug}/plan/export`` (GH #262, F09).

Two shapes of the same branch of the plan:

* ``jsonschema`` - one JSON Schema (draft 2020-12) per non-archived event,
  keyed ``<event_type>/<identity>``: the event's fields as properties, the
  required ones listed, enums from variables' allowed values and field enum
  options, ``pattern`` / ``minimum`` / ``maximum`` from the field contracts.
* ``codegen_model`` - the plan shaped for ``tripl codegen``: event types with
  their naming rule and fields (each with the closed list of values the plan
  allows, or ``null`` when free), their events, and the variables.

Both carry the plan revision and branch they were read from, and a
``plan_hash`` over the content, so generated code can name what it was built
from and a CI drift check can compare cheaply. Neither carries a timestamp:
the same plan exports byte-identical.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

PlanExportFormat = Literal["jsonschema", "codegen_model"]


class PlanExportMeta(BaseModel):
    # The latest plan revision of the project (main), or the base revision a
    # branch was opened from. ``None`` when no revision was ever taken.
    revision: uuid.UUID | None = None
    # The branch's name ("main" for main) and id.
    branch: str
    branch_id: uuid.UUID
    # sha256 over the exported content; changes whenever the export would.
    plan_hash: str


class PlanExportJsonSchemaBundle(PlanExportMeta):
    format: Literal["jsonschema"] = "jsonschema"
    # ``<event_type>/<identity>`` -> a JSON Schema draft 2020-12 document.
    schemas: dict[str, dict[str, Any]] = Field(default_factory=dict)


class CodegenField(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    required: bool = False
    # The plan's field type: string | number | boolean | json | enum | url.
    type: str
    # Every value the plan allows for this field on this type, as the exact
    # plan strings, in plan order; ``None`` when the value is free.
    values: list[str] | None = None
    # The variable backing the field when every event of the type fills it
    # with the same ``${variable}`` token; its name.
    variable: str | None = None


class CodegenEvent(BaseModel):
    identity: str
    name: str
    description: str = ""
    status: str
    # Field name -> the stored plan value (a literal or a ``${token}`` template).
    field_values: dict[str, str] = Field(default_factory=dict)
    # ``${token}`` -> this event's override list for the variable, which
    # REPLACES the variable's global ``allowed_values`` for this event only.
    # Keyed by every token spelling of the variable; tokens sorted, values in
    # plan order. Empty when the event overrides nothing.
    overrides: dict[str, list[str]] = Field(default_factory=dict)
    deprecated: bool = False


class CodegenEventType(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    # The governing ``event_name_format`` (``{category}:{action}:{label}``), if any.
    name_rule: str | None = None
    fields: list[CodegenField] = Field(default_factory=list)
    events: list[CodegenEvent] = Field(default_factory=list)


class CodegenVariable(BaseModel):
    name: str
    allowed_values: list[str] = Field(default_factory=list)
    # Every ``${token}`` spelling that resolves to this variable (name, source
    # name, bindings), so a stored template can be mapped back to it.
    tokens: list[str] = Field(default_factory=list)


class PlanExportCodegenModel(PlanExportMeta):
    format: Literal["codegen_model"] = "codegen_model"
    event_types: list[CodegenEventType] = Field(default_factory=list)
    variables: list[CodegenVariable] = Field(default_factory=list)
