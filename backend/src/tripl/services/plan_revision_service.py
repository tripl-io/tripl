"""Tracking-plan snapshot + diff service.

build_plan_snapshot constructs a deterministic JSON payload of the entire
project schema. compute_plan_diff compares two payloads entry-by-entry and
returns a flat list of added / removed / changed records keyed by the
entity's natural identifier (so deleted-and-recreated rows still align).
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from fastapi import HTTPException
from sqlalchemy import ColumnElement, Integer, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, selectinload

from tripl.models.event import Event
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.event_type import EventType
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.plan_revision import PlanRevision
from tripl.models.project import Project
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import (
    VariableEventValueOverride,
    copy_override_values,
)
from tripl.schemas.plan_revision import (
    PlanDiff,
    PlanDiffEntry,
    PlanFieldChange,
    PlanRevisionCreate,
    PlanRevisionDetail,
    PlanRevisionList,
    PlanRevisionSummary,
    PlanValueChange,
)
from tripl.services._origin_pairing import pair_rows, snapshot_id, snapshot_ref
from tripl.services._plan_diff_housekeeping import note_references
from tripl.services.project_lookup import resolve_project

PLAN_REVISIONS_DEFAULT_LIMIT = 50
PLAN_SNAPSHOT_VERSION = 2


def _snapshot_fingerprint(value: str | uuid.UUID | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _sanitize_public_value(value: Any, *, key: str | None = None) -> Any:
    """Redact internal merge fingerprints from API and diff payloads."""
    if key is not None and key.endswith("_fingerprint"):
        return None if value is None else "<redacted>"
    if isinstance(value, dict):
        return {
            item_key: _sanitize_public_value(item, key=item_key) for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_public_value(item) for item in value]
    return value


def _public_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_public_value(payload)
    assert isinstance(sanitized, dict)
    return sanitized


def _approval_relevant_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The snapshot minus discussion — what an approval actually answers for.

    Photo comment threads are in the snapshot because a *revision* records the
    whole branch, but an approval answers a narrower question: "is this plan
    still the one I reviewed?". A reply typed under a spec screenshot changes
    no plan content, and it voided every approval on the branch —
    merge then refused with ``current=0`` while author and reviewer both
    correctly insisted nobody had touched the plan.

    Projected here rather than in ``build_plan_snapshot`` so revision payloads,
    their diffs and the merge base keep the comments they are supposed to
    record. Photos themselves stay in: attaching or removing a spec screenshot
    changes what a reviewer is being asked to approve, while talking about one
    does not.
    """
    payload = _without_origin_ids(payload)
    events = payload.get("events")
    if not isinstance(events, list):
        return payload
    projected: list[Any] = []
    for event in events:
        photos = event.get("photos") if isinstance(event, dict) else None
        if not isinstance(photos, list):
            projected.append(event)
            continue
        projected.append({**event, "photos": photos_without_comments(photos)})
    return {**payload, "events": projected}


