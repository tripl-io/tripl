from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.plan_branch_merge_resolution import PlanBranchMergeResolution
from tripl.models.plan_revision import PlanRevision
from tripl.schemas.plan_branch import (
    BranchConflictsResponse,
    ConflictEntity,
    ConflictField,
    ResolutionCreate,
    ResolutionResponse,
)
from tripl.services._origin_pairing import pair_rows, snapshot_id, snapshot_ref
from tripl.services.plan_branch_service import (
    _get_branch,
    _reject_main,
    _resolve_project,
    ensure_main_branch_id,
)
from tripl.services.plan_revision_service import (
    PLAN_SNAPSHOT_VERSION,
    build_plan_snapshot,
    compute_plan_diff_entries,
    photos_without_comments,
    with_snapshot_defaults,
)

# --- 3-way merge engine ---------------------------------------------------
#
# base   = snapshot of main at branch open (stored as PlanBranch.base_revision)
# ours   = current main snapshot
# theirs = current branch snapshot
#
# A conflict is "same entity changed on both sides" (vs base). When clean, we
# apply theirs onto main *by natural key* — matched event_types/events keep
# their live ids, so attached runtime rows (metrics/photos/alerts) survive.

_ET_CHANGE_KEYS = ("display_name", "description", "color", "order")
_FD_CHANGE_KEYS = (
    "display_name",
    "field_type",
    "is_required",
    "enum_options",
    "description",
    "order",
    "sensitivity",
    "contract_required_max_null_rate",
    "contract_regex",
    "contract_min_value",
    "contract_max_value",
    "contract_max_bad_rate",
)
_EV_CHANGE_KEYS = (
    "source_name",
    # `title` is here for the same reason `description` is: it is authored text
    # that two people can write differently. It was added to the event and to
    # the DIFF's key list without reaching this one, so a title edited on both
    # sides merged silently, one side winning with nothing reported.
    "title",
    "description",
    "status",
    "sunset_at",
    # Serialized as a natural key, not a uuid — see the snapshot builder.
    "superseded_by",
    "order",
    "owner_id",
    "reviewed",
    "metric_breakdown_columns",
    "required_presence_threshold",
    "field_values",
    "meta_values",
    "tags",
    "photos",
)
_VAR_CHANGE_KEYS = (
    "source_name",
    "variable_type",
    "description",
    "allowed_values",
    "bindings",
    "excluded_from_scans",
    "event_value_overrides",
)
# No ``json_schema`` key above: ``comparable_field`` folds it into
# ``variable_type``, so the type is compared, conflicted on and resolved as one
# value. Two keys let main's retype and the branch's new schema pass as edits
# to different fields and land as a pair that contradicts itself.
_MF_CHANGE_KEYS = (
    "display_name",
    "field_type",
    "is_required",
    "allow_multiple",
    "enum_options",
    "default_value",
    "link_template",
    "order",
    "sensitivity",
)
_REL_CHANGE_KEYS = ("relation_type", "description")


