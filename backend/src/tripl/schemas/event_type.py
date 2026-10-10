import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from tripl.schemas.field_definition import FieldDefinitionCreate, FieldDefinitionResponse
from tripl.schemas.integers import Int32
from tripl.schemas.not_null_update import reject_explicit_nulls


class EventTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=255)
    description: str = ""
    color: str = Field(default="#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")
    order: Int32 = 0
    field_definitions: list[FieldDefinitionCreate] = Field(default_factory=list)


# Every field of EventTypeUpdate maps to a NOT NULL EventType column, and
# ``update_event_type`` ``setattr``s whatever the dump holds — so an explicit
# ``null`` is a 422 naming the field rather than a DB-level 500. See
# ``schemas/not_null_update``. ``name`` is immutable and has no
# update field at all.
_EVENT_TYPE_NOT_NULL_UPDATE_FIELDS = frozenset({"display_name", "description", "color", "order"})


class EventTypeUpdate(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    order: Int32 | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: object) -> object:
        return reject_explicit_nulls(data, _EVENT_TYPE_NOT_NULL_UPDATE_FIELDS)


class EventTypeResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    display_name: str
    description: str
    color: str
    order: int
    created_at: datetime
    updated_at: datetime
    field_definitions: list[FieldDefinitionResponse] = []
    # The scan naming rule that governs this type, already resolved on the
    # server — for a branch copy, through its main counterpart.
    # Null when no scan names the type and a free-text name is the identity.
    # Clients read this instead of re-deriving it from the scan config list,
    # which is how the form got it wrong on branches in the first place.
    event_name_format: str | None = None

    model_config = {"from_attributes": True}


class EventTypeBrief(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    color: str

    model_config = {"from_attributes": True}
