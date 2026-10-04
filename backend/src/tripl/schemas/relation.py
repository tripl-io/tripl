import uuid

from pydantic import BaseModel, Field, model_validator

from tripl.schemas.not_null_update import reject_explicit_nulls


class RelationCreate(BaseModel):
    source_event_type_id: uuid.UUID
    target_event_type_id: uuid.UUID
    source_field_id: uuid.UUID
    target_field_id: uuid.UUID
    # Bounded to the width of ``event_type_relations.relation_type``
    # (String(50)). The field is free-form text with no enum behind it — the UI
    # shows whatever was stored — so nothing else stopped a longer value from
    # reaching the INSERT and coming back as a generic 500.
    relation_type: str = Field("belongs_to", max_length=50)
    # No bound on the description: ``event_type_relations.description`` is Text.
    description: str = ""


# Every column behind RelationUpdate is NOT NULL, so each is refused as ``null``.
_RELATION_UPDATE_FIELDS = frozenset(
    {
        "source_event_type_id",
        "target_event_type_id",
        "source_field_id",
        "target_field_id",
        "relation_type",
        "description",
    }
)


class RelationUpdate(BaseModel):
    """Editing a relation in place (AU-13); every field optional, none nullable.

    An end is re-checked against the project branch whenever either of its ids
    changes, with the stored id standing in for the one not sent.
    """

    source_event_type_id: uuid.UUID | None = None
    target_event_type_id: uuid.UUID | None = None
    source_field_id: uuid.UUID | None = None
    target_field_id: uuid.UUID | None = None
    relation_type: str | None = Field(None, max_length=50)
    description: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: object) -> object:
        return reject_explicit_nulls(data, _RELATION_UPDATE_FIELDS)


class RelationResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    source_event_type_id: uuid.UUID
    target_event_type_id: uuid.UUID
    source_field_id: uuid.UUID
    target_field_id: uuid.UUID
    relation_type: str
    description: str

    model_config = {"from_attributes": True}