def _flatten_fields(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for et in payload.get("event_types", []):
        for fd in et.get("field_definitions", []):
            out.append({**fd, "_et": et["name"]})
    return out


def comparable_field(item: dict[str, Any], field: str) -> Any:
    """The part of a snapshot field that counts as a change to the plan.

    ``build_plan_snapshot`` nests each photo's comments inside the ``photos``
    subtree, so a comment used to read as "this event changed". That is not what
    a comment is — it is discussion hanging off the design, and it describes no
    part of the tracking plan. The consequence was worse than a noisy diff: a
    comment on main made the event conflict with the branch, an event conflict
    is not resolvable inline, and the branch became unmergeable while the
    conflicts endpoint reported nothing to resolve. Compare the
    attachments; leave the conversation out of it — including the ORDER the
    conversation gave them in the snapshot, which ``photos_without_comments``
    re-sorts away.
    """
    value = item.get(field)
    if field == "variable_type":
        # A variable's type is ``variable_type`` refined by ``json_schema``:
        # one value in two columns (see ``_VAR_CHANGE_KEYS``).
        return {"variable_type": value, "json_schema": item.get("json_schema")}
    if field != "photos" or not isinstance(value, list):
        return value
    return photos_without_comments(value)


def _entity_changed(
    base_item: dict[str, Any] | None,
    new_item: dict[str, Any] | None,
    fields: tuple[str, ...] | list[str],
) -> bool:
    if (base_item is None) != (new_item is None):
        return True
    if base_item is None or new_item is None:
        return False
    return any(comparable_field(base_item, f) != comparable_field(new_item, f) for f in fields)


def _entities_equal(
    a: dict[str, Any] | None,
    b: dict[str, Any] | None,
    fields: tuple[str, ...] | list[str],
) -> bool:
    if (a is None) != (b is None):
        return False
    if a is None or b is None:
        return True
    return all(comparable_field(a, f) == comparable_field(b, f) for f in fields)


def _conflict_set(
    *,
    entity_type: str,
    base_items: list[dict[str, Any]],
    ours_items: list[dict[str, Any]],
    theirs_items: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], Any],
    name_fn: Callable[[dict[str, Any]], str],
    change_keys: tuple[str, ...] | list[str],
    anchored: bool = False,
    theirs_origins_complete: bool = False,
) -> list[dict[str, Any]]:
    if anchored:
        base_by, ours_by, theirs_by = _anchored_slots(
            base_items,
            ours_items,
            theirs_items,
            key_fn,
            theirs_origins_complete=theirs_origins_complete,
        )
    else:
        base_by = {key_fn(item): item for item in base_items}
        ours_by = {key_fn(item): item for item in ours_items}
        theirs_by = {key_fn(item): item for item in theirs_items}

    conflicts: list[dict[str, Any]] = []
    for key in set(ours_by) | set(theirs_by) | set(base_by):
        b = base_by.get(key)
        o = ours_by.get(key)
        t = theirs_by.get(key)
        if b is not None and o is not None and t is not None:
            same_field_conflict = any(
                comparable_field(o, field) != comparable_field(b, field)
                and comparable_field(t, field) != comparable_field(b, field)
                and comparable_field(o, field) != comparable_field(t, field)
                for field in change_keys
            )
            if same_field_conflict:
                display = name_fn(o)
                conflicts.append({"entity_type": entity_type, "name": display})
            continue
        ours_changed = _entity_changed(b, o, change_keys)
        theirs_changed = _entity_changed(b, t, change_keys)
        if ours_changed and theirs_changed and not _entities_equal(o, t, change_keys):
            display = name_fn(o or t or b or {})
            conflicts.append({"entity_type": entity_type, "name": display})
    return conflicts


def _anchored_slots(
    base_items: list[dict[str, Any]],
    ours_items: list[dict[str, Any]],
    theirs_items: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], Any],
    *,
    theirs_origins_complete: bool,
) -> tuple[dict[Any, dict[str, Any]], dict[Any, dict[str, Any]], dict[Any, dict[str, Any]]]:
    """The three sides keyed by (natural key, base row id), not by the key alone.

    For entities whose natural key several rows may share (events, relations).
    Keyed one row per key, two namesakes collapsed to whichever was listed last
    on each side, so main's edit to one and the branch's edit to the other read
    as a conflict, and two edits to the SAME one could compare two different
    rows and pass — after which the merge, which pairs by origin id, wrote the
    branch's value over main's. Each side is placed against
    the base with ``pair_rows``: main's rows by their own ids, the branch's by
    their origin ids. A key the branch's pairing leaves ambiguous (a branch
    opened before origin ids) falls back to the old one-row-per-key slot on
    all three sides; so does a row added on a side, so that two sides adding
    one name still meet.
    """
    pairings = [
        pair_rows(
            base_items,
            side,
            key_of_old=key_fn,
            key_of_new=key_fn,
            id_of_old=snapshot_id,
            ref_of_new=snapshot_ref,
            unplaced_are_new=complete,
        )
        for side, complete in ((ours_items, True), (theirs_items, theirs_origins_complete))
    ]
    collapsed = {key for pairing in pairings for key in pairing.ambiguous}

    def base_slot(item: dict[str, Any]) -> tuple[Any, Any]:
        key = key_fn(item)
        return (key, None) if key in collapsed else (key, snapshot_id(item))

    base_by = {base_slot(item): item for item in base_items}
    sides: list[dict[Any, dict[str, Any]]] = []
    for pairing in pairings:
        by_slot: dict[Any, dict[str, Any]] = {}
        for base_item, item in pairing.pairs:
            by_slot[base_slot(base_item)] = item
        for item in [
            *pairing.added,
            *(item for _, item in pairing.renamed),
            *(item for _, items in pairing.ambiguous.values() for item in items),
        ]:
            by_slot[(key_fn(item), None)] = item
        sides.append(by_slot)
    return base_by, sides[0], sides[1]


