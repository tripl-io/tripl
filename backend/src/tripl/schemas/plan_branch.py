from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from tripl.models.domain_enums import MergeResolutionChoice
from tripl.models.plan_branch import BranchKind, BranchStatus
from tripl.schemas.plan_revision import PlanDiffEntry, PlanEntityType

BranchTransitionAction = Literal[
    "submit",
    "request_changes",
    "approve",
    "reopen",
    "close",
]


# A new branch's name reads like a ref everywhere it appears (the switcher,
# ``?branch=`` links): a letter or digit first, then letters, digits and
# ``- _ / .``, at most 64 characters. Mirrors ``branchNameProblem`` in the
# frontend's branchMeta.ts, which only explains it earlier. The 64 is
# checked in the validator rather than as the field's ``max_length``, which
# stays the column width (see below).
BRANCH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_.-]*")
BRANCH_NAME_MAX = 64


class PlanBranchCreate(BaseModel):
    # Bounded to the width of ``plan_branches.name`` (String(255)). The validator
    # below strips the name and rejects the two unusable ones, but nothing capped
    # its length, so a 300-character branch name passed here and failed in the
    # INSERT as a Postgres StringDataRightTruncation — a generic 500 naming no
    # field, for a body this layer had already accepted. The
    # bound is checked before the strip, the same order ``DataSourceCreate.name``
    # uses, so padding cannot buy extra characters.
    name: str = Field(max_length=255)
    # No bound on the description: ``plan_branches.description`` is Text.
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Branch name is required")
        if normalized.lower() == "main":
            raise ValueError("'main' is reserved for the live plan")
        if len(normalized) > BRANCH_NAME_MAX:
            raise ValueError(f"Branch name must be at most {BRANCH_NAME_MAX} characters")
        if not BRANCH_NAME_RE.fullmatch(normalized):
            if any(ch.isspace() for ch in normalized):
                raise ValueError("Branch names cannot contain spaces")
            raise ValueError(
                "Branch name must start with a letter or number and use only "
                "letters, numbers and - _ / ."
            )
        return normalized


class PlanBranchResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    kind: BranchKind
    status: BranchStatus
    description: str
    base_revision_id: uuid.UUID | None
    created_by: uuid.UUID | None
    merged_at: datetime | None
    merged_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    # Diff counts for the branches list' ahead/behind badge. Only populated by
    # ``GET /branches?include_diff_counts=true``, which computes them for every
    # open (not merged or closed) feature branch off a single main snapshot;
    # ``None`` everywhere else, so a caller can tell "not asked for" from
    # "nothing to show". ``ahead`` counts a
    # rename the merge will apply as ONE change, as the branch's diff view does.
    ahead: int | None = None
    behind_base: bool | None = None

    model_config = {"from_attributes": True}


class PlanBranchList(BaseModel):
    items: list[PlanBranchResponse]
    total: int


class BranchReviewerResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class BranchApprovalResponse(BaseModel):
    user_id: uuid.UUID | None
    approved_at: datetime
    # Whether the branch changed since this approval was given, i.e. the row's
    # plan_hash no longer matches the branch's content. A stale
    # approval does NOT count toward the merge quota, so a client that cannot
    # see this flag necessarily renders a green quota the merge endpoint then
    # rejects with insufficient_approvals. Legacy NULL-hash rows read stale.
    stale: bool

    model_config = {"from_attributes": True}


class PlanBranchDetailResponse(PlanBranchResponse):
    reviewers: list[BranchReviewerResponse]
    approvals: list[BranchApprovalResponse]


class BranchTransitionRequest(BaseModel):
    action: BranchTransitionAction


class BranchReviewerCreate(BaseModel):
    user_id: uuid.UUID


class BranchCommentCreate(BaseModel):
    body: str
    parent_id: uuid.UUID | None = None

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Comment body is required")
        return normalized


