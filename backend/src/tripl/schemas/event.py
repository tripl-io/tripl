import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from tripl.models.event import EventStatus
from tripl.models.variable_value import VariableValueKind
from tripl.schemas.event_type import EventTypeBrief
from tripl.schemas.lifecycle import LifecycleFindingResponse
from tripl.schemas.not_null_update import reject_explicit_nulls


class EventFieldValueIn(BaseModel):
    field_definition_id: uuid.UUID
    value: str = Field(max_length=100_000)


# The most a meta value may weigh ONCE STORED.
# ``uq_event_meta_value_event_meta_value`` (b7f4d02a91c6) took the stored value
# into a btree, and a btree entry cannot exceed 2704 bytes on the default 8 KiB
# page. Nothing bounded it, so a pasted note or blob reached Postgres and came
# back as ProgramLimitExceeded ("index row size ... exceeds btree version 4
# maximum") — a 500 on the whole event save, for a payload that stored fine while
# the key was the two uuids alone. The budget is in BYTES
# because bytes are what the index counts: 2000 characters of Cyrillic are 4000
# of them. 2000 leaves room for the tuple header and the two uuids beside it.
#
# Enforced in ``event_service._normalize_meta_values_against`` rather than here,
# and against the value that function RETURNS: a meta field with a
# ``link_template`` stores only the part the template wraps, so someone pasting
# the whole URL sends a prefix that never enters the key. Checking the pasted
# text refused saves whose stored value would have fitted with room to spare.
META_VALUE_MAX_BYTES = 2000
# A bound on the PAYLOAD, which is a different question: nothing should be able
# to hand an unbounded string to the JSON parser or to
# ``api/v1/events.event_create_audit_payload``. Deliberately loose, so it never
# fires ahead of the real rule above — the longest legitimate paste is a
# 2000-byte value wrapped in a ``link_template``, itself capped at 2000
# characters (``schemas/meta_field``).
_META_VALUE_MAX_PAYLOAD_CHARS = 8000


class EventMetaValueIn(BaseModel):
    meta_field_definition_id: uuid.UUID
    value: str = Field(max_length=_META_VALUE_MAX_PAYLOAD_CHARS)


class EventCreate(BaseModel):
    event_type_id: uuid.UUID
    name: str = Field(min_length=1, max_length=500)
    # Free-text label shown beside the identity; never part of it. Optional so
    # every existing client keeps working unchanged.
    title: str = Field("", max_length=500)
    description: str = ""
    status: EventStatus = EventStatus.draft
    sunset_at: datetime | None = None
    owner_id: uuid.UUID | None = None
    reviewed: bool = False
    metric_breakdown_columns: list[str] = []
    required_presence_threshold: float | None = Field(None, gt=0, le=1)
    tags: list[str] = []
    field_values: list[EventFieldValueIn] = []
    meta_values: list[EventMetaValueIn] = []

    @field_validator("metric_breakdown_columns")
    @classmethod
    def validate_metric_breakdown_columns(cls, value: list[str]) -> list[str]:
        return _normalize_metric_breakdown_columns(value)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str]) -> list[str]:
        return _normalize_tags(value)


# The single-event PATCH counterpart of ``_BULK_NOT_NULL_UPDATE_FIELDS`` below:
# the update fields whose Event column is NOT NULL, where ``update_event``
# assigns the dumped value straight onto the row. Four names
# only, and every omission is deliberate:
#   * ``title`` — NOT NULL, but ``update_event`` writes ``(value or "").strip()``,
#     so a null already means "clear the label" and has always worked;
#   * ``metric_breakdown_columns`` — NOT NULL, but its own validator turns a null
#     into ``[]``, which is a MEANING, not an error;
#   * ``tags`` / ``field_values`` / ``meta_values`` — the service tests these
#     ``is not None``, so a null is how a client says "leave the children alone";
#   * ``sunset_at`` / ``owner_id`` / ``superseded_by_event_id`` — nullable, and a
#     null clears them.
_NOT_NULL_UPDATE_FIELDS = frozenset({"name", "description", "status", "reviewed"})


class EventUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=500)
    title: str | None = Field(None, max_length=500)
    description: str | None = None
    status: EventStatus | None = None
    sunset_at: datetime | None = None
    # The event that replaced this one. Documentation: nothing matches, collects
    # or counts coverage through it. Not on EventCreate — a brand-new event has
    # no predecessor to name.
    superseded_by_event_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    reviewed: bool | None = None
    metric_breakdown_columns: list[str] | None = None
    # null restores the default threshold.
    required_presence_threshold: float | None = Field(None, gt=0, le=1)
    tags: list[str] | None = None
    field_values: list[EventFieldValueIn] | None = None
    meta_values: list[EventMetaValueIn] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: object) -> object:
        return reject_explicit_nulls(data, _NOT_NULL_UPDATE_FIELDS)

    @field_validator("metric_breakdown_columns")
    @classmethod
    def validate_metric_breakdown_columns(cls, value: list[str] | None) -> list[str]:
        # An explicit ``null`` means "no breakdown columns". It cannot mean
        # "leave them alone": ``update_event`` keys off PRESENCE in
        # ``model_dump(exclude_unset=True)``, so the field a client did not send
        # is already the way to leave them alone. Before this, a sent ``null``
        # was assigned straight to a NOT NULL column and re-indexed inside the
        # same transaction, where ``" ".join(None)`` in ``_event_document``
        # raised TypeError — a rolled-back 500 instead of a save.
        # Nothing can depend on the old meaning: it had none,
        # every such request failed.
        return _normalize_metric_breakdown_columns(value or [])

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _normalize_tags(value)


# ``event_tags.name`` is ``String(100)``.
_TAG_MAX_LENGTH = 100


