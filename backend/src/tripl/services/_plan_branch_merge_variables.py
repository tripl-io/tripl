"""The merge's variable arm, and the arm for their per-event value overrides.

A variable renamed on the branch stays one row: main's is renamed in place,
through the parking pass "Update from main" shares
(``rename_variables_with_parking``). The override arm runs after the events,
since each override follows its branch event to the main row that event landed
on. ``_plan_branch_merge_state`` lists where these arms sit in the merge.

Nothing here commits.
"""

from __future__ import annotations

import copy
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import (
    VariableEventValueOverride,
    copy_override_values,
)
from tripl.services._plan_branch_merge_state import MergeContext, MergedEvents, MergedVariables
from tripl.services._plan_branch_renames import pair_renames, rekey_in_place
from tripl.services.plan_branch_service import _load_for_branch

# A name parked here exists only between the two flushes in
# ``_rename_main_variables``, inside the merge's own transaction. The prefix is
# deliberately outside what ``VariableCreate`` admits (``^[a-z][a-z0-9_]*$``),
# so a value that ever escaped the transaction would be unmistakable rather than
# look like a variable someone named badly.
_RENAME_STAGING_PREFIX = "__merge_rename_"


async def _rename_main_variables(
    session: AsyncSession,
    main_var_by_name: dict[str, Variable],
    renames: dict[str, str],
) -> None:
    """Write the branch's new names onto main's rows, cycles included.

    ``pair_renames`` can hand back a permutation — a plain two-variable swap, or
    a longer rotation — and then at least one row is moving onto a name another
    main row still holds. ``uq_variable_project_name`` is UNIQUE and NOT
    DEFERRABLE — ``Variable`` declares it as a plain ``UniqueConstraint``
    (``models/variable.py``), which is the immediate form — so there is no order
    of the UPDATEs that avoids a duplicate existing between two of them: only a
    third value does. Park every mover on one, flush that, then write the real
    names.

    ``4e5f60718293`` is the migration that gave the constraint its current
    ``(project_id, branch_id, name)`` shape, not ``d4f5e6a7b8c9``: that later
    revision only re-asserts both variable constraints ``IF NOT EXISTS`` for
    drifted environments, and on a database built by running the chain in order
    it creates nothing at all — its own downgrade comment says so.

    Parking ALL of them rather than only the ones that look blocked is what
    makes the second pass safe in any order, which matters because the order is
    SQLAlchemy's and not ours — the pending names go out on whichever flush
    comes first, and that is usually an unrelated autoflush further down.

    ``main_var_by_name`` is read here before ``rekey_in_place`` moves it, so its
    keys are still the OLD names and its membership is still main's pre-rename
    name set — which is exactly the question "is this destination occupied?".
    """
    movers = [(main_var_by_name[old_name], new_name) for old_name, new_name in renames.items()]
    if any(new_name in main_var_by_name for new_name in renames.values()):
        for variable, _new_name in movers:
            variable.name = f"{_RENAME_STAGING_PREFIX}{uuid.uuid4().hex}"
        await session.flush()
    for variable, new_name in movers:
        variable.name = new_name


# Nothing in it is main-specific — it renames whichever rows it is handed — so
# "Update from main" moves a branch's variables through the same parking pass
# when main renamed them, cycles included.
rename_variables_with_parking = _rename_main_variables


async def _load_variables(
    session: AsyncSession, project_id: uuid.UUID, branch_id: uuid.UUID
) -> list[Variable]:
    """A branch's variables without their observed-value contexts.

    ``Variable.value_contexts`` is ``lazy="selectin"``, and nothing in the merge
    reads it: a bare select would pull every context row and its
    FieldDefinition on each of the three loads, inside the open merge
    transaction — the cost already removed from ``build_plan_snapshot``. A
    deleted variable still cascades its contexts; the ORM loads them at delete
    time.
    """
    rows = await session.execute(
        select(Variable)
        .where(Variable.project_id == project_id, Variable.branch_id == branch_id)
        .options(lazyload(Variable.value_contexts))
    )
    return list(rows.scalars().all())