class BranchCommentResponse(BaseModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    parent_id: uuid.UUID | None
    user_id: uuid.UUID | None
    body: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PlanDiffRename(BaseModel):
    """One removed entry and one added entry that are really a single renamed row.

    The diff keys entities by NAME, so a rename splits into a removal of the old
    name and an addition of the new one. The merge does not see it that way: it
    pairs the two by ``source_name`` and UPDATEs main's row in place, keeping the
    id and everything hanging off it. Stating the pairing here is what stops the
    UI having to re-derive it, which it cannot do correctly — the pairing also
    depends on main, and the diff the UI holds compares the base with the branch.

    ``removed_name`` and ``added_name`` are the two entries' ``name``, and
    ``entity_type`` / ``parent`` are shared by both, so the pair addresses its
    halves exactly the way ``BranchRevertRequest`` addresses a change.
    """

    entity_type: PlanEntityType
    parent: str | None = None
    removed_name: str
    added_name: str


class PlanBranchDiff(BaseModel):
    entries: list[PlanDiffEntry]
    summary: dict[str, int]
    # True when main advanced since the branch's base snapshot — the branch is
    # behind and should be rebased before merge (Phase 4).
    behind_base: bool
    # The renames hiding inside ``entries`` as a removal plus an addition. Empty
    # is the honest answer for a diff nothing pairs, and also the answer a caller
    # gets from a build that has not filled it in yet — which is why every reader
    # must treat an absent pairing as "these really are two unrelated changes"
    # rather than as an error.
    renames: list[PlanDiffRename] = Field(default_factory=list)


class BranchRevertRequest(BaseModel):
    """Undo one entry of the branch's diff.

    The entry is addressed the way the diff names it — entity type, natural name
    and parent — so the request describes a *change* rather than a row, plus
    the entry's own ``entity_id`` where a name may be shared (events and
    relations). ``field`` narrows the revert to one changed field; omitted, the
    whole entity goes back to its base state.
    """

    entity_type: PlanEntityType
    name: str
    parent: str | None = None
    field: str | None = None
    # The diff entry's own ``entity_id``. Events and relations may share a name
    # (namesakes), and then the name alone cannot say which entry is meant;
    # with the id the revert acts on exactly that row. Omitted,
    # the entry is found by name as before, and a name several entries share
    # is refused.
    entity_id: str | None = Field(default=None, max_length=64)


# --- inline 3-way merge conflict resolution ---------------------------------

ResolutionChoice = MergeResolutionChoice


class ConflictField(BaseModel):
    """A single field that diverged on both sides.

    ``base`` is the value when the branch was created (or last updated from
    main); ``ours`` is main's current value; ``theirs`` is the branch's current
    value. ``choice`` names the value to END with — ``ours`` takes main's,
    ``theirs`` keeps the branch's — for the merge and for "Update from main"
    alike; ``None`` means unresolved.

    ``field`` is ``@presence`` when one side deleted the entity and the other
    changed it: ``base``/``ours``/``theirs`` are then ``"present"`` or
    ``"absent"``. ``dependents`` counts, for an event type main deleted, the
    fields, events and relations this branch added or edited under it — what
    taking main's side removes with it.
    """

    field: str
    base: Any | None
    ours: Any | None
    theirs: Any | None
    choice: ResolutionChoice | None = None
    dependents: int = 0


class ConflictEntity(BaseModel):
    entity_type: PlanEntityType
    # The dotted name the conflict and its resolution are keyed by, e.g.
    # ``track.purchase`` for an event or ``a.f->b.g`` for a relation.
    name: str
    # The event type a field definition or event belongs to; None otherwise.
    parent: str | None = None
    # The entity's own name for display, without its parent.
    label: str = ""
    fields: list[ConflictField]


class BranchConflictsResponse(BaseModel):
    entities: list[ConflictEntity]
    unresolved_count: int
    # Main changed since the branch's base — the same test as the list's
    # ``behind_base``.
    behind: bool = False
    # How many distinct entities both sides changed (the rows above).
    overlap_count: int = 0
    # Whether ``POST /merge`` would refuse today on a conflict the inline
    # event-type resolutions cannot settle. Such a branch is brought level with
    # "Update from main", after which there is nothing left to refuse.
    merge_blocked: bool = False
    # Whether "Update from main" can run on this branch at all: False for a
    # branch whose base predates complete merge baselines. What else would stop
    # an update is the preview's ``blockers``.
    updatable: bool = True


class ResolutionCreate(BaseModel):
    entity_type: PlanEntityType
    # Bounded to the columns of ``plan_branch_merge_resolutions``.
    entity_name: str = Field(min_length=1, max_length=255)
    field_name: str = Field(min_length=1, max_length=80)
    choice: ResolutionChoice


class ResolutionResponse(BaseModel):
    id: uuid.UUID
    branch_id: uuid.UUID
    entity_type: str
    entity_name: str
    field_name: str
    choice: ResolutionChoice
    resolved_by: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- "Update from main" ------------------------------------------------


class EntityChangeCount(BaseModel):
    """How many entities of one type a side added, changed, removed or renamed."""

    entity_type: PlanEntityType
    added: int = 0
    changed: int = 0
    removed: int = 0
    renamed: int = 0


class UpdateBlocker(BaseModel):
    """Something that stops an update whatever is chosen, and what to do about it.

    ``kind``: ``incomplete_base_snapshot`` (the base predates complete merge
    baselines), ``ambiguous`` (main changed a row this branch holds more than
    once under one name, on a branch opened before origin ids) or
    ``identity_clash`` (a row of main's and one of the branch's own would share
    a name or ``source_name``; rename the branch's one).
    """

    kind: Literal["incomplete_base_snapshot", "ambiguous", "identity_clash"]
    entity_type: PlanEntityType | None = None
    name: str | None = None
    message: str


class UpdateFromMainPreview(BaseModel):
    behind: bool
    # False while ``blockers`` is non-empty: ``POST`` would refuse.
    updatable: bool = True
    blockers: list[UpdateBlocker] = Field(default_factory=list)
    base_revision_id: uuid.UUID | None
    # ``plan_snapshot_hash`` of main as this preview read it. Sent back as
    # ``expected_main_hash`` so an update never applies changes nobody saw.
    main_hash: str
    main_changes: list[EntityChangeCount]
    conflicts: BranchConflictsResponse


class UpdateFromMainRequest(BaseModel):
    # The preview's ``main_hash``. Without it only the inline ``resolutions``
    # count: a choice stored earlier was made against main as it was then, and
    # is honoured only by a caller who previewed main as it is now.
    expected_main_hash: str | None = Field(default=None, max_length=128)
    # Upserted into the branch's stored resolutions inside the update's own
    # transaction, so one call is enough; rolled back with it on a refusal.
    resolutions: list[ResolutionCreate] = Field(default_factory=list, max_length=5000)


class UpdateFromMainResult(BaseModel):
    updated: bool
    branch: PlanBranchDetailResponse
    applied: list[EntityChangeCount]
    previous_base_revision_id: uuid.UUID | None
    base_revision_id: uuid.UUID | None
