from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from tripl.models.plan_revision import PlanRevisionKind


class PlanRevisionCreate(BaseModel):
    summary: str = Field(default="", max_length=2000)


class PlanRevisionSummary(BaseModel):
    """List-view of a revision — payload omitted to keep responses small."""

    id: uuid.UUID
    project_id: uuid.UUID
    summary: str
    created_at: datetime
    created_by: uuid.UUID | None
    # What produced it — a user snapshot, a branch's merge base, or a merge —
    # and the branch behind the last two (NULL once that branch is deleted), so
    # History need not parse ``summary`` or resolve a branch by name.
    kind: PlanRevisionKind = PlanRevisionKind.snapshot
    branch_id: uuid.UUID | None = None
    # Coarse counts so the list UI can show "1 event type, 23 events, …" at a glance.
    entity_counts: dict[str, int]


class PlanRevisionDetail(PlanRevisionSummary):
    payload: dict[str, object]


class PlanRevisionList(BaseModel):
    items: list[PlanRevisionSummary]
    total: int


DriftKind = Literal["added", "removed", "changed"]

# The plan entities a diff entry — and a revert request — can describe.
PlanEntityType = Literal[
    "event_type",
    "field_definition",
    "event",
    "variable",
    "meta_field",
    "relation",
]


class PlanValueChange(BaseModel):
    """One member of a collection-valued field that moved.

    ``key`` is the member's natural identifier — the field name behind an event
    field value, the tag itself, the event an override targets. ``before``/
    ``after`` carry that member's value with the key stripped out, so a reviewer
    reads "currency: USD → EUR" instead of two JSON dumps of the whole
    collection.
    """

    key: str
    kind: DriftKind
    before: Any = None
    after: Any = None


class PlanFieldChange(BaseModel):
    """A single field that changed between the base and branch state.

    ``before``/``after`` carry the raw JSON values (not repr strings) so the UI
    can render structured values — arrays, override maps — instead of a Python
    ``repr``. Only present on ``changed`` entries.
    """

    field: str
    before: Any = None
    after: Any = None
    # Per-member breakdown for collection-valued fields (field_values, tags,
    # bindings, event_value_overrides, …). Empty for scalars, where before/after
    # already say everything, and for cross-shape comparisons where keying the
    # members is meaningless.
    items: list[PlanValueChange] = Field(default_factory=list)


class PlanDiffEntry(BaseModel):
    entity_type: PlanEntityType
    kind: DriftKind
    name: str
    parent: str | None = None
    # The entity's id, so the UI can link the row to the thing it describes:
    # the branch-side id for added/changed entries, the base-side id for removed
    # ones (which no longer exist on the branch). Null on legacy snapshots that
    # predate id capture.
    entity_id: str | None = None
    # For changed entries: list of human-readable field-level changes,
    # e.g. ["field_type: string → number", "is_required: false → true"].
    changes: list[str] = Field(default_factory=list)
    # Structured mirror of ``changes`` for changed entries: raw before/after
    # values per field so the UI can render a proper diff table.
    field_changes: list[PlanFieldChange] = Field(default_factory=list)
    # Full entity state on each side, with DB ids / ordering / nested child
    # collections stripped. ``before`` is null for added entries, ``after`` is
    # null for removed entries; both are populated for changed entries.
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    # Things a reviewer should know that are not a change between the two
    # sides — an event on a scan-governed type with no scan identity, which
    # will never merge with its scanned twin, or a natural key
    # more than one event or relation holds, which the diff, the merge and a
    # revert all match rows by. Plain sentences, rendered beside the row.
    warnings: list[str] = Field(default_factory=list)
    # Set when the entry is the machine's doing rather than the author's — a
    # scan-minted variable nobody used being retired, or a removal main has
    # already made — with the reason in words. Such entries stay in the list
    # but are left out of ``summary``'s added/removed/changed and shown folded.
    # Null for a change a reviewer should read.
    housekeeping: str | None = None
    # Not part of the API: a private attribute, so never serialized and not in
    # the schema. ``compute_plan_diff_entries`` sets it on a removed variable
    # that some event on the new side still names with a ``${token}`` in a field
    # or meta value, and ``mark_housekeeping`` reads it. Carried on the entry
    # because the diff is where the new side is read; main's
    # events, which the base-to-branch diff never reads, reach
    # ``mark_housekeeping`` as ``main_payload``.
    _still_referenced: bool = PrivateAttr(default=False)


class PlanDiff(BaseModel):
    revision_id: uuid.UUID
    compare_to: uuid.UUID
    entries: list[PlanDiffEntry]
    summary: dict[str, int]