async def apply_variables(ctx: MergeContext) -> MergedVariables:
    """Variables: upsert by name, renames paired onto main's own rows."""
    session, project_id = ctx.session, ctx.project_id
    main_branch_id, branch_id = ctx.main_branch_id, ctx.branch_id
    main_vars = await _load_variables(session, project_id, main_branch_id)
    branch_vars = await _load_variables(session, project_id, branch_id)
    main_var_by_name = {v.name: v for v in main_vars}
    branch_var_by_name = {v.name: v for v in branch_vars}
    base_var_by_name = {v["name"]: v for v in ctx.base_payload.get("variables", [])}
    # A rename is one row, not a removal plus an addition. Unpaired, the arms
    # below add a Variable carrying main's own ``source_name`` to main while the
    # delete of the row it replaces is still pending in the same flush —
    # SQLAlchemy orders a mapper's saves ahead of its deletes — so
    # ``uq_variable_project_source_name`` fails the whole merge with an
    # IntegrityError. Renaming main's row instead settles that and keeps the
    # variable's id, which every ``variable_values`` row hangs off.
    #
    # A swap or a rotation is several renames at once, so the moves have to be
    # applied as the permutation they are: the names through a parking value
    # (``_rename_main_variables``) and the lookups all-at-once
    # (``rekey_in_place``). Doing either one pair at a time re-creates the very
    # collision the pairing removes.
    var_renames = {
        old_key[0]: new_key[0]
        for old_key, new_key in pair_renames(
            {(name,): variable.get("source_name") for name, variable in base_var_by_name.items()},
            {(name,): variable.source_name for name, variable in main_var_by_name.items()},
            {(name,): variable.source_name for name, variable in branch_var_by_name.items()},
            vacate_removed=True,
        ).items()
    }
    # A move onto a name a non-moving main row holds is only proposed when the
    # branch deleted that row. It has to go FIRST and be flushed on
    # its own: SQLAlchemy orders a mapper's saves ahead of its deletes, so left
    # to the removal loop below it would still hold the name and the
    # ``source_name`` slot when the renamed row's UPDATE goes out.
    displaced = [
        main_var_by_name.pop(new_name)
        for new_name in var_renames.values()
        if new_name in main_var_by_name and new_name not in var_renames
    ]
    if displaced:
        for occupant in displaced:
            await session.delete(occupant)
        await session.flush()
    if var_renames:
        await _rename_main_variables(session, main_var_by_name, var_renames)
        rekey_in_place(main_var_by_name, var_renames)
        # The base entry has to move with it. Every comparison downstream reads
        # ``base_var_by_name.get(name)`` and falls back to branch-wins when the
        # entry is missing, so a base left under the old name would silently
        # overwrite main-only edits on the row that was renamed.
        rekey_in_place(base_var_by_name, var_renames)
    variable_attrs = (
        "source_name",
        "variable_type",
        "description",
        "allowed_values",
        "bindings",
        "excluded_from_scans",
        "json_schema",
    )
    for name, b_v in branch_var_by_name.items():
        m_v = main_var_by_name.get(name)
        if m_v is not None:
            base_var = base_var_by_name.get(name)
            # ``variable_type`` and ``json_schema`` are taken attribute by
            # attribute like the rest; they cannot come from different sides,
            # because the conflict check compares them as one value and refuses
            # the merge when both sides changed it (``comparable_field``).
            for attr in variable_attrs:
                branch_value = getattr(b_v, attr)
                if base_var is None or branch_value != base_var.get(attr):
                    if attr in ("allowed_values", "bindings"):
                        branch_value = list(branch_value or [])
                    elif attr == "json_schema":
                        branch_value = copy.deepcopy(branch_value)
                    setattr(m_v, attr, branch_value)
        else:
            if name in base_var_by_name:
                continue
            session.add(
                Variable(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    branch_id=main_branch_id,
                    name=b_v.name,
                    source_name=b_v.source_name,
                    variable_type=b_v.variable_type,
                    description=b_v.description,
                    allowed_values=list(b_v.allowed_values or []),
                    bindings=list(b_v.bindings or []),
                    json_schema=copy.deepcopy(b_v.json_schema),
                    excluded_from_scans=b_v.excluded_from_scans,
                )
            )

    # Removals go out AFTER the writes above, in the same flush, and that is
    # deliberate rather than left over. SQLAlchemy runs a mapper's saves ahead of
    # its deletes, so a name or a ``source_name`` still held by a row on its way
    # out is NOT free for the row taking it — and when the two arms disagree
    # about one identity, the flush raises and ``_commit_merged_plan`` turns that
    # into a 409 that loses nothing.
    #
    # Deleting first would make that collision succeed instead, which is worse
    # than it sounds. Base and main both hold ``a``/S1 and ``b``/S2; the branch
    # deletes ``b`` and renames ``a`` to ``b``. ``pair_renames`` proposes a -> b
    # and then drops it, because main's own ``b`` is not itself moving away — so
    # no rename is applied. Removing first would then delete main's ``a`` with
    # its ``variable_values``, ``variable_value_drifts`` and
    # ``variable_event_value_overrides``, and the upsert would write S1 onto
    # main's surviving ``b``: the row the user KEPT gone with all its observed
    # values and drift triage, the row the user DELETED left wearing the kept
    # row's scan identity, and the next scan matching warehouse data onto the
    # wrong history. Nothing in ``build_plan_snapshot`` would show it.
    #
    # The one unambiguous version of that shape — the branch deleted ``b``, whose
    # identity S2 no branch row carries any more, and ``b`` still wears S2 on
    # main as it did at the cut — is now paired, and its occupant deleted and
    # flushed ahead of the rename above. Every other merge that
    # wants both the deletion and the move onto the freed name is still
    # ambiguous, and 409 asking the user to rename the clashing entity is the
    # honest answer. Cycles do NOT rely on this order: the parking
    # pass in ``_rename_main_variables`` is what makes a swap or a rotation work,
    # and it operates on names before either arm runs.
    for name, m_v in list(main_var_by_name.items()):
        if name in base_var_by_name and name not in branch_var_by_name:
            await session.delete(m_v)
    return MergedVariables(branch_vars=branch_vars, base_var_by_name=base_var_by_name)


