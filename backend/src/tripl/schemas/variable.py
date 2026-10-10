import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from tripl.core.property_schema import check_schema_matches_type, validate_property_schema
from tripl.schemas.not_null_update import reject_explicit_nulls

# Warehouse column or dotted JSON path, e.g. "variant" or "page_data.extra.variant".
BINDING_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*$")

# The two list-row caps, declared where the contract is rather than where it is
# applied, and imported from here by ``services.variable_value_service`` so the
# number the response promises cannot drift from the number the accumulator
# enforces. They reach the wire as ``maxItems``, and a row that overflowed one
# now fails response validation instead of shipping: the value cap held only
# within a single context row and a variable with a hundred contexts walked out
# with 119 values, past a limit the description called hard.
SUMMARY_VALUE_LIMIT = 20
# Event names shipped inline on the list row. The row only renders a preview
# list (``event_count`` carries the true total), so a cap keeps a variable used
# by hundreds of events from dominating the paged payload.
SUMMARY_EVENT_LIMIT = 20


def _validate_bindings(bindings: list[str] | None) -> list[str] | None:
    if bindings is None:
        return None
    for binding in bindings:
        if not BINDING_PATTERN.match(binding):
            raise ValueError(f"Invalid binding path: {binding!r}")
    if len(set(bindings)) != len(bindings):
        raise ValueError("Duplicate binding paths")
    return bindings


# What a scan itself can write as a binding: the token grammar ``${...}``
# admits anything but ``}``, and a JSON map keyed by user-typed text mints keys
# such as ``props.$os`` or ``props.utm source``. An update resends the
# variable's stored bindings, so the schema accepts that grammar and the
# service applies ``BINDING_PATTERN`` only to bindings the update ADDS.
_STORED_BINDING_PATTERN = re.compile(r"^[^}\x00-\x1f\x7f]+$")


def _validate_update_bindings(bindings: list[str] | None) -> list[str] | None:
    if bindings is None:
        return None
    for binding in bindings:
        if not _STORED_BINDING_PATTERN.match(binding):
            raise ValueError(f"Invalid binding path: {binding!r}")
    if len(set(bindings)) != len(bindings):
        raise ValueError("Duplicate binding paths")
    return bindings


def _validate_json_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if schema is None else validate_property_schema(schema)


_JSON_SCHEMA_DESCRIPTION = (
    "JSON Schema fragment refining variable_type: type, format, items, properties,"
    " required and the numeric, string and array constraints. Must agree with"
    " variable_type (number may narrow to integer, json is an object or array)."
    " Documented values stay in allowed_values. null: the type is just variable_type."
)


class VariableType(StrEnum):
    string = "string"
    number = "number"
    boolean = "boolean"
    date = "date"
    datetime = "datetime"
    json = "json"
    string_array = "string_array"
    number_array = "number_array"


class VariableValueKind(StrEnum):
    low = "low"
    high = "high"


class VariableCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    variable_type: VariableType = VariableType.string
    description: str = ""
    allowed_values: list[str] = Field(default_factory=list, max_length=500)
    bindings: list[str] = Field(default_factory=list, max_length=100)
    json_schema: dict[str, Any] | None = Field(None, description=_JSON_SCHEMA_DESCRIPTION)

    _check_bindings = field_validator("bindings")(_validate_bindings)
    _check_json_schema = field_validator("json_schema")(_validate_json_schema)

    @model_validator(mode="after")
    def _schema_matches_type(self) -> VariableCreate:
        check_schema_matches_type(self.variable_type.value, self.json_schema)
        return self


# Every field of VariableUpdate maps to a NOT NULL Variable column, and
# ``update_variable`` ``setattr``s whatever the dump holds — so an explicit
# ``null`` is a 422 naming the field rather than a DB-level 500. See
# ``schemas/not_null_update``. ``name`` is in the set for a
# second reason: the rename branch reaches ``_STRICT_NAME_PATTERN.match(None)``,
# which is a TypeError before any column is touched.
_VARIABLE_NOT_NULL_UPDATE_FIELDS = frozenset(
    {
        "name",
        "variable_type",
        "description",
        "allowed_values",
        "bindings",
        "excluded_from_scans",
    }
)


class VariableUpdate(BaseModel):
    # No pattern here: the edit form resends the stored name, and a scan-created
    # name can be anything the scan writes (``userId``, ``params.screenName``).
    # The service enforces the strict pattern only when the name actually
    # changes.
    name: str | None = Field(None, min_length=1, max_length=100)
    variable_type: VariableType | None = None
    description: str | None = None
    allowed_values: list[str] | None = Field(None, max_length=500)
    bindings: list[str] | None = Field(None, max_length=100)
    excluded_from_scans: bool | None = None
    # Nullable on purpose, unlike the rest: an explicit null clears the schema.
    # Agreement with variable_type is checked by the service, which knows the
    # stored half of the pair.
    json_schema: dict[str, Any] | None = Field(None, description=_JSON_SCHEMA_DESCRIPTION)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: object) -> object:
        return reject_explicit_nulls(data, _VARIABLE_NOT_NULL_UPDATE_FIELDS)

    _check_bindings = field_validator("bindings")(_validate_update_bindings)
    _check_json_schema = field_validator("json_schema")(_validate_json_schema)


class VariableEventRef(BaseModel):
    """One event a variable was observed in, with the id a link needs."""

    id: uuid.UUID
    name: str


class VariableResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    source_name: str | None
    variable_type: VariableType
    description: str
    allowed_values: list[str] = []
    bindings: list[str] = []
    excluded_from_scans: bool = False
    json_schema: dict[str, Any] | None = None
    event_count: int = 0
    context_count: int = 0
    low_context_count: int = 0
    high_context_count: int = 0
    sample_values: list[str] = Field(
        default=[],
        max_length=SUMMARY_VALUE_LIMIT,
        description=(
            "Observed values unioned across every (variable, event, field) context,"
            " de-duplicated and capped at 20. Lets a list client render the row's"
            " value chips without one /values call per variable."
        ),
    )
    open_drift_count: int = 0
    listed_event_count: int = Field(
        default=0,
        description=(
            "Events whose property list carries this property (F23), on the"
            " property's branch; unlike 'event_count', which counts where scans saw it."
        ),
    )
    required_event_count: int = Field(
        default=0,
        description="Of 'listed_event_count', the events that require the property.",
    )
    value_event_count: int = Field(
        default=0,
        description=(
            "Events on the property's branch whose field or meta values name its"
            " ${token}, found with the same token scan as the 'usage' filter."
            " Counted apart from 'listed_event_count' and from 'event_count'"
            " (where scans saw it)."
        ),
    )
    event_names: list[str] = Field(
        default=[],
        max_length=SUMMARY_EVENT_LIMIT,
        description=(
            "Distinct names of the events this variable was observed in, alphabetical"
            " and capped at 20. 'event_count' carries the untruncated total; fetch"
            " /variables/{id}/values for the full per-event breakdown."
        ),
    )
    event_refs: list[VariableEventRef] = Field(
        default=[],
        max_length=SUMMARY_EVENT_LIMIT,
        description=(
            "The events this variable was observed in, as id + name so each can link"
            " to its event. Ordered by name (then id) and capped at 20, like"
            " 'event_names'; 'event_count' carries the untruncated total."
        ),
    )

    model_config = {"from_attributes": True}


class VariableListResponse(BaseModel):
    items: list[VariableResponse]
    total: int


class VariableBulkUpdate(BaseModel):
    variable_ids: list[uuid.UUID] = Field(min_length=1)
    variable_type: VariableType | None = None
    description: str | None = None
    allowed_values_add: list[str] | None = None
    allowed_values_remove: list[str] | None = None

    @model_validator(mode="after")
    def validate_has_update(self) -> VariableBulkUpdate:
        if (
            self.variable_type is None
            and self.description is None
            and self.allowed_values_add is None
            and self.allowed_values_remove is None
        ):
            raise ValueError(
                "At least one of variable_type, description, allowed_values_add or"
                " allowed_values_remove must be provided"
            )
        return self


class VariableBulkDelete(BaseModel):
    variable_ids: list[uuid.UUID] = Field(min_length=1)


class VariableEventOverrideUpsert(BaseModel):
    """Add the variable to the event's property list, or edit its entry.

    A patch: a field left out keeps what the entry holds, and a new entry
    starts with no override and not required. ``values: null`` removes the
    override and keeps the property; deleting the entry removes both.
    """

    values: list[str] | None = Field(
        None,
        max_length=500,
        description="Allowed values for this event, replacing the variable's global list."
        " null: no override, the global list applies.",
    )
    required: bool | None = Field(
        None, description="Whether every occurrence of the event must carry this property."
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_null_required(cls, data: object) -> object:
        return reject_explicit_nulls(data, frozenset({"required"}))


class VariableEventOverrideResponse(BaseModel):
    id: uuid.UUID
    variable_id: uuid.UUID
    event_id: uuid.UUID
    event_name: str
    values: list[str] | None = None
    required: bool = False

    model_config = {"from_attributes": True}


class EventPropertyResponse(BaseModel):
    """One entry of an event's property list, with the variable it names."""

    id: uuid.UUID
    variable_id: uuid.UUID
    name: str
    variable_type: VariableType
    json_schema: dict[str, Any] | None = None
    description: str = ""
    required: bool = False
    values: list[str] | None = Field(
        None, description="This event's override of the allowed values; null when there is none."
    )
    effective_values: list[str] = Field(
        default=[],
        description="The allowed values in force for this event: the override when there is"
        " one, else the variable's global list.",
    )
    presence_rate: float | None = Field(
        None,
        description="Share of this event's scanned rows that carried the property, from the"
        " last scan that measured it; null when unknown.",
    )
    suggested_required: bool | None = Field(
        None,
        description="Whether presence_rate reaches the event's required_presence_threshold"
        " (default 0.95); null when presence is unknown. A suggestion: 'required' is only"
        " ever set by a person.",
    )


class VariableValueContextResponse(BaseModel):
    id: uuid.UUID
    variable_id: uuid.UUID
    variable_name: str
    event_id: uuid.UUID
    event_name: str
    field_definition_id: uuid.UUID
    field_name: str
    field_display_name: str
    source_column: str
    value_kind: VariableValueKind
    observed_count: int
    values: list[str] = []
    # When these stored values were last WRITTEN — not when a scan last
    # confirmed them, and emphatically not when they were first seen. Two
    # mechanisms produce it and both are honest only under that reading: the
    # scan path DELETES the row and re-inserts a fresh uuid
    # (``_event_generator_variables.insert_variable_contexts``), so on that path
    # ``created_at`` is the last scan write and would be a fabrication if
    # exposed as "first observed"; the metrics-replay merge
    # (``worker.tasks.metrics.generation``) mutates in place, so
    # ``TimestampMixin.onupdate`` fires, but only when the merge changed
    # something. A scan that re-observes nothing new leaves this frozen —
    # "last seen" and "last checked" are both wrong, "last refreshed" is what
    # it supports.
    updated_at: datetime

    model_config = {"from_attributes": True}
