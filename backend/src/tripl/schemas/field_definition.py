import re
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from tripl.models.domain_enums import FieldDefinitionType, Sensitivity
from tripl.schemas.not_null_update import reject_explicit_nulls


class FieldDefinitionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=255)
    field_type: FieldDefinitionType
    is_required: bool = False
    enum_options: list[str] | None = None
    description: str = ""
    order: int = 0
    sensitivity: Sensitivity = Sensitivity.none
    contract_required_max_null_rate: float | None = Field(None, ge=0, le=1)
    contract_regex: str | None = Field(None, min_length=1, max_length=500)
    # `allow_inf_nan=False` makes `-Infinity`, `1e400` and `NaN` a 422 here rather
    # than a range contract no warehouse can evaluate: see the field contract
    # section of `core/adapters/base.py`, where such a bound is declared inert on
    # all four engines. This is the belt, not the fix — branch copy
    # (`plan_branch_service`), branch revert (`plan_branch_revert_service`) and
    # branch merge (`plan_branch_merge_service`, via `setattr`) all write this
    # column straight onto the ORM model without passing through this schema, and
    # rows predating the rule exist.
    contract_min_value: float | None = Field(None, allow_inf_nan=False)
    contract_max_value: float | None = Field(None, allow_inf_nan=False)
    contract_max_bad_rate: float = Field(0.0, ge=0, le=1)

    @field_validator("contract_regex")
    @classmethod
    def validate_contract_regex(cls, value: str | None) -> str | None:
        """A typo screen, deliberately NOT a portability guarantee.

        Python's ``re`` catches what an operator actually mistypes — an unclosed
        ``[``, a dangling quantifier — and that is all this promises. The pattern
        is evaluated by whichever warehouse holds the data: RE2 on ClickHouse and
        BigQuery, POSIX ARE on PostgreSQL, ``re`` only in the in-memory fallback.
        Each accepts syntax the others reject, so a pattern can save here and be
        one an engine refuses; that is contained where the engine is known, in
        the adapters, which ask the engine before compiling a pattern into a
        statement and drop just that expectation if it is refused (the field
        contract section of ``core/adapters/base.py``).

        Screening for the portable intersection instead was rejected, and not
        only because the engine is unknowable here — a FieldDefinition hangs off
        an EventType, which has no data source, and a project can scan several on
        different engines. It would also be wrong in both directions: it would
        reject the lookahead a PostgreSQL-only project is entitled to write,
        while still passing the patterns all three dialects compile and then read
        DIFFERENTLY (``\\b``, a trailing ``$``), which no syntax check can see.
        """
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"Invalid regex: {exc}") from exc
        return value

    @model_validator(mode="after")
    def validate_contract_range(self) -> FieldDefinitionCreate:
        if (
            self.contract_min_value is not None
            and self.contract_max_value is not None
            and self.contract_min_value > self.contract_max_value
        ):
            raise ValueError("contract_min_value must be <= contract_max_value")
        return self


# The update fields whose FieldDefinition column is NOT NULL, so an explicit
# ``null`` is a 422 naming the field and not a DB-level 500 out of
# ``update_field``'s generic ``setattr`` loop — see ``schemas/not_null_update``.
# Out of the set on purpose: ``enum_options`` and the three
# nullable ``contract_*`` bounds, where a null CLEARS the setting, and
# ``contract_max_bad_rate``, whose null ``update_field`` already reads as "back
# to 0.0".
_FIELD_NOT_NULL_UPDATE_FIELDS = frozenset(
    {
        "display_name",
        "field_type",
        "is_required",
        "description",
        "order",
        "sensitivity",
    }
)


class FieldDefinitionUpdate(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=255)
    field_type: FieldDefinitionType | None = None
    is_required: bool | None = None
    enum_options: list[str] | None = None
    description: str | None = None
    order: int | None = None
    sensitivity: Sensitivity | None = None
    contract_required_max_null_rate: float | None = Field(None, ge=0, le=1)
    contract_regex: str | None = Field(None, min_length=1, max_length=500)
    # Mirrors FieldDefinitionCreate above; the two classes are deliberate
    # near-duplicates with no shared base, so a rule added to one has to be added
    # to the other or a PATCH becomes the way around it.
    contract_min_value: float | None = Field(None, allow_inf_nan=False)
    contract_max_value: float | None = Field(None, allow_inf_nan=False)
    contract_max_bad_rate: float | None = Field(None, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: object) -> object:
        return reject_explicit_nulls(data, _FIELD_NOT_NULL_UPDATE_FIELDS)

    @field_validator("contract_regex")
    @classmethod
    def validate_contract_regex(cls, value: str | None) -> str | None:
        """A typo screen, not a portability guarantee: see FieldDefinitionCreate.

        The near-duplicate of the validator above, for the same reason the
        ``contract_min_value`` comment gives: without it a PATCH is the way
        around the rule.
        """
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"Invalid regex: {exc}") from exc
        return value

    @model_validator(mode="after")
    def validate_contract_range(self) -> FieldDefinitionUpdate:
        if (
            self.contract_min_value is not None
            and self.contract_max_value is not None
            and self.contract_min_value > self.contract_max_value
        ):
            raise ValueError("contract_min_value must be <= contract_max_value")
        return self


class FieldDefinitionResponse(BaseModel):
    id: uuid.UUID
    event_type_id: uuid.UUID
    name: str
    display_name: str
    field_type: FieldDefinitionType
    is_required: bool
    enum_options: list[Any] | None
    description: str
    order: int
    sensitivity: Sensitivity
    contract_required_max_null_rate: float | None
    contract_regex: str | None
    contract_min_value: float | None
    contract_max_value: float | None
    contract_max_bad_rate: float

    model_config = {"from_attributes": True}


class FieldReorder(BaseModel):
    field_ids: list[uuid.UUID]


class FieldDefinitionBulkCreate(BaseModel):
    fields: list[FieldDefinitionCreate] = Field(min_length=1)