def _normalize_tags(value: list[str]) -> list[str]:
    """The one place a tag is put in the form the tag FILTER can find.

    ``?tag=`` is an equality on ``event_tags.name``, so the stored spelling IS
    the lookup key and the documented rule ("free-form labels, lower-cased") had
    to hold on every door. It held on exactly one: the web form's own
    ``addTag``. Everything else — MCP ``create_event``, the bulk paste, a raw
    API client — stored what it was handed, so ``Checkout`` was invisible to
    ``?tag=checkout`` and sat beside it in the tag list as a second label.

    This fixes WRITES from here on; no migration rewrites the rows already
    stored. What reaches those is the other half of the same repair:
    ``event_service.list_events`` case-folds both sides of the ``?tag=``
    equality and ``event_service.list_tags`` lower-cases the facet, so a legacy
    ``Checkout`` row answers ``checkout`` and appears under it. A row keeps its
    old spelling in its own ``tags`` array until the event is next saved, which
    is cosmetic — nothing looks an event up by the spelling it carries.

    Two of the ways that went wrong were bare 500s. A repeated tag hit
    ``uq_event_tag`` at a flush no ``IntegrityError`` arm covers, and an
    over-long one overflowed ``String(100)`` in Postgres; both surfaced through
    the generic handler in ``main.py``. Deduping here means the constraint is
    never reached by a client that simply said the same thing twice. Length is
    REFUSED rather than truncated: two labels cut to the same 100 characters
    would then collide on that same key, turning a bad tag into a failed save
    of the whole event.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = item.strip().lower()
        if not tag:
            continue
        if len(tag) > _TAG_MAX_LENGTH:
            raise ValueError(f"Tag is longer than {_TAG_MAX_LENGTH} characters: '{tag[:40]}...'")
        if tag not in seen:
            normalized.append(tag)
            seen.add(tag)
    return normalized


def _normalize_metric_breakdown_columns(value: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        column = item.strip()
        if not column:
            continue
        if "." in column:
            raise ValueError("metric_breakdown_columns supports scalar columns only")
        if column not in seen:
            normalized.append(column)
            seen.add(column)
    return normalized


class EventBulkDelete(BaseModel):
    event_ids: list[uuid.UUID] = Field(min_length=1)


# ``events.status`` and ``events.reviewed`` are NOT NULL, and ``bulk_update_events``
# feeds this model's dump straight into ``update(...).values()``, so a null for
# either would reach the column. ``sunset_at`` and ``owner_id`` stay OUT of the
# set on purpose: they are nullable, and a null is the only way to clear them
# across a selection.
_BULK_NOT_NULL_UPDATE_FIELDS = frozenset({"status", "reviewed"})


class EventBulkUpdate(BaseModel):
    event_ids: list[uuid.UUID] = Field(min_length=1)
    status: EventStatus | None = None
    sunset_at: datetime | None = None
    owner_id: uuid.UUID | None = None
    reviewed: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_nulls(cls, data: object) -> object:
        return reject_explicit_nulls(data, _BULK_NOT_NULL_UPDATE_FIELDS)

    @model_validator(mode="after")
    def validate_has_update(self) -> EventBulkUpdate:
        # The set of fields the client actually SENT, not their values — the
        # shape ``MetricDefinitionBulkUpdate`` already uses. Reading the values
        # made ``{"owner_id": null}`` a 422 claiming nothing was provided, and
        # ``{"reviewed": true, "owner_id": null}`` a 204 that kept every owner,
        # so the one selection-wide unassign the API offered could not be
        # spelled. The same body on metrics unassigns.
        if not (self.model_fields_set - {"event_ids"}):
            raise ValueError(
                "At least one of status, sunset_at, owner_id or reviewed must be provided"
            )
        return self


class EventMove(BaseModel):
    direction: Literal["up", "down"]
    visible_event_ids: list[uuid.UUID] | None = None


# The events of one view, in the order they should be shown. Ids must be unique —
# a duplicate is rejected with 400 (it used to walk off the end of the slot list
# with an IndexError 500). A comment rather than a docstring: a docstring here
# would change the published schema description in ``backend/openapi.json``.
class EventReorder(BaseModel):
    event_ids: list[uuid.UUID] = Field(min_length=1)


class EventFieldVariableValueResponse(BaseModel):
    id: uuid.UUID
    variable_id: uuid.UUID
    variable_name: str
    source_column: str
    value_kind: VariableValueKind
    observed_count: int
    values: list[str] = []
    # Last WRITE, not last confirmation — see the long note on
    # ``VariableValueContextResponse.updated_at`` in schemas/variable.py.
    updated_at: datetime
    # Excluding a variable no longer deletes its contexts, so this row can now
    # outlive the scanning that produced it. The values below are then the last
    # ones seen and not a live reading, and the client has to be able to say
    # which it is holding: one rendering standing for two unrelated facts is the
    # defect tripl-xv77.4 fixed for the empty context, and a stale value shown as
    # current is the same mistake with more consequences.
    excluded_from_scans: bool = False

    model_config = {"from_attributes": True}


class EventFieldValueResponse(BaseModel):
    id: uuid.UUID
    field_definition_id: uuid.UUID
    value: str
    # A hand-typed correction is frozen: ``_upsert_field_values`` never
    # overwrites an authored value, so the scan stops maintaining this field
    # for good. That was invisible — the flag reached plan snapshots and the
    # branch diff but never the event API, so the form could not tell a value
    # the scan still refreshes from one it has permanently stopped touching.
    is_authored: bool = False
    variable_values: list[EventFieldVariableValueResponse] = []

    model_config = {"from_attributes": True}


class EventMetaValueResponse(BaseModel):
    id: uuid.UUID
    meta_field_definition_id: uuid.UUID
    value: str

    model_config = {"from_attributes": True}


class EventTagResponse(BaseModel):
    id: uuid.UUID
    name: str

    model_config = {"from_attributes": True}


class EventChangeResponse(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    user_id: uuid.UUID | None = None
    user_email: str | None = None
    # Who to show when there is no user: ``"tripl (scan)"`` on a transition the
    # metrics worker made from data (auto-live, #258). Null on a user's edit —
    # ``user_email`` names those.
    author_label: str | None = None
    field: str
    old_value: str | None = None
    new_value: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class EventResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    event_type_id: uuid.UUID
    event_type: EventTypeBrief
    name: str
    # The key a scan matches this event on, which is NOT ``name``: renaming an
    # event deliberately leaves it alone so the next scan does not recreate the
    # renamed event as a duplicate (core/analyzers/event_generator.py). It
    # decides whether an authored event merges with its scanned counterpart, and
    # until tripl-u2h9.10 it appeared in no response at all — so after creating
    # an event by hand there was no way to see which identity it had claimed, or
    # that a later rename had parted the two. NULL on an event no scan has seen
    # and no naming rule governed; the generator adopts ``name`` as the identity
    # the first time one does.
    source_name: str | None = None
    # The human label, empty when the identity is all there is.
    title: str = ""
    description: str
    required_presence_threshold: float | None = None
    order: int
    status: EventStatus
    sunset_at: datetime | None = None
    # The event that replaced this one, when it was retired in favour of
    # something. Absent from the LIST response: the row has no space for it and
    # nothing on that surface asks the question.
    superseded_by_event_id: uuid.UUID | None = None
    last_seen_at: datetime | None = None
    # The earliest metric bucket with traffic, read off the main twin for a
    # branch copy. ``created_at`` is when the ROW was authored, which the detail
    # page used to label "First seen" — for an event planned before it shipped,
    # that is a date nothing was seen on. Null until the first
    # collection finds it, and on list responses, which do not compute it.
    first_seen_at: datetime | None = None
    # A branch copy's twin on main (``_branch_counterparts.main_counterparts``),
    # so a branch page can link to the same event on main (EVT-42). Null on a
    # main row, on a branch event main has no counterpart of, and — like
    # ``first_seen_at`` — on every response but the single-event read.
    main_event_id: uuid.UUID | None = None
    # Open lifecycle findings of the daily sunset watch (#258) on this event —
    # or naming it as a silent successor — read through to the main twin for a
    # branch copy. Populated on the single-event read only.
    lifecycle_findings: list[LifecycleFindingResponse] = []
    owner_id: uuid.UUID | None = None
    reviewed: bool = False
    metric_breakdown_columns: list[str] = []
    drift_count: int = 0
    tags: list[EventTagResponse] = []
    field_values: list[EventFieldValueResponse] = []
    meta_values: list[EventMetaValueResponse] = []
    created_at: datetime
    updated_at: datetime
    # The branch this row lives on. A link into a branch event without its
    # ``?branch=`` used to dead-end on a 404; the read path now answers for the
    # row's own branch and says which one, so the client can switch.
    branch_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class EventMutationResponse(EventResponse):
    """Event returned after a create or update, with advisory template warnings."""

    warnings: list[str] = Field(default_factory=list)


class EventListItemResponse(BaseModel):
    """Slim variant of EventResponse used by the list endpoint.

    Drops the nested ``event_type`` payload — the frontend already loads
    EventTypes separately and looks them up by id, so shipping the brief here
    is pure overhead at scale (and triggers an extra selectin SQL query).
    """

    id: uuid.UUID
    project_id: uuid.UUID
    event_type_id: uuid.UUID
    name: str
    # Carried here as well as on ``EventResponse``: it is a plain column, so it
    # costs no extra query (unlike the ``event_type`` brief this variant drops),
    # and a client deciding whether an identity is free has to test the same
    # predicate the server does — ``source_name``, falling back to ``name`` only
    # where ``source_name`` is NULL. Comparing names alone silently misses a
    # scanned event that has since been renamed.
    source_name: str | None = None
    title: str = ""
    description: str
    required_presence_threshold: float | None = None
    order: int
    status: EventStatus
    sunset_at: datetime | None = None
    last_seen_at: datetime | None = None
    # The earliest metric bucket with traffic, read off the main twin for a
    # branch copy. ``created_at`` is when the ROW was authored, which the detail
    # page used to label "First seen" — for an event planned before it shipped,
    # that is a date nothing was seen on. Null until the first
    # collection finds it, and on list responses, which do not compute it.
    first_seen_at: datetime | None = None
    owner_id: uuid.UUID | None = None
    reviewed: bool = False
    metric_breakdown_columns: list[str] = []
    drift_count: int = 0
    # Unanswered threads on the event's discussion, read through to the main twin
    # for a branch copy. A snooze that has lapsed counts again. Populated by
    # list_events only; the detail response renders the thread itself and can
    # count what it already holds.
    open_question_count: int = 0
    # Alert-rule coverage: True when at least one enabled rule watches this event.
    # Populated by list_events; distinct from a live firing signal.
    monitored: bool = False
    # An open lifecycle finding hangs on this event (#258): a deprecated event
    # past its sunset still receiving data, or one whose successor is silent.
    # The catalog's warning chip. Populated by list_events.
    lifecycle_warning: bool = False
    tags: list[EventTagResponse] = []
    field_values: list[EventFieldValueResponse] = []
    meta_values: list[EventMetaValueResponse] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EventListResponse(BaseModel):
    items: list[EventListItemResponse]
    total: int


# Names one identity lookup takes. The bulk form checks up to 100 pasted names;
# the bound keeps a query string (and one SELECT's IN list) finite.
MAX_IDENTITY_LOOKUP_NAMES = 200


class EventIdentityHolder(BaseModel):
    """An event that already answers to a looked-up identity (EVT-37).

    ``identity`` is the name that was asked about; ``name`` is the holder's own
    name, which differs when a scanned event has since been renamed.
    """

    identity: str
    event_id: uuid.UUID
    name: str
    source_name: str | None


class EventIdentityHoldersResponse(BaseModel):
    items: list[EventIdentityHolder]