def _without_origin_ids(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload with the branch copies' ``origin_id`` left out.

    ``origin_id`` is bookkeeping — which main row a copy came from — not plan
    content, and hashing it would void approvals with nothing to review: the
    migration that introduced it backfilled it onto branches already approved.
    It is deliberately not a foreign key, so main deleting
    the row a copy came from leaves the copy's ``origin_id`` in place.
    """
    projected = payload
    for key in _ORIGIN_CARRYING_SETS:
        items = projected.get(key)
        if not isinstance(items, list) or not any(
            isinstance(item, dict) and "origin_id" in item for item in items
        ):
            continue
        projected = {
            **projected,
            key: [
                {k: v for k, v in item.items() if k != "origin_id"}
                if isinstance(item, dict)
                else item
                for item in items
            ],
        }
    return projected


# The snapshot sets whose entries can carry an ``origin_id``.
_ORIGIN_CARRYING_SETS = ("events", "relations")


def photos_without_comments(photos: list[Any]) -> list[Any]:
    """A snapshot ``photos`` list with every photo's discussion removed.

    Re-sorted AFTER stripping, or the removal leaks through the ORDER.
    ``serialize_photos`` sorts by canonical JSON of the whole photo dict, and
    "comments" sorts first among a photo's keys — so with two or more photos a
    new comment can swap their positions, and dropping the field afterwards
    leaves that reordering in place. Sorting the stripped dicts makes the order
    depend only on the attachments. Approval hashing and merge conflict
    detection both compare through this, so they cannot disagree about whether
    a comment changed the plan.
    """
    stripped = [
        {key: value for key, value in photo.items() if key != "comments"}
        if isinstance(photo, dict)
        else photo
        for photo in photos
    ]
    stripped.sort(
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
    )
    return stripped


def plan_snapshot_hash(payload: dict[str, Any]) -> str:
    """Stable sha256 of the plan content a branch approval covers.

    Used to pin a branch approval to the exact content it reviewed
    (PlanBranchApproval.plan_hash): the snapshot builder is deterministic
    (name-ordered queries), so canonical JSON of equal content hashes equal.

    Hashes ``_approval_relevant_payload``, not the raw snapshot, so commenting
    on a photo cannot invalidate a review. Every caller of this function is an
    approval-freshness check; nothing else stores or compares this digest.
    """
    canonical = json.dumps(
        _approval_relevant_payload(payload), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Field-level keys we compare per entity type. Fields not listed are
# treated as metadata and ignored by the diff (so cosmetic edits like
# `order` don't churn the diff log).
_FIELD_DEFINITION_CHANGE_KEYS = (
    "field_type",
    "is_required",
    "enum_options",
    "description",
    "sensitivity",
    "contract_required_max_null_rate",
    "contract_regex",
    "contract_min_value",
    "contract_max_value",
    "contract_max_bad_rate",
)
_EVENT_TYPE_CHANGE_KEYS = ("display_name", "description", "color")
_EVENT_CHANGE_KEYS = (
    "source_name",
    "title",
    "description",
    "status",
    "sunset_at",
    "superseded_by",
    # Not ``event_type_name``: it is half of the key events are aligned by, so
    # a changed entry has the same type on both sides by construction, and
    # nothing moves an event between types. Listed, it was a change key the
    # diff could never report and the revert no longer restores.
    "owner_id",
    "reviewed",
    "metric_breakdown_columns",
    "required_presence_threshold",
    "field_values",
    "meta_values",
    "tags",
    "photos",
)
_VARIABLE_CHANGE_KEYS = (
    "variable_type",
    "source_name",
    "description",
    "allowed_values",
    "bindings",
    "excluded_from_scans",
    "json_schema",
    "event_value_overrides",
)
_META_FIELD_CHANGE_KEYS = (
    "field_type",
    "is_required",
    # Behaviour, not cosmetics: it decides whether an event may hold several
    # values. The snapshot (and so the approval hash), the conflict scan, the
    # merge and the revert all carry it; left out here, flipping it voided every
    # approval with an empty diff, merged a change no reviewer saw, and made the
    # revert answer "not in this branch's diff".
    "allow_multiple",
    "enum_options",
    "default_value",
    "link_template",
    "sensitivity",
)
_RELATION_CHANGE_KEYS = ("relation_type", "description")


def _meta_value_order(member: dict[str, Any]) -> tuple[str, str]:
    """The one order an event's ``meta_values`` are written and compared in.

    By field name, then by value. The name alone was enough while a meta field
    held one value per event; with ``allow_multiple`` a field
    holds several rows and nothing orders them — the selectin load has no ORDER
    BY, ``update_event`` deletes and re-inserts them in payload order, and a heap
    reorder moves them with no edit at all. A stable sort on the name kept that
    arrival order, so one unchanged set could serialize two ways: a diff row, a
    stale approval and a spurious merge conflict out of nothing.

    For a field with one value per event the order is exactly what it was, so a
    snapshot without a multi-value field hashes as it did before. ``str`` on both
    halves so a hand-edited payload cannot make the sort raise.
    """
    return (str(member.get("meta_field_name", "")), str(member.get("value", "")))


async def _resolve_project(session: AsyncSession, slug: str) -> Project:
    return await resolve_project(session, slug, detail=f"Project '{slug}' not found")


async def build_plan_snapshot(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Construct a deterministic JSON snapshot of the project schema.

    Scoped to a single branch. When ``branch_id`` is omitted it resolves the
    project's main branch (the live plan), so existing callers keep snapshotting
    main unchanged.
    """
    if branch_id is None:
        branch_id = await session.scalar(
            select(PlanBranch.id).where(
                PlanBranch.project_id == project_id,
                PlanBranch.kind == BranchKind.main.value,
            )
        )

    event_types_rows = (
        (
            await session.execute(
                select(EventType)
                .where(EventType.project_id == project_id, EventType.branch_id == branch_id)
                .options(selectinload(EventType.field_definitions))
                .execution_options(populate_existing=True)
                .order_by(EventType.name)
            )
        )
        .scalars()
        .all()
    )

    event_type_name_by_id: dict[uuid.UUID, str] = {et.id: et.name for et in event_types_rows}
    field_name_by_id = {
        fd.id: fd.name for event_type in event_types_rows for fd in event_type.field_definitions
    }

    event_types: list[dict[str, Any]] = []
    for et in event_types_rows:
        fds = sorted(et.field_definitions, key=lambda fd: fd.name)
        event_types.append(
            {
                "id": str(et.id),
                "name": et.name,
                "display_name": et.display_name,
                "description": et.description,
                "color": et.color,
                "order": et.order,
                "field_definitions": [
                    {
                        "id": str(fd.id),
                        "name": fd.name,
                        "display_name": fd.display_name,
                        "field_type": fd.field_type,
                        "is_required": fd.is_required,
                        "enum_options": list(fd.enum_options) if fd.enum_options else None,
                        "description": fd.description,
                        "order": fd.order,
                        "sensitivity": fd.sensitivity,
                        "contract_required_max_null_rate": fd.contract_required_max_null_rate,
                        "contract_regex": fd.contract_regex,
                        "contract_min_value": fd.contract_min_value,
                        "contract_max_value": fd.contract_max_value,
                        "contract_max_bad_rate": fd.contract_max_bad_rate or 0.0,
                    }
                    for fd in fds
                ],
            }
        )

    events_rows = (
        (
            await session.execute(
                select(Event)
                .where(Event.project_id == project_id, Event.branch_id == branch_id)
                .options(
                    selectinload(Event.field_values),
                    selectinload(Event.meta_values),
                    selectinload(Event.tags),
                )
                .execution_options(populate_existing=True)
                .order_by(Event.name)
            )
        )
        .scalars()
        .all()
    )
    # ``lazyload`` for the reason ``variable_service._check_binding_conflicts``
    # gives: ``Variable.value_contexts`` is ``lazy="selectin"`` and each context
    # then selectin-loads its FieldDefinition, so a bare select here hydrates the
    # project's entire context table — and a snapshot is built on every branch
    # DIFF, not only on a merge or a revision.
    #
    # Proven safe rather than assumed: the serializer below reads columns and
    # ``overrides_by_variable`` only, and no code in the repo dereferences
    # ``Variable.value_contexts`` at all — the readers that want contexts
    # (``attach_variable_summaries``, ``_search_documents``) issue their own
    # ``select(VariableValue)``. The one place an unloaded collection would
    # still be needed is the ORM delete cascade, and that runs inside
    # ``await session.delete(...)``, which is a coroutine precisely so the load
    # it may emit happens in the greenlet — not the plain attribute access that
    # raises ``MissingGreenlet``.
    variables_rows = (
        (
            await session.execute(
                select(Variable)
                .where(Variable.project_id == project_id, Variable.branch_id == branch_id)
                .options(lazyload(Variable.value_contexts))
                .order_by(Variable.name)
            )
        )
        .scalars()
        .all()
    )
    event_key_by_id = {
        ev.id: (event_type_name_by_id.get(ev.event_type_id, ""), ev.name) for ev in events_rows
    }
    override_rows = (
        (
            await session.execute(
                select(VariableEventValueOverride).where(
                    VariableEventValueOverride.project_id == project_id,
                    VariableEventValueOverride.branch_id == branch_id,
                )
            )
        )
        .scalars()
        .all()
    )
    overrides_by_variable: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for override in override_rows:
        event_key = event_key_by_id.get(override.event_id)
        if event_key is None:
            continue
        event_type_name, event_name = event_key
        overrides_by_variable.setdefault(override.variable_id, []).append(
            {
                "event_type_name": event_type_name,
                "event_name": event_name,
                "values": copy_override_values(override.values),
                "required": override.required,
            }
        )
    for variable_overrides in overrides_by_variable.values():
        # Values break the tie between two namesake events, so equal content
        # lists in one order however the database returned the rows — a base
        # and a branch holding the same overrides compare equal.
        variable_overrides.sort(
            key=lambda override: (
                override["event_type_name"],
                override["event_name"],
                json.dumps(override["values"], default=str),
                override["required"],
            )
        )

    variables = [
        {
            "id": str(v.id),
            "name": v.name,
            "source_name": v.source_name,
            "variable_type": v.variable_type,
            "description": v.description,
            "allowed_values": list(v.allowed_values or []),
            "bindings": list(v.bindings or []),
            "excluded_from_scans": v.excluded_from_scans,
            "json_schema": copy.deepcopy(v.json_schema),
            "event_value_overrides": overrides_by_variable.get(v.id, []),
        }
        for v in variables_rows
    ]

    meta_fields_rows = (
        (
            await session.execute(
                select(MetaFieldDefinition)
                .where(
                    MetaFieldDefinition.project_id == project_id,
                    MetaFieldDefinition.branch_id == branch_id,
                )
                .order_by(MetaFieldDefinition.name)
            )
        )
        .scalars()
        .all()
    )
    meta_fields = [
        {
            "id": str(mf.id),
            "name": mf.name,
            "display_name": mf.display_name,
            "field_type": mf.field_type,
            "is_required": mf.is_required,
            "allow_multiple": mf.allow_multiple,
            "enum_options": list(mf.enum_options) if mf.enum_options else None,
            "default_value": mf.default_value,
            "link_template": mf.link_template,
            "order": mf.order,
            "sensitivity": mf.sensitivity,
        }
        for mf in meta_fields_rows
    ]
    meta_field_name_by_id = {mf.id: mf.name for mf in meta_fields_rows}

    event_ids = [event.id for event in events_rows]
    photos_by_event: dict[uuid.UUID, list[EventPhoto]] = {}
    comments_by_photo: dict[uuid.UUID, list[EventPhotoComment]] = {}
    if event_ids:
        photo_rows = list(
            (await session.execute(select(EventPhoto).where(EventPhoto.event_id.in_(event_ids))))
            .scalars()
            .all()
        )
        for photo in photo_rows:
            photos_by_event.setdefault(photo.event_id, []).append(photo)
        if photo_rows:
            comment_rows = list(
                (
                    await session.execute(
                        select(EventPhotoComment).where(
                            EventPhotoComment.photo_id.in_([photo.id for photo in photo_rows])
                        )
                    )
                )
                .scalars()
                .all()
            )
            for comment in comment_rows:
                # The query is keyed on photo_id, so an event-anchored comment
                # cannot appear here — and must not: the event discussion is
                # deliberately outside the snapshot.
                if comment.photo_id is None:
                    continue
                comments_by_photo.setdefault(comment.photo_id, []).append(comment)

    def serialize_comments(photo_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = comments_by_photo.get(photo_id, [])
        children: dict[uuid.UUID | None, list[EventPhotoComment]] = {}
        for row in rows:
            children.setdefault(row.parent_id, []).append(row)

        def walk(parent_id: uuid.UUID | None) -> list[dict[str, Any]]:
            serialized = [
                {
                    "user_fingerprint": _snapshot_fingerprint(row.user_id),
                    "body_fingerprint": _snapshot_fingerprint(row.body),
                    "replies": walk(row.id),
                }
                for row in children.get(parent_id, [])
            ]
            return sorted(
                serialized,
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            )

        return walk(None)

    def serialize_photos(event_id: uuid.UUID) -> list[dict[str, Any]]:
        serialized = [
            {
                "uploaded_by_user_fingerprint": _snapshot_fingerprint(photo.uploaded_by_user_id),
                "original_filename": photo.original_filename,
                "content_type": photo.content_type,
                "size_bytes": photo.size_bytes,
                "kind": photo.kind,
                "external_url": photo.external_url,
                "storage_backend": photo.storage_backend,
                "storage_key_fingerprint": _snapshot_fingerprint(photo.storage_key),
                "sort_order": photo.sort_order,
                "comments": serialize_comments(photo.id),
            }
            for photo in photos_by_event.get(event_id, [])
        ]
        return sorted(
            serialized,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )

    # The successor is serialized by NATURAL KEY, never as a uuid. The merge
    # applies branch changes onto main BY (event_type_name, name) and matched
    # main rows keep their own live ids, so a branch-local uuid means nothing
    # over there. Exactly the reason `event_type_name` rides beside the raw
    # `event_type_id` below. Neither is a change key: the name is half of the
    # key the diff aligns events by, so it cannot differ within a change, and
    # the id is branch-local.
    event_by_id = {ev.id: ev for ev in events_rows}

    def _superseded_key(ev: Event) -> str | None:
        if ev.superseded_by_event_id is None:
            return None
        successor = event_by_id.get(ev.superseded_by_event_id)
        if successor is None:
            return None
        return f"{event_type_name_by_id.get(successor.event_type_id, '')}.{successor.name}"

    events = [
        {
            "id": str(ev.id),
            "event_type_id": str(ev.event_type_id),
            "event_type_name": event_type_name_by_id.get(ev.event_type_id, ""),
            "name": ev.name,
            "title": ev.title,
            "source_name": ev.source_name,
            "description": ev.description,
            "order": ev.order,
            "status": ev.status,
            "sunset_at": str(ev.sunset_at) if ev.sunset_at is not None else None,
            "superseded_by": _superseded_key(ev),
            "owner_id": str(ev.owner_id) if ev.owner_id is not None else None,
            "reviewed": ev.reviewed,
            "metric_breakdown_columns": list(ev.metric_breakdown_columns or []),
            "required_presence_threshold": ev.required_presence_threshold,
            "field_values": sorted(
                [
                    {
                        "field_name": field_name_by_id.get(value.field_definition_id, ""),
                        "value": value.value,
                        "is_authored": value.is_authored,
                    }
                    for value in ev.field_values
                ],
                key=lambda value: str(value["field_name"]),
            ),
            "meta_values": sorted(
                [
                    {
                        "meta_field_name": meta_field_name_by_id.get(
                            value.meta_field_definition_id, ""
                        ),
                        "value": value.value,
                    }
                    for value in ev.meta_values
                ],
                key=_meta_value_order,
            ),
            "tags": sorted(tag.name for tag in ev.tags),
            "photos": serialize_photos(ev.id),
            # Only where there is one, so a main snapshot, and every stored
            # base, reads exactly as it did before origin ids existed.
            **({"origin_id": str(ev.origin_id)} if ev.origin_id is not None else {}),
        }
        for ev in events_rows
    ]
    # The id breaks the tie between namesakes. The query orders by name alone,
    # so two rows sharing (type, name) came back in whatever order the database
    # chose, and the approval hash moved with no edit.
    events.sort(key=lambda event: (event["event_type_name"], event["name"], event["id"]))

    relations_rows = (
        (
            await session.execute(
                select(EventTypeRelation)
                .where(
                    EventTypeRelation.project_id == project_id,
                    EventTypeRelation.branch_id == branch_id,
                )
                .options(
                    selectinload(EventTypeRelation.source_event_type),
                    selectinload(EventTypeRelation.target_event_type),
                    selectinload(EventTypeRelation.source_field),
                    selectinload(EventTypeRelation.target_field),
                )
            )
        )
        .scalars()
        .all()
    )
    relations = [
        {
            "id": str(rel.id),
            "source_event_type_id": str(rel.source_event_type_id),
            "source_event_type_name": rel.source_event_type.name,
            "target_event_type_id": str(rel.target_event_type_id),
            "target_event_type_name": rel.target_event_type.name,
            "source_field_name": rel.source_field.name,
            "target_field_name": rel.target_field.name,
            "relation_type": rel.relation_type,
            "description": rel.description,
            **({"origin_id": str(rel.origin_id)} if rel.origin_id is not None else {}),
        }
        for rel in relations_rows
    ]
    relations.sort(
        key=lambda r: (
            r["source_event_type_name"],
            r["source_field_name"],
            r["target_event_type_name"],
            r["target_field_name"],
            r["id"],
        )
    )

    return {
        "snapshot_version": PLAN_SNAPSHOT_VERSION,
        "event_types": event_types,
        "events": events,
        "variables": variables,
        "meta_fields": meta_fields,
        "relations": relations,
    }


def _entity_counts(payload: dict[str, Any]) -> dict[str, int]:
    field_count = sum(
        len(et.get("field_definitions") or []) for et in payload.get("event_types", [])
    )
    return {
        "event_types": len(payload.get("event_types", [])),
        "fields": field_count,
        "events": len(payload.get("events", [])),
        "variables": len(payload.get("variables", [])),
        "meta_fields": len(payload.get("meta_fields", [])),
        "relations": len(payload.get("relations", [])),
    }


#: The snapshot's top-level lists, the ones ``_entity_counts`` takes ``len()``
#: of directly. Fields are not among them: they live inside each event type.
#: Fixed identifiers of ours, so formatting them into the SQL below is not
#: somewhere a request can reach.
_COUNTED_COLLECTIONS = ("event_types", "events", "variables", "meta_fields", "relations")

#: Counting a snapshot list in the database rather than in Python. A revision
#: is written at every branch creation, every merge and every manual snapshot,
#: and ``payload`` is a plain JSON column with no deferral, so a page of the
#: History tab (50 by default, 200 at most) pulled that many WHOLE plan
#: snapshots over the wire and json-decoded them on the event loop only to take
#: ``len()`` of five lists. Postgres and SQLite are the only
#: dialects this runs on; both count a JSON array in place, and only the syntax
#: differs. A list the payload lacks — a snapshot older than the key — is NULL
#: on both and counts 0, as the Python did.
_ARRAY_LENGTH_SQL = {
    "postgresql": "COALESCE(json_array_length(plan_revisions.payload -> '{key}'), 0)",
    "sqlite": "COALESCE(json_array_length(plan_revisions.payload, '$.{key}'), 0)",
}

#: Fields are the one count that is not a top-level list: they live inside each
#: event type's ``field_definitions``, so summing them needs to walk the types.
_FIELD_COUNT_SQL = {
    "postgresql": (
        "(SELECT COALESCE(SUM(json_array_length(et.value -> 'field_definitions')), 0)"
        " FROM json_array_elements(plan_revisions.payload -> 'event_types') AS et(value))"
    ),
    "sqlite": (
        "(SELECT COALESCE(SUM(json_array_length(et.value, '$.field_definitions')), 0)"
        " FROM json_each(plan_revisions.payload, '$.event_types') AS et)"
    ),
}


def _entity_count_columns(dialect: str) -> list[ColumnElement[int]]:
    """The six ``entity_counts`` as columns, so the payload can stay in the DB."""
    array_length = _ARRAY_LENGTH_SQL[dialect]
    columns: list[ColumnElement[int]] = [
        literal_column(array_length.format(key=key), Integer).label(f"count_{key}")
        for key in _COUNTED_COLLECTIONS
    ]
    columns.append(literal_column(_FIELD_COUNT_SQL[dialect], Integer).label("count_fields"))
    return columns


def _summary_from_counted_row(row: Any) -> PlanRevisionSummary:
    """One list item from a row that carries counts instead of a payload."""
    return PlanRevisionSummary(
        id=row.id,
        project_id=row.project_id,
        summary=row.summary,
        created_at=row.created_at,
        created_by=row.created_by,
        kind=row.kind,
        branch_id=row.branch_id,
        entity_counts={
            "event_types": row.count_event_types,
            "fields": row.count_fields,
            "events": row.count_events,
            "variables": row.count_variables,
            "meta_fields": row.count_meta_fields,
            "relations": row.count_relations,
        },
    )


# Collection-valued change keys, mapped to the item fields that identify one
# member. An empty tuple marks a list of scalars, where the member IS its own
# key (tags, bindings, documented values). Diffing these by member — instead of
# comparing the whole list — is what turns two JSON dumps into
# "currency: USD → EUR".
_COLLECTION_ITEM_KEYS: dict[str, tuple[str, ...]] = {
    "field_values": ("field_name",),
    "meta_values": ("meta_field_name",),
    "event_value_overrides": ("event_type_name", "event_name"),
    "photos": ("original_filename",),
    "tags": (),
    "allowed_values": (),
    "bindings": (),
    "metric_breakdown_columns": (),
    "enum_options": (),
}


def _item_key_and_value(item: Any, key_fields: tuple[str, ...]) -> tuple[str, Any] | None:
    """Split one collection member into its natural key and its value.

    Returns None when the member doesn't match the collection's declared shape
    (a container where a scalar was expected, or a dict missing a key field);
    the caller then diffs the collection as a whole rather than inventing a key.
    """
    if not key_fields:
        if isinstance(item, (dict, list)):
            return None
        return str(item), item
    if not isinstance(item, dict) or any(field not in item for field in key_fields):
        return None
    key = ".".join(str(item[field]) for field in key_fields)
    value = {k: v for k, v in item.items() if k not in key_fields}
    # A member carrying a single attribute (a meta value's ``value``, an
    # override's ``values``) reads better unwrapped than as a one-key dict.
    if len(value) == 1:
        return key, next(iter(value.values()))
    return key, value


def _collection_item_changes(field: str, old_value: Any, new_value: Any) -> list[PlanValueChange]:
    """Per-member diff of a collection-valued field, keyed by natural identity.

    Empty when the field isn't a known collection, when either side isn't a list
    (a v1 base stored overrides as a dict — cross-shape keying is meaningless),
    when a member doesn't fit the declared shape, or when two members share a
    key (photos, for one, may repeat a filename — nothing in the schema stops
    them, and keying by it would let one member mask another's removal). In each
    of those cases the field change still carries its raw before/after, so the
    reviewer sees the whole collection rather than a lie about part of it.
    """
    key_fields = _COLLECTION_ITEM_KEYS.get(field)
    if key_fields is None:
        return []
    if not isinstance(old_value, list) or not isinstance(new_value, list):
        return []

    def index(items: list[Any]) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        for item in items:
            pair = _item_key_and_value(item, key_fields)
            if pair is None:
                return None
            if pair[0] in out:
                return None
            out[pair[0]] = pair[1]
        return out

    old_by_key = index(old_value)
    new_by_key = index(new_value)
    if old_by_key is None or new_by_key is None:
        return []

    ignored = _MEMBER_ATTRS_NOT_A_CHANGE.get(field, ())

    def comparable(member: Any) -> Any:
        if not isinstance(member, dict):
            return member
        return {k: v for k, v in member.items() if k not in ignored}

    changes: list[PlanValueChange] = []
    for key in sorted(set(old_by_key) | set(new_by_key)):
        before = old_by_key.get(key)
        after = new_by_key.get(key)
        if key not in new_by_key:
            changes.append(PlanValueChange(key=key, kind="removed", before=before))
        elif key not in old_by_key:
            changes.append(PlanValueChange(key=key, kind="added", after=after))
        elif comparable(before) != comparable(after):
            changes.append(PlanValueChange(key=key, kind="changed", before=before, after=after))
    return changes


def _format_change(change: PlanFieldChange) -> str:
    """One-line summary of a field change for the collapsed diff row.

    Collections are summarised by member counts — dumping both lists into the
    row is exactly the noise the per-member breakdown exists to remove.
    """
    if change.items:
        counts = {"added": 0, "changed": 0, "removed": 0}
        for item in change.items:
            counts[item.kind] += 1
        parts = [f"{n} {kind}" for kind, n in counts.items() if n]
        return f"{change.field}: {', '.join(parts)}"
    return f"{change.field}: {change.before!r} → {change.after!r}"


# Snapshot keys added to v2 WITHOUT a version bump, with the value an older v2
# payload is read as carrying. A bump would make every open branch unmergeable
# ("recreate it from current main", plan_branch_merge_service) for the sake of
# one optional text column, so the older shape is upgraded on read instead.
_V2_EVENT_DEFAULTS: dict[str, Any] = {
    "title": "",
    "superseded_by": None,
    # F23 (#306): an older snapshot predates the per-event threshold.
    "required_presence_threshold": None,
}
# Same argument for the meta field's ``allow_multiple``: an
# older payload predates the key, and ``_field_changes_between`` refuses to
# treat one absent from a current-version payload as skew, so
# without this every pre-existing snapshot would diff every meta field as
# changed. That danger is real only because the key IS diffed — which it was
# not until it was added to
# ``_META_FIELD_CHANGE_KEYS``.
_V2_META_FIELD_DEFAULTS: dict[str, Any] = {"allow_multiple": False}
# F23 (#306) added ``json_schema`` to a variable and ``required`` to each of its
# per-event entries. Without them every stored base would read each variable
# with an override as changed, and the conflict scan would call an untouched
# variable a conflict as soon as main edited its overrides.
_V2_VARIABLE_DEFAULTS: dict[str, Any] = {"json_schema": None}
_V2_OVERRIDE_DEFAULTS: dict[str, Any] = {"required": False}


def _with_override_defaults(variable: Any) -> Any:
    overrides = variable.get("event_value_overrides") if isinstance(variable, dict) else None
    if not isinstance(overrides, list) or all(
        isinstance(o, dict) and _V2_OVERRIDE_DEFAULTS.keys() <= o.keys() for o in overrides
    ):
        return variable
    return {
        **variable,
        "event_value_overrides": [
            {**_V2_OVERRIDE_DEFAULTS, **o} if isinstance(o, dict) else o for o in overrides
        ],
    }


# Member attributes the diff does not read as a change on their own. A field
# value's ``is_authored`` flips when a person re-saves a scan-observed value
# unchanged (every save used to do that); the reviewer sees the
# same text on both sides and a row claiming it changed. The flag still rides
# along in ``before``/``after`` — it is only not a difference by itself.
_MEMBER_ATTRS_NOT_A_CHANGE: dict[str, tuple[str, ...]] = {"field_values": ("is_authored",)}


def _with_ordered_meta_values(event: Any) -> Any:
    """``event`` with its meta values in ``_meta_value_order`` — itself if already so.

    A revision stores the snapshot exactly as it was serialized, and one written
    before the ordering fix holds a multi-value field's rows in whatever order the
    database returned them. Read beside a fresh snapshot of the same content,
    that order alone would be a change to the diff and a divergence to the merge.
    """
    if not isinstance(event, dict):
        return event
    values = event.get("meta_values")
    if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
        return event
    ordered = sorted(values, key=_meta_value_order)
    if ordered == values:
        return event
    return {**event, "meta_values": ordered}


def with_snapshot_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload with the keys later v2 serializers added, and meta values in order.

    Fills in the keys later v2 serializers added, and puts each event's meta
    values in the order ``build_plan_snapshot`` now emits them — an ordering
    fixed without a version bump for the reason the defaults were,
    so a stored base's meta values compare equal to a fresh
    snapshot's of the same content.

    Returns a new dict when something was missing or out of order and the same
    object when nothing was, so callers holding a base payload can normalize it
    once and pass it everywhere — the diff, the conflict scan and the merge all
    read the same shape. The input is never modified.
    """
    filled = payload
    for key, defaults in (
        ("events", _V2_EVENT_DEFAULTS),
        ("meta_fields", _V2_META_FIELD_DEFAULTS),
        ("variables", _V2_VARIABLE_DEFAULTS),
    ):
        items = filled.get(key)
        if not isinstance(items, list):
            continue
        if all(isinstance(item, dict) and defaults.keys() <= item.keys() for item in items):
            continue
        filled = {
            **filled,
            key: [{**defaults, **item} if isinstance(item, dict) else item for item in items],
        }
    variables = filled.get("variables")
    if isinstance(variables, list):
        upgraded = [_with_override_defaults(variable) for variable in variables]
        if any(new is not old for new, old in zip(upgraded, variables, strict=True)):
            filled = {**filled, "variables": upgraded}
    events = filled.get("events")
    if isinstance(events, list):
        ordered_events = [_with_ordered_meta_values(event) for event in events]
        if any(new is not old for new, old in zip(ordered_events, events, strict=True)):
            filled = {**filled, "events": ordered_events}
    return filled


def _comparable(field: str, value: Any) -> Any:
    """``value`` with the attributes that are not a change on their own removed."""
    ignored = _MEMBER_ATTRS_NOT_A_CHANGE.get(field)
    if ignored is None or not isinstance(value, list):
        return value
    return [
        {k: v for k, v in member.items() if k not in ignored}
        if isinstance(member, dict)
        else member
        for member in value
    ]


def _snapshot_values_equal(old_value: Any, new_value: Any) -> bool:
    """Value equality tolerant of snapshot-version shape drift.

    v1 payloads stored a variable's ``event_value_overrides`` as a dict where
    v2 stores a list — when both sides are empty containers they describe the
    same (absent) content, so treat them as equal. Non-empty containers of
    differing shapes still compare unequal (cross-shape comparison is
    meaningless; reporting "changed" is the safe outcome).
    """
    if (
        isinstance(old_value, (dict, list))
        and isinstance(new_value, (dict, list))
        and not old_value
        and not new_value
    ):
        return True
    return bool(old_value == new_value)


def _field_changes_between(
    old: dict[str, Any],
    new: dict[str, Any],
    keys: Iterable[str],
    *,
    old_is_current_version: bool,
) -> list[PlanFieldChange]:
    # Snapshot-version skew is tolerated ONLY when ``old`` predates the current
    # PLAN_SNAPSHOT_VERSION. A pre-bump (v1) payload legitimately lacks keys the
    # v2 serializer added; comparing those absent keys would flag every
    # pre-existing entity as "changed" (None vs the v2 default, e.g. None vs [] /
    # None vs False), so we skip keys missing from an older ``old``.
    #
    # When ``old`` IS the current version, the invariant is that
    # ``build_plan_snapshot`` emits every change key. A key missing from a
    # current-version ``old`` is therefore NOT version skew — it is a genuine
    # divergence (e.g. a future conditionally-omitting serializer path). We must
    # NOT silently drop it: treat the absent key as a real change so the diff
    # surfaces instead of being lost.
    changed_keys = [
        key
        for key in keys
        if (old_is_current_version or key in old)
        and not _snapshot_values_equal(
            _comparable(key, old.get(key)), _comparable(key, new.get(key))
        )
    ]
    field_changes: list[PlanFieldChange] = []
    for key in changed_keys:
        before = _sanitize_public_value(old.get(key))
        after = _sanitize_public_value(new.get(key))
        field_changes.append(
            PlanFieldChange(
                field=key,
                before=before,
                after=after,
                items=_collection_item_changes(key, before, after),
            )
        )
    return field_changes


def _public_state(item: dict[str, Any]) -> dict[str, Any]:
    """The user-facing state of a plan entity for the diff detail view.

    Strips DB ids (``id`` and ``*_id`` foreign keys), the cosmetic ``order``,
    internal join keys (``_event_type_name``), and the nested
    ``field_definitions`` list — the latter surface as their own diff entries,
    so embedding them here would be redundant and noisy.
    """
    return {
        key: _sanitize_public_value(value, key=key)
        for key, value in item.items()
        if key not in ("id", "order", "field_definitions")
        and not key.startswith("_")
        and not key.endswith("_id")
    }


def _entity_id(item: dict[str, Any]) -> str | None:
    """The entity's id, kept out of ``_public_state`` but needed to link to it."""
    value = item.get("id")
    return None if value is None else str(value)


# Entity types whose natural key nothing makes unique: an event's type and name,
# and the two fields a relation links. Event types, fields, variables and meta
# fields carry a unique constraint on theirs.
_SHARED_KEY_TYPES = ("event", "relation")


def _shared_key_warning(entity_type: str, name: str, parent: str | None) -> str:
    """The notice on an entry whose natural key more than one row holds.

    Said only where the ids leave the rows unpaired: ``_placed_entries`` pairs
    every branch copy with the base row it came from (``origin_id``) and every
    main row with itself, and what reaches ``_diff_by_key`` is the rows under a
    key several of them share with no id to tell them apart — in practice a
    branch opened before origin ids, whose namesakes the migration could not
    link. There one row per key is matched, and the merge and
    a revert match the same way, so a change to one of them can show on, or
    land on, the other.

    It deliberately claims NOTHING about the merge refusing. A refusal was
    written in batch 7 and then removed: the branch is how an analyst CLEANS
    UP a pair of namesakes — delete both copies, author one row in their place —
    and refusing that merge takes away the only door out of the state the message
    complains about (test_event_comment_main_twin holds exactly that
    workflow).
    """
    if entity_type == "event":
        return (
            f"More than one event is named '{name}' in '{parent}'. This diff, the "
            "merge and a revert all match rows by name, so a change to one of them "
            "can show on, or land on, the other. Rename one of them before changing "
            "either."
        )
    return (
        f"More than one relation links the same two fields ({name}). This diff and a "
        "revert match relations by those fields, so a change to one of them can show "
        "on, or land on, the other. Remove one of them before changing either."
    )


# Said on the stand-in entry for a shared key whose two sides do not hold the
# same rows. The entry carries no field changes of its own because there is no
# honest way to name them: one row per key is matched, and the row the change
# was made to may not be the row that was matched.
_UNATTRIBUTABLE_CHANGE_WARNING = (
    "The two sides do not hold the same rows under this key. Rows are matched one per "
    "key, so this diff cannot say which of them was added, removed or edited — read "
    "both sides in full."
)


def _diff_set(
    *,
    entity_type: str,
    old_items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    key_of: Callable[[dict[str, Any]], object],
    name_of: Callable[[dict[str, Any]], str],
    parent_of: Callable[[dict[str, Any]], str] | None = None,
    change_keys: Iterable[str],
    old_is_current_version: bool,
    collision_items: Sequence[dict[str, Any]] = (),
    pair_by_origin: bool = False,
    origins_complete: bool = False,
) -> list[PlanDiffEntry]:
    placed: list[PlanDiffEntry] = []
    if pair_by_origin:
        # Rows the ids place are entered here, one entry per row, and only the
        # keys the ids leave ambiguous go on to the one-row-per-key matching
        # below, warnings and all.
        placed, old_items, new_items = _placed_entries(
            entity_type=entity_type,
            old_items=old_items,
            new_items=new_items,
            key_of=key_of,
            name_of=name_of,
            parent_of=parent_of,
            change_keys=change_keys,
            old_is_current_version=old_is_current_version,
            origins_complete=origins_complete,
        )
    return [
        *placed,
        *_diff_by_key(
            entity_type=entity_type,
            old_items=old_items,
            new_items=new_items,
            key_of=key_of,
            name_of=name_of,
            parent_of=parent_of,
            change_keys=change_keys,
            old_is_current_version=old_is_current_version,
            collision_items=collision_items,
        ),
    ]


def _placed_entries(
    *,
    entity_type: str,
    old_items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    key_of: Callable[[dict[str, Any]], object],
    name_of: Callable[[dict[str, Any]], str],
    parent_of: Callable[[dict[str, Any]], str] | None,
    change_keys: Iterable[str],
    old_is_current_version: bool,
    origins_complete: bool,
) -> tuple[list[PlanDiffEntry], list[dict[str, Any]], list[dict[str, Any]]]:
    """Entries for the rows ``pair_rows`` places, and the rows it leaves over.

    Placed by origin id — a branch copy names the base row it came from, a
    main row is its own — or as the only unplaced row under its key on both
    sides. A row renamed onto another key is its old key removed and its new
    key added, as the diff has always shown a rename. The rows returned are
    the ones under a key the ids leave ambiguous (a branch opened before origin
    ids with namesakes the migration could not tell apart), for the
    one-row-per-key matching to handle as it always has.
    """
    change_keys = tuple(change_keys)
    pairing = pair_rows(
        old_items,
        new_items,
        key_of_old=key_of,
        key_of_new=key_of,
        id_of_old=snapshot_id,
        ref_of_new=snapshot_ref,
        unplaced_are_new=origins_complete,
    )
    new_position = {id(item): index for index, item in enumerate(new_items)}
    old_position = {id(item): index for index, item in enumerate(old_items)}
    added = sorted(
        [*pairing.added, *(new for _, new in pairing.renamed)],
        key=lambda item: new_position[id(item)],
    )
    removed = sorted(
        [*pairing.removed, *(old for old, _ in pairing.renamed)],
        key=lambda item: old_position[id(item)],
    )
    entries = [
        PlanDiffEntry(
            entity_type=entity_type,
            kind="added",
            name=name_of(item),
            parent=parent_of(item) if parent_of else None,
            entity_id=_entity_id(item),
            after=_public_state(item),
        )
        for item in added
    ]
    entries.extend(
        PlanDiffEntry(
            entity_type=entity_type,
            kind="removed",
            name=name_of(item),
            parent=parent_of(item) if parent_of else None,
            entity_id=_entity_id(item),
            before=_public_state(item),
        )
        for item in removed
    )
    for old_item, new_item in sorted(pairing.pairs, key=lambda pair: new_position[id(pair[1])]):
        field_changes = _field_changes_between(
            old_item, new_item, change_keys, old_is_current_version=old_is_current_version
        )
        if not field_changes:
            continue
        entries.append(
            PlanDiffEntry(
                entity_type=entity_type,
                kind="changed",
                name=name_of(new_item),
                parent=parent_of(new_item) if parent_of else None,
                entity_id=_entity_id(new_item),
                changes=[_format_change(fc) for fc in field_changes],
                field_changes=field_changes,
                before=_public_state(old_item),
                after=_public_state(new_item),
            )
        )
    left_old = [item for olds, _ in pairing.ambiguous.values() for item in olds]
    left_new = [item for _, news in pairing.ambiguous.values() for item in news]
    return entries, left_old, left_new


def _diff_by_key(
    *,
    entity_type: str,
    old_items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    key_of: Callable[[dict[str, Any]], object],
    name_of: Callable[[dict[str, Any]], str],
    parent_of: Callable[[dict[str, Any]], str] | None,
    change_keys: Iterable[str],
    old_is_current_version: bool,
    collision_items: Sequence[dict[str, Any]],
) -> list[PlanDiffEntry]:
    change_keys = tuple(change_keys)
    old_by_key = {key_of(item): item for item in old_items}
    new_by_key = {key_of(item): item for item in new_items}
    entries: list[PlanDiffEntry] = []

    # ``collision_items`` is a THIRD side read for nothing but this count: main
    # as it stands now, which a base-to-branch diff never looks at. The merge
    # pairs main's rows with the base by their own ids, and the branch's by
    # origin id, so a key main holds twice is safe wherever the ids place the
    # branch's rows. The rows that reach this function are the ones they do not
    # (a branch opened before origin ids, left incomplete by the migration):
    # there the merge falls back to one row per key and can write the branch's
    # change onto either of main's namesakes. Read off the two sides alone, that
    # diff called the key safe.
    shared_keys: set[object] = set()
    if entity_type in _SHARED_KEY_TYPES:
        for items in (old_items, new_items, collision_items):
            held = Counter(key_of(item) for item in items)
            shared_keys.update(key for key, count in held.items() if count > 1)

    def warnings_for(key: object, item: dict[str, Any]) -> list[str]:
        if key not in shared_keys:
            return []
        parent = parent_of(item) if parent_of else None
        return [_shared_key_warning(entity_type, name_of(item), parent)]

    def sides_hold_the_same_rows(key: object) -> bool:
        """Whether old and new hold the same rows under ``key``, order aside.

        Read only for a shared key, and only to decide whether the collapse hid
        something: the rows are paired off by "no change between them" — the
        same comparison the changed branch makes — and any row left over on
        either side is a difference the one-per-key matching cannot show.
        """
        old_rows = [item for item in old_items if key_of(item) == key]
        new_rows = [item for item in new_items if key_of(item) == key]
        if len(old_rows) != len(new_rows):
            return False
        unpaired = list(new_rows)
        for old_row in old_rows:
            for index, candidate in enumerate(unpaired):
                if not _field_changes_between(
                    old_row,
                    candidate,
                    change_keys,
                    old_is_current_version=old_is_current_version,
                ):
                    del unpaired[index]
                    break
            else:
                return False
        return True

    # Keys an entry was raised for, so the stand-in pass below adds one only
    # where the collapse left the reviewer with nothing at all.
    entered: set[object] = set()

    for key, item in new_by_key.items():
        if key not in old_by_key:
            entered.add(key)
            entries.append(
                PlanDiffEntry(
                    entity_type=entity_type,
                    kind="added",
                    name=name_of(item),
                    parent=parent_of(item) if parent_of else None,
                    entity_id=_entity_id(item),
                    after=_public_state(item),
                    warnings=warnings_for(key, item),
                )
            )
    for key, item in old_by_key.items():
        if key not in new_by_key:
            entered.add(key)
            entries.append(
                PlanDiffEntry(
                    entity_type=entity_type,
                    kind="removed",
                    name=name_of(item),
                    parent=parent_of(item) if parent_of else None,
                    # The entity is gone from the new side, so the only id that
                    # resolves is the old one.
                    entity_id=_entity_id(item),
                    before=_public_state(item),
                    warnings=warnings_for(key, item),
                )
            )
    for key, new_item in new_by_key.items():
        old_item = old_by_key.get(key)
        if old_item is None:
            continue
        # Under a shared key the two representatives are not a pair: each side
        # kept whichever row it listed last, and ``build_plan_snapshot`` orders
        # events by name alone, so two snapshots of the SAME namesakes can list
        # them in either order. Comparing the representatives then reads one
        # namesake as an edit of the other, and a project that has namesakes is
        # ahead of, and behind, everything for ever. Say nothing when the sides
        # hold the same rows.
        if key in shared_keys and sides_hold_the_same_rows(key):
            continue
        field_changes = _field_changes_between(
            old_item,
            new_item,
            change_keys,
            old_is_current_version=old_is_current_version,
        )
        if field_changes:
            entered.add(key)
            entries.append(
                PlanDiffEntry(
                    entity_type=entity_type,
                    kind="changed",
                    name=name_of(new_item),
                    parent=parent_of(new_item) if parent_of else None,
                    entity_id=_entity_id(new_item),
                    changes=[_format_change(fc) for fc in field_changes],
                    field_changes=field_changes,
                    before=_public_state(old_item),
                    after=_public_state(new_item),
                    warnings=warnings_for(key, new_item),
                )
            )

    # A shared key can otherwise produce NO entry at all. Both sides collapse to
    # one representative, and when a deleted namesake is not the one that sorted
    # last, the survivor lands in the slot the pair used to share: the two
    # representatives then compare equal, the deletion is invisible and so is
    # the warning, which rides on an entry. Which namesake is
    # the representative is not even stable — ``build_plan_snapshot`` orders
    # events by name alone, so ties keep whatever order the database returned.
    # Stand an entry in, but only where the sides genuinely differ under the
    # key: a project that merely has namesakes must not read as ahead of, or
    # behind, anything on every diff it is in. A key one side alone holds is
    # already entered by the added/removed pass, and a key only
    # ``collision_items`` holds twice has no row here to hang a notice on.
    for key, new_item in new_by_key.items():
        old_item = old_by_key.get(key)
        if old_item is None or key in entered or key not in shared_keys:
            continue
        if sides_hold_the_same_rows(key):
            continue
        entries.append(
            PlanDiffEntry(
                entity_type=entity_type,
                kind="changed",
                name=name_of(new_item),
                parent=parent_of(new_item) if parent_of else None,
                entity_id=_entity_id(new_item),
                before=_public_state(old_item),
                after=_public_state(new_item),
                warnings=[
                    *warnings_for(key, new_item),
                    _UNATTRIBUTABLE_CHANGE_WARNING,
                ],
            )
        )
    return entries


def compute_plan_diff_entries(
    old_payload: dict[str, Any],
    new_payload: dict[str, Any],
    *,
    key_collisions_from: dict[str, Any] | None = None,
    origins_complete: bool = False,
) -> list[PlanDiffEntry]:
    """The changes between two plan snapshots, one entry per entity.

    ``key_collisions_from`` is a third snapshot read for one purpose: keys that
    more than one of ITS rows holds join the ones the two diffed sides hold
    twice, so the entry carries the shared-key warning. A branch diff passes
    main as it stands now, which neither the frozen base nor the branch shows
    and which the merge will nonetheless have to match rows against.

    ``origins_complete`` says the new side records the origin of every row it
    holds for an old one — true of main against any snapshot of main, and of
    a branch whose ``origin_ids_complete`` is set. Rows under a name several
    of them share are then entered one by one rather than matched one per name
    with a warning.
    """
    old_payload = with_snapshot_defaults(old_payload)
    new_payload = with_snapshot_defaults(new_payload)
    collisions = with_snapshot_defaults(key_collisions_from) if key_collisions_from else {}
    entries: list[PlanDiffEntry] = []

    # Only the OLD payload's version governs skip-absent-key tolerance: a v1
    # (pre-bump) base legitimately lacks keys the v2 serializer added, but a
    # current-version base is expected to carry every change key, so a missing
    # key there is a genuine diff — not skew — and must not be dropped.
    # Absent/unknown snapshot_version is treated as older (tolerant).
    old_is_current_version = old_payload.get("snapshot_version") == PLAN_SNAPSHOT_VERSION

    entries.extend(
        _diff_set(
            entity_type="event_type",
            old_items=old_payload.get("event_types", []),
            new_items=new_payload.get("event_types", []),
            key_of=lambda item: item["name"],
            name_of=lambda item: item["name"],
            change_keys=_EVENT_TYPE_CHANGE_KEYS,
            old_is_current_version=old_is_current_version,
        )
    )

    # Field definitions: key on (event_type_name, field_name) so a field
    # moved between types is recorded as a removal + an addition.
    old_fields: list[dict[str, Any]] = []
    for et in old_payload.get("event_types", []):
        for fd in et.get("field_definitions", []):
            old_fields.append({**fd, "_event_type_name": et["name"]})
    new_fields: list[dict[str, Any]] = []
    for et in new_payload.get("event_types", []):
        for fd in et.get("field_definitions", []):
            new_fields.append({**fd, "_event_type_name": et["name"]})
    entries.extend(
        _diff_set(
            entity_type="field_definition",
            old_items=old_fields,
            new_items=new_fields,
            key_of=lambda item: (item["_event_type_name"], item["name"]),
            name_of=lambda item: item["name"],
            parent_of=lambda item: item["_event_type_name"],
            change_keys=_FIELD_DEFINITION_CHANGE_KEYS,
            old_is_current_version=old_is_current_version,
        )
    )

    entries.extend(
        _diff_set(
            entity_type="event",
            old_items=old_payload.get("events", []),
            new_items=new_payload.get("events", []),
            key_of=lambda item: (item["event_type_name"], item["name"]),
            name_of=lambda item: item["name"],
            parent_of=lambda item: item["event_type_name"],
            change_keys=_EVENT_CHANGE_KEYS,
            old_is_current_version=old_is_current_version,
            collision_items=collisions.get("events", []),
            pair_by_origin=True,
            origins_complete=origins_complete,
        )
    )

    variable_entries = _diff_set(
        entity_type="variable",
        old_items=old_payload.get("variables", []),
        new_items=new_payload.get("variables", []),
        key_of=lambda item: item["name"],
        name_of=lambda item: item["name"],
        change_keys=_VARIABLE_CHANGE_KEYS,
        old_is_current_version=old_is_current_version,
    )
    # Read here, where the new side is at hand: whether an event there still
    # names a variable that side no longer has decides whether its removal can
    # be housekeeping at all.
    note_references(variable_entries, new_payload)
    entries.extend(variable_entries)

    entries.extend(
        _diff_set(
            entity_type="meta_field",
            old_items=old_payload.get("meta_fields", []),
            new_items=new_payload.get("meta_fields", []),
            key_of=lambda item: item["name"],
            name_of=lambda item: item["name"],
            change_keys=_META_FIELD_CHANGE_KEYS,
            old_is_current_version=old_is_current_version,
        )
    )

    entries.extend(
        _diff_set(
            entity_type="relation",
            old_items=old_payload.get("relations", []),
            new_items=new_payload.get("relations", []),
            key_of=lambda item: (
                item["source_event_type_name"],
                item["source_field_name"],
                item["target_event_type_name"],
                item["target_field_name"],
            ),
            name_of=lambda item: (
                f"{item['source_event_type_name']}.{item['source_field_name']}"
                f" → {item['target_event_type_name']}.{item['target_field_name']}"
            ),
            change_keys=_RELATION_CHANGE_KEYS,
            old_is_current_version=old_is_current_version,
            collision_items=collisions.get("relations", []),
            pair_by_origin=True,
            origins_complete=origins_complete,
        )
    )

    return entries


def _summary_counts(entries: list[PlanDiffEntry]) -> dict[str, int]:
    out = {"added": 0, "removed": 0, "changed": 0}
    for entry in entries:
        out[entry.kind] += 1
    return out


async def create_revision(
    session: AsyncSession,
    slug: str,
    data: PlanRevisionCreate,
    *,
    user_id: uuid.UUID | None = None,
) -> PlanRevisionDetail:
    project = await _resolve_project(session, slug)
    payload = await build_plan_snapshot(session, project.id)
    revision = PlanRevision(
        project_id=project.id,
        created_by=user_id,
        summary=data.summary,
        payload=payload,
    )
    session.add(revision)
    await session.commit()
    await session.refresh(revision)
    return PlanRevisionDetail(
        id=revision.id,
        project_id=revision.project_id,
        summary=revision.summary,
        created_at=revision.created_at,
        created_by=revision.created_by,
        kind=revision.kind,
        branch_id=revision.branch_id,
        entity_counts=_entity_counts(payload),
        payload=_public_snapshot_payload(payload),
    )


async def list_revisions(
    session: AsyncSession,
    slug: str,
    offset: int = 0,
    limit: int = PLAN_REVISIONS_DEFAULT_LIMIT,
) -> PlanRevisionList:
    project = await _resolve_project(session, slug)
    total = (
        await session.execute(
            select(func.count(PlanRevision.id)).where(PlanRevision.project_id == project.id)
        )
    ).scalar_one()
    # Named columns, not whole ``PlanRevision`` rows: the list view shows counts
    # and nothing else from the payload, and selecting the entity would bring
    # every snapshot on the page along with it.
    rows = (
        await session.execute(
            select(
                PlanRevision.id,
                PlanRevision.project_id,
                PlanRevision.summary,
                PlanRevision.created_at,
                PlanRevision.created_by,
                PlanRevision.kind,
                PlanRevision.branch_id,
                *_entity_count_columns(session.get_bind().dialect.name),
            )
            .where(PlanRevision.project_id == project.id)
            .order_by(PlanRevision.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return PlanRevisionList(items=[_summary_from_counted_row(row) for row in rows], total=total)


async def _get_revision(
    session: AsyncSession, project_id: uuid.UUID, revision_id: uuid.UUID
) -> PlanRevision:
    revision = await session.get(PlanRevision, revision_id)
    if revision is None or revision.project_id != project_id:
        raise HTTPException(status_code=404, detail="Revision not found")
    return revision


async def get_revision(
    session: AsyncSession, slug: str, revision_id: uuid.UUID
) -> PlanRevisionDetail:
    project = await _resolve_project(session, slug)
    revision = await _get_revision(session, project.id, revision_id)
    return PlanRevisionDetail(
        id=revision.id,
        project_id=revision.project_id,
        summary=revision.summary,
        created_at=revision.created_at,
        created_by=revision.created_by,
        kind=revision.kind,
        branch_id=revision.branch_id,
        entity_counts=_entity_counts(revision.payload or {}),
        payload=_public_snapshot_payload(revision.payload or {}),
    )


async def diff_revisions(
    session: AsyncSession,
    slug: str,
    revision_id: uuid.UUID,
    compare_to: uuid.UUID,
) -> PlanDiff:
    project = await _resolve_project(session, slug)
    new_rev = await _get_revision(session, project.id, revision_id)
    old_rev = await _get_revision(session, project.id, compare_to)
    # Both are snapshots of main, whose rows are their own origin.
    entries = compute_plan_diff_entries(
        old_rev.payload or {}, new_rev.payload or {}, origins_complete=True
    )
    return PlanDiff(
        revision_id=new_rev.id,
        compare_to=old_rev.id,
        entries=entries,
        summary=_summary_counts(entries),
    )