def _event_type_add_remove_conflicts(
    base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]
) -> list[dict[str, Any]]:
    """add/remove-class conflicts on event_type — modify-vs-modify is handled
    at field level via _field_conflicts_event_type. Conflict only if BOTH
    sides made a divergent change (one-sided edits auto-merge)."""
    base_by = {e["name"]: e for e in base.get("event_types", [])}
    ours_by = {e["name"]: e for e in ours.get("event_types", [])}
    theirs_by = {e["name"]: e for e in theirs.get("event_types", [])}

    conflicts: list[dict[str, Any]] = []
    for name in set(base_by) | set(ours_by) | set(theirs_by):
        b = base_by.get(name)
        o = ours_by.get(name)
        t = theirs_by.get(name)
        # Modify-vs-modify path lives in _field_conflicts_event_type.
        if b is not None and o is not None and t is not None:
            continue
        # A parent deletion races not only with parent metadata edits but also
        # with additions/edits to dependent fields, events, and relations.
        # Treat that dependency state as part of the parent for add/remove
        # conflicts so deleting a type cannot silently discard a main-only
        # child added after the branch was opened.
        ours_changed = _entity_changed(b, o, _ET_CHANGE_KEYS)
        theirs_changed = _entity_changed(b, t, _ET_CHANGE_KEYS)
        if b is not None and (o is None or t is None):
            base_deps = _event_type_dependency_state(base, name)
            ours_changed = ours_changed or _event_type_dependency_state(ours, name) != base_deps
            theirs_changed = (
                theirs_changed or _event_type_dependency_state(theirs, name) != base_deps
            )
        if ours_changed and theirs_changed and not _entities_equal(o, t, _ET_CHANGE_KEYS):
            conflicts.append({"entity_type": "event_type", "name": name})
    return conflicts


def _event_type_dependency_state(payload: dict[str, Any], name: str) -> dict[str, Any]:
    event_type = next(
        (event_type for event_type in payload.get("event_types", []) if event_type["name"] == name),
        None,
    )
    state = {
        "fields": (event_type or {}).get("field_definitions", []),
        "events": [
            event for event in payload.get("events", []) if event.get("event_type_name") == name
        ],
        "relations": [
            relation
            for relation in payload.get("relations", [])
            if relation.get("source_event_type_name") == name
            or relation.get("target_event_type_name") == name
        ],
    }

    def without_ids(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: without_ids(item)
                for key, item in value.items()
                if key
                not in {
                    "id",
                    "event_type_id",
                    "source_event_type_id",
                    "target_event_type_id",
                    "source_field_id",
                    "target_field_id",
                }
            }
        if isinstance(value, list):
            return [without_ids(item) for item in value]
        return value

    cleaned = without_ids(state)
    assert isinstance(cleaned, dict)
    return cleaned


