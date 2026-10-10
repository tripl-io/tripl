"""The merge's field-definition, meta-field and relation arms.

Each applies only the branch's side of a change from the base, so main-only
additions and edits survive an unrelated merge, and updates matched rows in
place so the ids that values and relations hang off are kept.
``_plan_branch_merge_state`` lists where these arms sit in the merge: the
relation arm runs last of all.

Nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.services._plan_branch_merge_state import (
    MergeContext,
    MergedEventTypes,
    MergedFields,
    MergedMetaFields,
)
from tripl.services._plan_merge_slots import merge_slots
from tripl.services.plan_branch_conflicts import main_keeps
from tripl.services.plan_branch_service import _load_for_branch
from tripl.services.scan_config_lookup import (
    name_format_conflict_detail,
    scan_configs_blocking_field_removals,
)


async def _reject_removals_a_scan_names_events_by(
    session: AsyncSession,
    project_id: uuid.UUID,
    removals: Sequence[tuple[uuid.UUID, str, FieldDefinition]],
) -> None:
    """Refuse the whole merge when it would delete a field a scan names events by.

    The THIRD door to the earlier outage, after the drift-accept in
    ``schema_drift_service`` and the plan-UI delete in ``field_service``. A merge
    that drops a FieldDefinition from main is the same ``session.delete(field)``
    with the same consequence: ``generate_events`` builds its format arguments
    only from columns that still have one, so every collection then dies on "the
    event name format references unknown keys".

    **Refusing the whole merge**, with a message naming every offending field, is
    the shape chosen over two alternatives:

    * *Refuse only that deletion and report it in the merge result.* It would
      silently diverge main from the branch that was just declared merged — main
      keeps a field the branch says is gone — and every later three-way merge
      compares against a base that never describes that state. There is also
      nowhere to report it: the merge returns ``PlanBranchDetailResponse``, so
      this needs a new response field and a new UI to read it, i.e. a fourth
      shape for one warning.
    * *Surface it through the merge-conflict machinery.* ``_detect_merge_conflicts``
      is a pure three-way payload diff and ``GET /branches/{id}/conflicts`` renders
      ``ConflictEntity{name, fields:[{field, base, ours, theirs}]}``. A scan-config
      dependency has no base/ours/theirs values and is not a divergence between two
      sides at all — it is an external constraint that would hold even if both
      sides agreed. Forcing it in means either fabricating those three values or
      inventing the fourth shape anyway.

    Blocking a large merge on one field is the cost, and it is the cost every
    other gate in ``merge_branch`` already charges (insufficient approvals, a
    stale base, an unresolved field conflict). The repair is one edit to the
    scan's Event name format, and then the merge goes through untouched.
    """
    # One query per DISTINCT event type rather than one per removed field: a
    # merge deleting twenty fields would otherwise issue twenty SELECTs with the
    # transaction already open. The batching lives in scan_config_lookup so this
    # door still does not assemble the predicate itself.
    naming = await scan_configs_blocking_field_removals(
        session,
        project_id=project_id,
        removals=[(event_type_id, field.name) for event_type_id, _, field in removals],
    )
    blocked: list[str] = []
    for main_event_type_id, event_type_name, field in removals:
        configs = naming.get((main_event_type_id, field.name))
        if configs:
            blocked.append(
                name_format_conflict_detail(
                    field_name=field.name,
                    configs=configs,
                    lead=(
                        "Cannot merge this branch: merging deletes "
                        f"'{event_type_name}.{field.name}' from main."
                    ),
                    then="merge the branch",
                )
            )
    if blocked:
        raise HTTPException(status_code=409, detail=" ".join(blocked))


async def apply_field_definitions(ctx: MergeContext, event_types: MergedEventTypes) -> MergedFields:
    """Field definitions: apply only branch-side deltas from the base.

    Main-only additions and edits therefore survive an unrelated branch merge.
    A field the branch removed is deleted from main, and the whole merge is
    refused when a scan names its events by it.
    """
    session, project_id = ctx.session, ctx.project_id
    base_et_by_name = event_types.base_et_by_name
    branch_ets = event_types.branch_ets
    branch_et_by_name = event_types.branch_et_by_name
    main_ets_after = event_types.main_ets_after
    main_et_name_to_id = event_types.main_et_name_to_id
    branch_et_id_to_name = event_types.branch_et_id_to_name
    main_field_by_key: dict[tuple[str, str], uuid.UUID] = {}
    field_attrs = (
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
    main_et_by_id = {event_type.id: event_type for event_type in main_ets_after}
    main_fields = list(
        (
            await session.execute(
                select(FieldDefinition).where(
                    FieldDefinition.event_type_id.in_(list(main_et_by_id))
                )
            )
        )
        .scalars()
        .all()
    )
    main_fields_by_key = {
        (main_et_by_id[field.event_type_id].name, field.name): field for field in main_fields
    }
    removals: list[tuple[uuid.UUID, str, FieldDefinition]] = []
    for et_name, b_et in branch_et_by_name.items():
        if et_name not in main_et_name_to_id:
            continue
        m_et_id = main_et_name_to_id[et_name]
        base_fields = {
            field["name"]: field
            for field in base_et_by_name.get(et_name, {}).get("field_definitions", [])
        }
        branch_fields = {field.name: field for field in b_et.field_definitions}
        for field_name, b_fd in branch_fields.items():
            key = (et_name, field_name)
            m_fd = main_fields_by_key.get(key)
            base_fd = base_fields.get(field_name)
            # Read before a missing row is created below: a field new on the
            # branch alone takes every value from the branch, its order too.
            main_existed = m_fd is not None
            if m_fd is None:
                if base_fd is not None:
                    continue
                m_fd = FieldDefinition(id=uuid.uuid4(), event_type_id=m_et_id, name=field_name)
                session.add(m_fd)
                main_fields_by_key[key] = m_fd
            for attr in field_attrs:
                if main_existed and main_keeps(attr, base_fd, getattr(m_fd, attr)):
                    continue
                branch_value = getattr(b_fd, attr)
                if base_fd is None or branch_value != base_fd.get(attr):
                    if attr == "enum_options":
                        branch_value = list(branch_value) if branch_value else None
                    setattr(m_fd, attr, branch_value)
        for field_name in set(base_fields) - set(branch_fields):
            m_fd = main_fields_by_key.pop((et_name, field_name), None)
            if m_fd is not None:
                removals.append((m_et_id, et_name, m_fd))

    # Checked here rather than from the payloads in ``merge_branch`` so there is
    # exactly one definition of "fields this merge deletes" — the list the deletes
    # are actually issued from. Nothing is committed yet, so a refusal rolls the
    # whole merge back.
    await _reject_removals_a_scan_names_events_by(session, project_id, removals)
    for _, _, m_fd in removals:
        await session.delete(m_fd)
    await session.flush()

    main_field_by_key = {key: field.id for key, field in main_fields_by_key.items()}

    branch_field_by_id = {
        fd.id: (branch_et_id_to_name[fd.event_type_id], fd.name)
        for et in branch_ets
        for fd in et.field_definitions
    }
    return MergedFields(main_field_by_key=main_field_by_key, branch_field_by_id=branch_field_by_id)


async def apply_meta_fields(ctx: MergeContext) -> MergedMetaFields:
    """Meta-field definitions: upsert by name, ids preserved."""
    session, project_id = ctx.session, ctx.project_id
    main_branch_id, branch_id = ctx.main_branch_id, ctx.branch_id
    main_mfs = await _load_for_branch(session, MetaFieldDefinition, project_id, main_branch_id)
    branch_mfs = await _load_for_branch(session, MetaFieldDefinition, project_id, branch_id)
    main_mf_by_name = {mf.name: mf for mf in main_mfs}
    branch_mf_by_name = {mf.name: mf for mf in branch_mfs}
    base_mf_by_name = {mf["name"]: mf for mf in ctx.base_payload.get("meta_fields", [])}
    meta_attrs = (
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
    for name, b_mf in branch_mf_by_name.items():
        m_mf = main_mf_by_name.get(name)
        if m_mf is not None:
            base_mf = base_mf_by_name.get(name)
            for attr in meta_attrs:
                if main_keeps(attr, base_mf, getattr(m_mf, attr)):
                    continue
                branch_value = getattr(b_mf, attr)
                if base_mf is None or branch_value != base_mf.get(attr):
                    if attr == "enum_options":
                        branch_value = list(branch_value) if branch_value else None
                    setattr(m_mf, attr, branch_value)
        else:
            if name in base_mf_by_name:
                continue
            session.add(
                MetaFieldDefinition(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    branch_id=main_branch_id,
                    name=b_mf.name,
                    display_name=b_mf.display_name,
                    field_type=b_mf.field_type,
                    is_required=b_mf.is_required,
                    allow_multiple=b_mf.allow_multiple,
                    enum_options=list(b_mf.enum_options) if b_mf.enum_options else None,
                    default_value=b_mf.default_value,
                    link_template=b_mf.link_template,
                    order=b_mf.order,
                    sensitivity=b_mf.sensitivity,
                )
            )
    for name, m_mf in list(main_mf_by_name.items()):
        if name in base_mf_by_name and name not in branch_mf_by_name:
            await session.delete(m_mf)
    await session.flush()
    main_mf_name_to_id: dict[str, uuid.UUID] = {
        mf.name: mf.id
        for mf in await _load_for_branch(session, MetaFieldDefinition, project_id, main_branch_id)
    }
    branch_mf_id_to_name = {mf.id: mf.name for mf in branch_mfs}
    return MergedMetaFields(
        main_mf_name_to_id=main_mf_name_to_id, branch_mf_id_to_name=branch_mf_id_to_name
    )


async def apply_relations(
    ctx: MergeContext, event_types: MergedEventTypes, fields: MergedFields
) -> None:
    """Relations: a three-way apply, row by row.

    Paired like the events (``apply_events``): main's by their own ids, the
    branch's by origin id, the natural key only for rows neither places.
    Nothing makes a relation's four names unique either.
    """
    session, project_id = ctx.session, ctx.project_id
    main_branch_id, branch_id = ctx.main_branch_id, ctx.branch_id
    branch_origins_complete = ctx.branch_origins_complete
    branch_ets = event_types.branch_ets
    branch_et_id_to_name = event_types.branch_et_id_to_name
    main_et_name_to_id = event_types.main_et_name_to_id
    main_field_by_key = fields.main_field_by_key
    branch_relations = await _load_for_branch(session, EventTypeRelation, project_id, branch_id)
    branch_fd_id_to_key = {
        fd.id: (branch_et_id_to_name[fd.event_type_id], fd.name)
        for et in branch_ets
        for fd in et.field_definitions
    }
    main_relations = await _load_for_branch(session, EventTypeRelation, project_id, main_branch_id)
    main_et_id_to_name_after = {et_id: name for name, et_id in main_et_name_to_id.items()}
    main_fd_id_to_key = {field_id: key for key, field_id in main_field_by_key.items()}

    def relation_key(
        relation: EventTypeRelation,
        et_names: dict[uuid.UUID, str],
        field_keys: dict[uuid.UUID, tuple[str, str]],
    ) -> tuple[str, str, str, str]:
        source_field = field_keys[relation.source_field_id]
        target_field = field_keys[relation.target_field_id]
        return (
            et_names[relation.source_event_type_id],
            source_field[1],
            et_names[relation.target_event_type_id],
            target_field[1],
        )

    relation_slots = merge_slots(
        list(ctx.base_payload.get("relations", [])),
        [
            relation
            for relation in main_relations
            # ``relation_key`` indexes both maps directly, so a relation naming
            # an end that is not on this side was a KeyError — a 500 on the
            # merge with nothing saying which relation.
            # ``relation_service.create_relation`` now refuses to write one, but
            # rows stored before that refusal have no migration sweeping them,
            # and the merge is where they surface. Main needs the event-type half
            # as much as the branch does: ``main_et_id_to_name_after`` covers
            # MAIN's types only, and the four ids were never checked against
            # each other, so a main relation can hold a branch copy's type id
            # beside a main field id.
            if relation.source_field_id in main_fd_id_to_key
            and relation.target_field_id in main_fd_id_to_key
            and relation.source_event_type_id in main_et_id_to_name_after
            and relation.target_event_type_id in main_et_id_to_name_after
        ],
        [
            relation
            for relation in branch_relations
            # Guarded the same way main's are above, and for the same reason.
            if relation.source_field_id in branch_fd_id_to_key
            and relation.target_field_id in branch_fd_id_to_key
            and relation.source_event_type_id in branch_et_id_to_name
            and relation.target_event_type_id in branch_et_id_to_name
        ],
        base_key=lambda relation: (
            relation["source_event_type_name"],
            relation["source_field_name"],
            relation["target_event_type_name"],
            relation["target_field_name"],
        ),
        main_key=lambda relation: relation_key(
            relation, main_et_id_to_name_after, main_fd_id_to_key
        ),
        branch_key=lambda relation: relation_key(
            relation, branch_et_id_to_name, branch_fd_id_to_key
        ),
        main_ref=lambda relation: str(relation.id),
        branch_ref=lambda relation: str(relation.origin_id or relation.id),
        # A relation whose ends moved is carried as a removal plus an addition,
        # as the natural-key merge always carried it.
        follows_rename=lambda old_key, new_key: False,
        branch_origins_complete=branch_origins_complete,
    )
    for relation_slot in relation_slots.slots:
        m_rel, b_rel, base_relation = (
            relation_slot.main,
            relation_slot.branch,
            relation_slot.base,
        )
        if m_rel is None:
            continue
        if b_rel is None:
            if base_relation is not None and relation_slot.branch_known:
                await session.delete(m_rel)
            continue
        if base_relation is None or b_rel.relation_type != base_relation.get("relation_type"):
            m_rel.relation_type = b_rel.relation_type
        if base_relation is None or b_rel.description != base_relation.get("description"):
            m_rel.description = b_rel.description
    for b_rel in relation_slots.created:
        src_et_name, _src_field_name, tgt_et_name, _tgt_field_name = relation_key(
            b_rel, branch_et_id_to_name, branch_fd_id_to_key
        )
        session.add(
            EventTypeRelation(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=main_branch_id,
                source_event_type_id=main_et_name_to_id[src_et_name],
                target_event_type_id=main_et_name_to_id[tgt_et_name],
                source_field_id=main_field_by_key[branch_fd_id_to_key[b_rel.source_field_id]],
                target_field_id=main_field_by_key[branch_fd_id_to_key[b_rel.target_field_id]],
                relation_type=b_rel.relation_type,
                description=b_rel.description,
            )
        )
