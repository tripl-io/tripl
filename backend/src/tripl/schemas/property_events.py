"""One property's events, and edits of its entry across many events (F23.8, #306).

The per-event entry is a ``variable_event_value_overrides`` row (F23.3): these
schemas read and write that row from the property's side, for the Properties
catalog page and its bulk edit.
"""

import uuid

from pydantic import BaseModel, Field, model_validator

from tripl.schemas.not_null_update import reject_explicit_nulls

# One bulk request's ceiling: the events list pages at 10 000 and a property on
# more events than that is edited in slices.
BULK_EVENT_LIMIT = 5000


class PropertyEventResponse(BaseModel):
    """One event whose property list carries the property, with its entry."""

    event_id: uuid.UUID
    event_name: str
    event_type_id: uuid.UUID
    status: str
    required: bool = False
    values: list[str] | None = Field(
        None, description="This event's override of the allowed values; null when there is none."
    )
    effective_values: list[str] = Field(
        default=[],
        description="The allowed values in force for this event: the override when there is"
        " one, else the property's global list.",
    )
    presence_rate: float | None = Field(
        None, description="Share of the event's rows that carried the property at the last scan."
    )
    required_presence_threshold: float | None = Field(
        None, description="The event's own threshold; null means the default (0.95)."
    )
    suggested_required: bool | None = Field(
        None,
        description="Whether presence_rate reaches the event's threshold; null when presence is"
        " unknown.",
    )


class PropertyEventsBulkUpsert(BaseModel):
    """Add the property to many events' lists, or edit its entry on each.

    The same patch as the single ``PUT .../event-overrides/{event_id}``: a field
    left out keeps what each entry holds, a new entry starts with no override
    and not required, and ``values: null`` drops the override.
    """

    event_ids: list[uuid.UUID] = Field(min_length=1, max_length=BULK_EVENT_LIMIT)
    required: bool | None = Field(
        None, description="Whether every occurrence of each event must carry the property."
    )
    values: list[str] | None = Field(
        None,
        max_length=500,
        description="Allowed values for these events, replacing the property's global list."
        " null: no override, the global list applies.",
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_null_required(cls, data: object) -> object:
        return reject_explicit_nulls(data, frozenset({"required"}))


class PropertyEventsBulkDelete(BaseModel):
    """Take the property off many events' lists (their overrides go with it)."""

    event_ids: list[uuid.UUID] = Field(min_length=1, max_length=BULK_EVENT_LIMIT)


class PropertyEventsBulkResult(BaseModel):
    created: int = Field(description="Events the property was added to.")
    updated: int = Field(description="Events whose existing entry was edited.")
    removed: int = Field(0, description="Events the property was taken off.")