async def apply_value_overrides(
    ctx: MergeContext, variables: MergedVariables, events: MergedEvents
) -> None:
    """Variable event value overrides, replaced per variable.

    Only for variables whose branch-side override map changed from the base.
    Each override follows its branch event to the main row that event landed
    on, not to whichever main row shares the event's (type, name).
    """
    session, project_id = ctx.session, ctx.project_id
    main_branch_id, branch_id = ctx.main_branch_id, ctx.branch_id
    branch_snapshot_payload = ctx.branch_snapshot_payload
    branch_vars, base_var_by_name = variables.branch_vars, variables.base_var_by_name
    main_target_by_branch_id = events.main_target_by_branch_id
    surviving_main_ids = events.surviving_main_ids
    main_vars_after = await _load_variables(session, project_id, main_branch_id)
    main_var_name_to_id = {v.name: v.id for v in main_vars_after}
    branch_overrides = await _load_for_branch(
        session, VariableEventValueOverride, project_id, branch_id
    )
    branch_overrides_by_var: dict[uuid.UUID, list[VariableEventValueOverride]] = {}
    for override in branch_overrides:
        branch_overrides_by_var.setdefault(override.variable_id, []).append(override)
    branch_var_snapshot_by_name = {
        variable["name"]: variable for variable in branch_snapshot_payload.get("variables", [])
    }
    for branch_var in branch_vars:
        name = branch_var.name
        base_var = base_var_by_name.get(name)
        branch_var_snapshot = branch_var_snapshot_by_name[name]
        if base_var is not None and branch_var_snapshot.get(
            "event_value_overrides"
        ) == base_var.get("event_value_overrides"):
            continue
        main_var_id = main_var_name_to_id.get(name)
        if main_var_id is None:
            continue
        await session.execute(
            delete(VariableEventValueOverride).where(
                VariableEventValueOverride.project_id == project_id,
                VariableEventValueOverride.branch_id == main_branch_id,
                VariableEventValueOverride.variable_id == main_var_id,
            )
        )
        for override in branch_overrides_by_var.get(branch_var.id, []):
            landed = main_target_by_branch_id.get(override.event_id)
            if landed is None or landed.id not in surviving_main_ids:
                continue
            session.add(
                VariableEventValueOverride(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    branch_id=main_branch_id,
                    variable_id=main_var_id,
                    event_id=landed.id,
                    values=copy_override_values(override.values),
                    required=override.required,
                )
            )
    await session.flush()