def _field_definition_dependency_state(
    payload: dict[str, Any], event_type_name: str, field_name: str
) -> dict[str, Any]:
    event_values = [
        {
            "event_type_name": event.get("event_type_name"),
            "event_name": event.get("name"),
            "value": value,
        }
        for event in payload.get("events", [])
        if event.get("event_type_name") == event_type_name
        for value in event.get("field_values", [])
        if value.get("field_name") == field_name
    ]
    event_values.sort(
        key=lambda item: (
            str(item["event_type_name"]),
            str(item["event_name"]),
            str(item["value"]),
        )
    )
    return {
        "event_values": event_values,
        "relations": [
            relation
            for relation in payload.get("relations", [])
            if (
                relation.get("source_event_type_name") == event_type_name
                and relation.get("source_field_name") == field_name
            )
            or (
                relation.get("target_event_type_name") == event_type_name
                and relation.get("target_field_name") == field_name
            )
        ],
    }


def _meta_field_dependency_state(payload: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    values = [
        {
            "event_type_name": event.get("event_type_name"),
            "event_name": event.get("name"),
            "value": value,
        }
        for event in payload.get("events", [])
        for value in event.get("meta_values", [])
        if value.get("meta_field_name") == field_name
    ]
    return sorted(
        values,
        key=lambda item: (
            str(item["event_type_name"]),
            str(item["event_name"]),
            str(item["value"]),
        ),
    )


def _definition_dependency_conflicts(
    base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    base_fields = {
        (event_type["name"], field["name"])
        for event_type in base.get("event_types", [])
        for field in event_type.get("field_definitions", [])
    }
    ours_fields = {
        (event_type["name"], field["name"])
        for event_type in ours.get("event_types", [])
        for field in event_type.get("field_definitions", [])
    }
    theirs_fields = {
        (event_type["name"], field["name"])
        for event_type in theirs.get("event_types", [])
        for field in event_type.get("field_definitions", [])
    }
    for event_type_name, field_name in base_fields:
        base_state = _field_definition_dependency_state(base, event_type_name, field_name)
        if (event_type_name, field_name) not in theirs_fields and (
            _field_definition_dependency_state(ours, event_type_name, field_name) != base_state
        ):
            conflicts.append(
                {
                    "entity_type": "field_definition",
                    "name": f"{event_type_name}.{field_name}",
                }
            )
        if (event_type_name, field_name) not in ours_fields and (
            _field_definition_dependency_state(theirs, event_type_name, field_name) != base_state
        ):
            conflicts.append(
                {
                    "entity_type": "field_definition",
                    "name": f"{event_type_name}.{field_name}",
                }
            )

    base_meta = {field["name"] for field in base.get("meta_fields", [])}
    ours_meta = {field["name"] for field in ours.get("meta_fields", [])}
    theirs_meta = {field["name"] for field in theirs.get("meta_fields", [])}
    for field_name in base_meta:
        meta_base_state = _meta_field_dependency_state(base, field_name)
        if (
            field_name not in theirs_meta
            and _meta_field_dependency_state(ours, field_name) != meta_base_state
        ):
            conflicts.append({"entity_type": "meta_field", "name": field_name})
        if (
            field_name not in ours_meta
            and _meta_field_dependency_state(theirs, field_name) != meta_base_state
        ):
            conflicts.append({"entity_type": "meta_field", "name": field_name})
    return conflicts


def _detect_merge_conflicts(
    base: dict[str, Any],
    ours: dict[str, Any],
    theirs: dict[str, Any],
    *,
    theirs_origins_complete: bool = False,
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    conflicts.extend(_event_type_add_remove_conflicts(base, ours, theirs))
    conflicts.extend(
        _conflict_set(
            entity_type="field_definition",
            base_items=_flatten_fields(base),
            ours_items=_flatten_fields(ours),
            theirs_items=_flatten_fields(theirs),
            key_fn=lambda x: (x["_et"], x["name"]),
            name_fn=lambda x: f"{x['_et']}.{x['name']}",
            change_keys=_FD_CHANGE_KEYS,
        )
    )
    conflicts.extend(
        _conflict_set(
            entity_type="event",
            base_items=base.get("events", []),
            ours_items=ours.get("events", []),
            theirs_items=theirs.get("events", []),
            key_fn=lambda x: (x["event_type_name"], x["name"]),
            name_fn=lambda x: f"{x['event_type_name']}.{x['name']}",
            change_keys=_EV_CHANGE_KEYS,
            anchored=True,
            theirs_origins_complete=theirs_origins_complete,
        )
    )
    conflicts.extend(
        _conflict_set(
            entity_type="variable",
            base_items=base.get("variables", []),
            ours_items=ours.get("variables", []),
            theirs_items=theirs.get("variables", []),
            key_fn=lambda x: x["name"],
            name_fn=lambda x: x["name"],
            change_keys=_VAR_CHANGE_KEYS,
        )
    )
    conflicts.extend(
        _conflict_set(
            entity_type="meta_field",
            base_items=base.get("meta_fields", []),
            ours_items=ours.get("meta_fields", []),
            theirs_items=theirs.get("meta_fields", []),
            key_fn=lambda x: x["name"],
            name_fn=lambda x: x["name"],
            change_keys=_MF_CHANGE_KEYS,
        )
    )
    conflicts.extend(
        _conflict_set(
            entity_type="relation",
            base_items=base.get("relations", []),
            ours_items=ours.get("relations", []),
            theirs_items=theirs.get("relations", []),
            key_fn=lambda x: (
                x["source_event_type_name"],
                x["source_field_name"],
                x["target_event_type_name"],
                x["target_field_name"],
            ),
            name_fn=lambda x: (
                f"{x['source_event_type_name']}.{x['source_field_name']}"
                f"->{x['target_event_type_name']}.{x['target_field_name']}"
            ),
            change_keys=_REL_CHANGE_KEYS,
            anchored=True,
            theirs_origins_complete=theirs_origins_complete,
        )
    )
    conflicts.extend(_definition_dependency_conflicts(base, ours, theirs))
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for conflict in conflicts:
        key = (conflict["entity_type"], conflict["name"])
        if key not in seen:
            seen.add(key)
            unique.append(conflict)
    return unique


# --- inline 3-way field conflicts (v1 covers event_type metadata only) -------


def _field_conflicts_event_type(
    base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]
) -> list[dict[str, Any]]:
    """Per-field conflicts on event_type metadata.

    Returns one dict per (entity_name, field) where main and the branch both
    changed the value vs base and the two new values disagree. The shape feeds
    the inline-resolution UI: name + field + base/ours/theirs values.
    """
    base_by = {e["name"]: e for e in base.get("event_types", [])}
    ours_by = {e["name"]: e for e in ours.get("event_types", [])}
    theirs_by = {e["name"]: e for e in theirs.get("event_types", [])}

    rows: list[dict[str, Any]] = []
    for name in set(base_by) | set(ours_by) | set(theirs_by):
        b = base_by.get(name)
        o = ours_by.get(name)
        t = theirs_by.get(name)
        # Adds and removes are not field-level — they bubble up to the
        # entity-level _detect_merge_conflicts path. Skip here.
        if b is None or o is None or t is None:
            continue
        for field in _ET_CHANGE_KEYS:
            bv = b.get(field)
            ov = o.get(field)
            tv = t.get(field)
            if ov != bv and tv != bv and ov != tv:
                rows.append(
                    {
                        "entity_type": "event_type",
                        "name": name,
                        "field": field,
                        "base": bv,
                        "ours": ov,
                        "theirs": tv,
                    }
                )
    return rows


async def _load_resolutions(
    session: AsyncSession, branch_id: uuid.UUID
) -> dict[tuple[str, str, str], PlanBranchMergeResolution]:
    rows = (
        (
            await session.execute(
                select(PlanBranchMergeResolution).where(
                    PlanBranchMergeResolution.branch_id == branch_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {(r.entity_type, r.entity_name, r.field_name): r for r in rows}


def detect_field_conflicts(
    base: dict[str, Any],
    main: dict[str, Any],
    branch: dict[str, Any],
    *,
    origins_complete: bool,
) -> list[dict[str, Any]]:
    """Every field both main and the branch changed since the base, all six types.

    One row per ``(entity, field)``: ``{entity_type, name, parent, label, field,
    base, ours, theirs, dependents}``. ``field`` is ``@presence`` where one side
    deleted what the other changed, and an entity both sides added under one
    key reports its differing fields with ``base=None``. The identity each
    entity is matched by is the merge's — see ``_plan_branch_three_way``, which
    "Update from main" applies from, so what this lists is exactly what an
    update asks the user to choose (PL-8).

    ``_field_conflicts_event_type`` stays as the merge's own gate: its rows are
    the event-type slice of these, without the presence rows the merge refuses
    outright.
    """
    # Local import: the three-way module reads this module's change keys.
    from tripl.services._plan_branch_three_way import plan_three_way

    return plan_three_way(base, main, branch, origins_complete=origins_complete).rows


def merge_blocked_by(
    base: dict[str, Any],
    main: dict[str, Any],
    branch: dict[str, Any],
    *,
    origins_complete: bool,
) -> bool:
    """Whether ``merge_branch`` would refuse on a conflict no inline choice settles.

    The merge's own test, in the merge's own words: every entity-level conflict
    that is not an event-type field conflict the resolutions cover.
    """
    resolvable = {
        (row["entity_type"], row["name"]) for row in _field_conflicts_event_type(base, main, branch)
    }
    return any(
        (conflict["entity_type"], conflict["name"]) not in resolvable
        for conflict in _detect_merge_conflicts(
            base, main, branch, theirs_origins_complete=origins_complete
        )
    )


def conflicts_response(
    rows: list[dict[str, Any]],
    resolutions: dict[tuple[str, str, str], str],
    *,
    behind: bool,
    merge_blocked: bool,
    updatable: bool = True,
) -> BranchConflictsResponse:
    """Conflict rows grouped per entity, each with the choice already made for it."""
    by_entity: dict[tuple[str, str], ConflictEntity] = {}
    unresolved = 0
    for row in rows:
        entity_key = (row["entity_type"], row["name"])
        entity = by_entity.get(entity_key)
        if entity is None:
            entity = ConflictEntity(
                entity_type=row["entity_type"],
                name=row["name"],
                parent=row.get("parent"),
                label=row.get("label") or row["name"],
                fields=[],
            )
            by_entity[entity_key] = entity
        if any(existing.field == row["field"] for existing in entity.fields):
            # Namesakes share one key, so one row stands for all of them.
            continue
        choice = resolutions.get((row["entity_type"], row["name"], row["field"]))
        if choice is None:
            unresolved += 1
        entity.fields.append(
            ConflictField(
                field=row["field"],
                base=row["base"],
                ours=row["ours"],
                theirs=row["theirs"],
                choice=choice,
                dependents=row.get("dependents", 0),
            )
        )
    order = {
        name: index
        for index, name in enumerate(
            ("event_type", "meta_field", "variable", "field_definition", "event", "relation")
        )
    }
    entities = sorted(
        by_entity.values(),
        key=lambda entity: (order.get(entity.entity_type, 99), entity.parent or "", entity.name),
    )
    return BranchConflictsResponse(
        entities=entities,
        unresolved_count=unresolved,
        behind=behind,
        overlap_count=len(entities),
        merge_blocked=merge_blocked,
        updatable=updatable,
    )


def is_behind(base: dict[str, Any], main: dict[str, Any]) -> bool:
    """Main changed since the base: the list's ``behind_base`` test, verbatim."""
    return bool(compute_plan_diff_entries(base, main, origins_complete=True))


async def get_branch_conflicts(
    session: AsyncSession, slug: str, branch_id: uuid.UUID
) -> BranchConflictsResponse:
    project = await _resolve_project(session, slug)
    branch = await _get_branch(session, project.id, branch_id)
    _reject_main(branch)

    main_branch_id = await ensure_main_branch_id(session, project.id)
    base_payload: dict[str, Any] = {}
    if branch.base_revision_id is not None:
        base_rev = await session.get(PlanRevision, branch.base_revision_id)
        if base_rev is not None:
            base_payload = with_snapshot_defaults(base_rev.payload or {})
    main_payload = await build_plan_snapshot(session, project.id, branch_id=main_branch_id)
    branch_payload = await build_plan_snapshot(session, project.id, branch_id=branch.id)
    if not base_payload:
        # A legacy branch with no base has no third side to compare against.
        return BranchConflictsResponse(entities=[], unresolved_count=0, updatable=False)

    # Local import: the three-way module reads this module's change keys.
    from tripl.services._plan_branch_three_way import plan_three_way

    plan = plan_three_way(
        base_payload,
        main_payload,
        branch_payload,
        origins_complete=branch.origin_ids_complete,
    )
    resolutions = await _load_resolutions(session, branch.id)
    return conflicts_response(
        plan.rows,
        {key: resolution.choice for key, resolution in resolutions.items()},
        # Any change of main's the update would bring, cosmetic ones included
        # (a display name, an order): the preview's and the POST's own test,
        # so a merge blocked by such an overlap still offers the update.
        behind=is_behind(base_payload, main_payload)
        or any(any(counts.values()) for counts in plan.main_changes.values()),
        merge_blocked=merge_blocked_by(
            base_payload,
            main_payload,
            branch_payload,
            origins_complete=branch.origin_ids_complete,
        ),
        # A base older than complete merge baselines can never be updated.
        # Other blockers (``UpdateFromMainPreview.blockers``) are left to the
        # update dialog, which names them and what to do about each.
        updatable=base_payload.get("snapshot_version") == PLAN_SNAPSHOT_VERSION,
    )


def validate_resolution(data: ResolutionCreate) -> None:
    """Refuse a resolution naming a field no conflict row can carry (422)."""
    from tripl.services._plan_branch_three_way_model import CHANGE_KEYS, PRESENCE_FIELD

    allowed = {PRESENCE_FIELD, "name", *CHANGE_KEYS[data.entity_type]}
    if data.field_name not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"'{data.field_name}' is not a field of a {data.entity_type} conflict",
        )


async def upsert_resolution(
    session: AsyncSession,
    branch_id: uuid.UUID,
    data: ResolutionCreate,
    user_id: uuid.UUID | None,
) -> PlanBranchMergeResolution:
    """Insert or overwrite one stored choice, without committing."""
    validate_resolution(data)
    existing = await session.scalar(
        select(PlanBranchMergeResolution).where(
            PlanBranchMergeResolution.branch_id == branch_id,
            PlanBranchMergeResolution.entity_type == data.entity_type,
            PlanBranchMergeResolution.entity_name == data.entity_name,
            PlanBranchMergeResolution.field_name == data.field_name,
        )
    )
    if existing is not None:
        existing.choice = data.choice
        existing.resolved_by = user_id
        return existing
    resolution = PlanBranchMergeResolution(
        branch_id=branch_id,
        entity_type=data.entity_type,
        entity_name=data.entity_name,
        field_name=data.field_name,
        choice=data.choice,
        resolved_by=user_id,
    )
    session.add(resolution)
    return resolution


async def save_resolution(
    session: AsyncSession,
    slug: str,
    branch_id: uuid.UUID,
    data: ResolutionCreate,
    user_id: uuid.UUID | None,
) -> ResolutionResponse:
    project = await _resolve_project(session, slug)
    branch = await _get_branch(session, project.id, branch_id)
    _reject_main(branch)
    resolution = await upsert_resolution(session, branch.id, data, user_id)
    await session.commit()
    await session.refresh(resolution)
    return ResolutionResponse.model_validate(resolution)


async def delete_resolution(
    session: AsyncSession,
    slug: str,
    branch_id: uuid.UUID,
    resolution_id: uuid.UUID,
) -> None:
    project = await _resolve_project(session, slug)
    branch = await _get_branch(session, project.id, branch_id)
    _reject_main(branch)
    resolution = await session.get(PlanBranchMergeResolution, resolution_id)
    if resolution is None or resolution.branch_id != branch.id:
        raise HTTPException(status_code=404, detail="Resolution not found")
    await session.delete(resolution)
    await session.commit()
