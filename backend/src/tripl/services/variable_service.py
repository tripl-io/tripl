import re
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload
from sqlalchemy.sql.elements import ColumnElement

from tripl.core.property_drift import effective_threshold
from tripl.core.property_schema import PropertySchemaError, check_schema_matches_type
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import (
    VariableEventValueOverride,
    copy_override_values,
)
from tripl.models.variable_value import VariableValue
from tripl.schemas.variable import (
    BINDING_PATTERN,
    EventPropertyResponse,
    VariableBulkDelete,
    VariableBulkUpdate,
    VariableCreate,
    VariableEventOverrideUpsert,
    VariableUpdate,
)
from tripl.services import variable_retirement_service
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id
from tripl.services.search_service import reindex_project_branch
from tripl.services.variable_value_service import attach_variable_summaries

# Strict name for NEW names (create + actual renames). Legacy scan-created
# dotted names stay valid as long as they are not being changed.
_STRICT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# Page size used when a caller does not pass one; mirrors the router default so
# service-level callers get the same bounded read as HTTP clients.
DEFAULT_LIST_LIMIT = 200


async def _check_binding_conflicts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    bindings: list[str],
    exclude_variable_id: uuid.UUID | None = None,
    name: str | None = None,
) -> None:
    """409 when a token is already claimed by another variable in the branch.

    A binding conflicts when another variable carries it as its ``name``, in
    ``bindings`` or as its scan identity (``source_name``): scan adoption matches
    on all three, and the index hands a contested token to whichever variable
    sorts first, so a shared token makes attribution ambiguous.

    ``name``, when given, is a name being created or renamed to, and conflicts
    with another variable's ``bindings`` or ``source_name`` for the same reason.
    Its clash with another NAME is the caller's plain duplicate check. A
    variable renamed away keeps its ``source_name``, so without this a new
    variable could take that token and the scan's ``source_name`` backfill would
    then violate the unique key on every run.
    """
    if not bindings and name is None:
        return
    # ``lazyload`` because ``Variable.value_contexts`` is ``lazy="selectin"`` and
    # each context then selectin-loads its FieldDefinition: hydrating the
    # entities plainly pulls the project's whole context table into memory to
    # answer a question about NAMES, on a statement that runs on every create
    # and on every update that touches bindings. The same option
    # for the same reason as ``worker.tasks.metrics.catalog_sync``.
    #
    # One of TWO whole-project selects the same request makes: ``create_variable``
    # and ``update_variable`` both call ``reindex_project_branch`` a few lines
    # below, whose ``_search_documents`` pass selects every Variable of the branch
    # as well. That one now carries the same option, so the hydration is gone
    # rather than merely halved — the two belong together if either ever moves.
    result = await session.execute(
        select(Variable)
        .where(Variable.project_id == project_id, Variable.branch_id == branch_id)
        .options(lazyload(Variable.value_contexts))
    )
    wanted = set(bindings)
    for other in result.scalars().all():
        if exclude_variable_id is not None and other.id == exclude_variable_id:
            continue
        claimed = set(other.bindings or [])
        if other.source_name:
            claimed.add(other.source_name)
        clash = wanted & (claimed | {other.name})
        if clash:
            raise HTTPException(
                status_code=409,
                detail=f"Binding '{sorted(clash)[0]}' is already used by property '{other.name}'",
            )
        if name is not None and name in claimed:
            raise HTTPException(
                status_code=409,
                detail=f"Name '{name}' is already bound to property '{other.name}'",
            )


async def list_variables(
    session: AsyncSession,
    slug: str,
    branch_id: uuid.UUID | None = None,
    *,
    offset: int = 0,
    limit: int = DEFAULT_LIST_LIMIT,
    usage: str | None = None,
) -> tuple[list[Variable], int]:
    """One page of the project's variables plus the untruncated total.

    Paging bounds ``attach_variable_summaries`` too: it only ever fans out over
    the ids on the page, never the whole project.

    ``usage="unused"`` narrows the page to exactly the rows the retirement sweep
    would take, and ``"used"`` to its complement. It is answered by the shared
    predicate in ``core.variable_retirement`` rather than by an "``event_count``
    is zero" filter, and the difference is not cosmetic: a variable can have no
    observed context and still be named by a live event's field value — that is
    tripl-xfxa, eighteen rows on production. A cheap zero-count filter would
    have offered precisely those for deletion, from a screen that has a
    select-all checkbox on it.

    The predicate costs one pass over the project's stored field values, so it
    runs only when the filter is actually asked for.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    scope: list[ColumnElement[bool]] = [
        Variable.project_id == project_id,
        Variable.branch_id == branch_id,
    ]

    if usage in {"used", "unused"}:
        plan = await variable_retirement_service.plan_project_retirement(
            session, project_id=project_id, branch_id=branch_id
        )
        # ``in_(())`` and ``not_in(())`` are both well defined on an empty
        # sequence, so a project with nothing to retire needs no special case:
        # "unused" correctly returns no rows, "used" correctly returns all.
        #
        # This one IS a literal id list, unlike the anti-joins inside
        # ``plan_project_retirement`` which take a subquery: the retirable set
        # is computed in Python and has no SQL expression to stand in for it.
        # The bound is therefore PostgreSQL's 65535 bind parameters against the
        # project's variable count — comfortable now that the end-of-scan sweep
        # keeps that count from growing without limit, which is exactly the
        # property this filter exists to make visible.
        scope.append(
            Variable.id.in_(plan.retirable)
            if usage == "unused"
            else Variable.id.not_in(plan.retirable)
        )

    total = await session.scalar(select(func.count(Variable.id)).where(*scope)) or 0
    # ``lazyload`` for the reason ``_check_binding_conflicts`` gives, on the very
    # endpoint the retirement docstring above names as a request path. Nothing
    # downstream touches the collection: ``VariableResponse`` declares no
    # contexts field, and ``attach_variable_summaries`` re-reads what it needs
    # with its own ``select(VariableValue)`` rather than walking this
    # relationship. The frontend pins its page size to VARIABLES_PAGE_LIMIT =
    # 5000 and routes every list caller through it, so without the option one
    # page IS the whole-project select tripl-xkbb exists to remove.
    result = await session.execute(
        select(Variable)
        .where(*scope)
        .options(lazyload(Variable.value_contexts))
        .order_by(Variable.name)
        .offset(offset)
        .limit(limit)
    )
    variables = list(result.scalars().all())
    await attach_variable_summaries(session, variables)
    return variables, total


async def create_variable(
    session: AsyncSession,
    slug: str,
    data: VariableCreate,
    branch_id: uuid.UUID | None = None,
) -> Variable:
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    existing = await session.execute(
        select(Variable).where(
            Variable.project_id == project_id,
            Variable.branch_id == branch_id,
            Variable.name == data.name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Property with this name already exists")
    await _check_binding_conflicts(
        session,
        project_id=project_id,
        branch_id=branch_id,
        bindings=data.bindings,
        name=data.name,
    )
    var = Variable(**data.model_dump(), project_id=project_id, branch_id=branch_id)
    session.add(var)
    await session.commit()
    await session.refresh(var)
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    return var


async def rewrite_variable_token_references(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    old_name: str,
    new_name: str,
) -> None:
    """Re-point every stored ``${old_name}`` in this branch at ``${new_name}``.

    Renaming a variable is advertised as carrying through the values that
    reference it, so this rewrite has to travel with the name wherever the name
    moves. Two callers move it: ``update_variable`` below, and
    ``plan_branch_revert_service``'s rename-aware revert, which moves the name
    BACK. That second direction is why this is a module-level coroutine instead
    of a few lines inside the update: the revert wrote only ``renamed.name`` and
    left every value on the branch naming ``${new_name}``, a token no variable
    answers to, so ``event_service._attach_template_warnings`` stamped "Unknown
    variable token" on each affected event and a merge carried the broken
    templates to main — under a confirm dialog promising the variable's
    "documented values and history are untouched". One
    implementation for both directions, because two cannot drift.

    BOTH value tables, for the reason ``variable_retirement_service`` spells out
    on the read side: a ``${token}`` is legal in an event's field values and in
    its META values. Field values alone left the meta half holding a literal
    ``${old_name}`` naming nothing anybody can now find by the variable's name.

    No ``is_authored`` test on either pass, and none to add. The column lives
    only on ``EventFieldValue``, where TRUE marks a value a PERSON typed and a
    scan must therefore leave alone — ``core.analyzers.event_generator`` only
    overwrites a value whose flag is false. A rename is not a scan and replaces
    no value: it re-points a reference inside whatever value is already there,
    so an authored value must be carried across exactly like a harvested one,
    and skipping the authored rows is precisely how a hand-typed template would
    be left naming the old name. ``EventMetaValue`` carries no such column at
    all, because no scan writes that table.

    Scoped to one branch, like both callers: a branch's rename must not reach
    main's values.
    """
    old_ref = f"${{{old_name}}}"
    new_ref = f"${{{new_name}}}"

    fv_result = await session.execute(
        select(EventFieldValue)
        .join(Event, EventFieldValue.event_id == Event.id)
        .where(
            Event.project_id == project_id,
            Event.branch_id == branch_id,
            EventFieldValue.value.contains(old_ref),
        )
    )
    for fv in fv_result.scalars().all():
        fv.value = fv.value.replace(old_ref, new_ref)

    mv_result = await session.execute(
        select(EventMetaValue)
        .join(Event, EventMetaValue.event_id == Event.id)
        .where(
            Event.project_id == project_id,
            Event.branch_id == branch_id,
            EventMetaValue.value.contains(old_ref),
        )
    )
    for mv in mv_result.scalars().all():
        mv.value = mv.value.replace(old_ref, new_ref)


async def update_variable(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID,
    data: VariableUpdate,
    branch_id: uuid.UUID | None = None,
) -> tuple[Variable, str]:
    """Patch one variable, returning it and the name it had BEFORE the patch.

    The previous name is returned for the audit trail. A rename is not confined
    to this row — it rewrites ``${old}`` to ``${new}`` in every event field value
    on the branch — and the record carried only the new name, so the token that
    was replaced was not recoverable from the trail. Unchanged
    when the patch is not a rename, which is how the route tells the two apart.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    result = await session.execute(
        select(Variable).where(
            Variable.id == variable_id,
            Variable.project_id == project_id,
            Variable.branch_id == branch_id,
        )
    )
    var = result.scalar_one_or_none()
    if not var:
        raise HTTPException(status_code=404, detail="Property not found")
    previous_name = var.name
    update_data = data.model_dump(exclude_unset=True)
    # Only what the update ADDS is judged: the edit form resends the stored
    # bindings, which the scan may have written in a grammar the strict pattern
    # refuses, or which may predate a conflict rule.
    added_bindings: list[str] = []
    if "bindings" in update_data and update_data["bindings"] is not None:
        stored = set(var.bindings or [])
        added_bindings = [b for b in update_data["bindings"] if b not in stored]
        for binding in added_bindings:
            if not BINDING_PATTERN.match(binding):
                raise HTTPException(status_code=422, detail=f"Invalid binding path: {binding!r}")
    renamed_to = (
        update_data["name"] if "name" in update_data and update_data["name"] != var.name else None
    )
    if added_bindings or renamed_to is not None:
        await _check_binding_conflicts(
            session,
            project_id=project_id,
            branch_id=branch_id,
            bindings=added_bindings,
            exclude_variable_id=var.id,
            name=renamed_to,
        )
    # The two halves of the type are judged together, whichever one the patch
    # touches: a type change against the stored schema is as wrong as a schema
    # against the stored type.
    if "variable_type" in update_data or "json_schema" in update_data:
        try:
            check_schema_matches_type(
                update_data.get("variable_type", var.variable_type),
                update_data.get("json_schema", var.json_schema),
            )
        except PropertySchemaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    if "name" in update_data and update_data["name"] != var.name:
        if not _STRICT_NAME_PATTERN.match(update_data["name"]):
            raise HTTPException(
                status_code=422,
                detail="Property names must be lowercase letters, digits and underscores"
                " (bind data paths via 'bindings' instead of dotted names)",
            )
        dup = await session.execute(
            select(Variable).where(
                Variable.project_id == project_id,
                Variable.branch_id == branch_id,
                Variable.name == update_data["name"],
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Property with this name already exists")

        # Carry the name change through every stored ``${old_name}``. Called
        # HERE, before the ``setattr`` loop below writes the new name, because
        # the helper reads the branch's values against the name the row still
        # holds — the same ordering ``plan_branch_revert_service`` keeps when it
        # moves the name back.
        await rewrite_variable_token_references(
            session,
            project_id=project_id,
            branch_id=branch_id,
            old_name=var.name,
            new_name=update_data["name"],
        )

    # ``excluded_from_scans`` lands here like any other field, with no purge
    # beside it. Every scan-side guard asks the FLAG, never the rows —
    # ``_event_generator_variables.record_variable_contexts`` and
    # ``normalize_variable_tokens``, ``catalog_sync._unfilled_json_path_candidates``
    # and both replay accumulators in ``metrics.generation`` — so deleting the
    # observations bought no extra silence and cost the operator the only copy of
    # the history, under a button the UI advertises as reversible. It was also
    # the only setter of this flag that deleted anything: the retirement sweep,
    # branch merge and branch revert all carry the flag across untouched, so the
    # same variable could be excluded destructively or harmlessly depending on
    # which door it came through.
    #
    # Readers that must not ACT on an excluded variable filter on the flag too:
    # ``variable_value_service.attach_variable_summaries`` for the drift badge,
    # ``worker.tasks.metrics.signals`` for alert candidates.
    # New bindings point the property at other warehouse paths, so the scan's
    # one-off type check (``catalog_sync._type_backfill_candidates``) may run
    # again against them.
    if "bindings" in update_data and list(update_data["bindings"] or []) != list(
        var.bindings or []
    ):
        var.type_checked_at = None
    for key, value in update_data.items():
        setattr(var, key, value)
    await session.commit()
    await session.refresh(var)
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    return var, previous_name


async def delete_variable(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> str:
    """Delete one branch-scoped variable and return the name it had.

    Returning the name lets the caller write its audit record without a second
    lookup — the indexed (id, project_id, branch_id) read below is the only one
    the delete path needs.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    var = await _get_variable_in_branch(session, project_id, branch_id, variable_id)
    name = var.name
    await session.delete(var)
    await session.commit()
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    return name


async def clear_variable_values(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
    context_id: uuid.UUID | None = None,
) -> tuple[str, int]:
    """Drop this variable's observed contexts, keeping the variable itself.

    The reset that used to require deleting the whole row. Everything a person
    put on the variable survives — description, documented values, bindings,
    per-event overrides, the exclusion tombstone — and so does every drift
    verdict, which is triage no scan can rebuild. Nothing is orphaned by this:
    no table in the schema carries a foreign key into ``variable_values``, and
    drift detection reads the run's own in-memory contexts rather than these
    rows.

    **Repopulation is not a promise this makes.** A scan re-records a context
    only where a stored field value still names ``${token}``, so a context
    whose event has moved on is gone for good.

    **It can also cost the variable its place.** ``plan_retirement`` keeps a
    row it finds in ``VariableValue`` (``KeptReason.OBSERVED``); clear the
    contexts and that arm stops firing, so a scan-minted variable with no
    human claim, no drift, no override and no surviving ``${token}`` becomes
    retirable and the next sweep deletes it. The narrow case where that bites
    — contexts recorded but the token no longer stored anywhere — is exactly
    the fossil the sweep exists to collect, so the behaviour is right; the UI
    says it out loud rather than surprising anyone.

    Returns the variable's name for the audit record and the number of rows
    removed.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    var = await _get_variable_in_branch(session, project_id, branch_id, variable_id)
    name = var.name
    stmt = delete(VariableValue).where(
        VariableValue.project_id == project_id,
        VariableValue.branch_id == branch_id,
        VariableValue.variable_id == variable_id,
    )
    if context_id is not None:
        stmt = stmt.where(VariableValue.id == context_id)
    result = await session.execute(stmt)
    # ``Result`` only declares ``rowcount`` on the cursor subclass; the same
    # getattr the detection reset uses, for the same reason.
    removed = int(getattr(result, "rowcount", 0) or 0)
    await session.commit()
    # The event search document folds each context's source_column, value_kind,
    # observed_count and values into its body, so a clear that skipped this
    # would leave deleted values searchable.
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    return name, removed


async def _load_variables_by_ids(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    variable_ids: list[uuid.UUID],
) -> list[Variable]:
    result = await session.execute(
        select(Variable).where(
            Variable.project_id == project_id,
            Variable.branch_id == branch_id,
            Variable.id.in_(variable_ids),
        )
    )
    variables = list(result.scalars().all())
    missing = set(variable_ids) - {variable.id for variable in variables}
    if missing:
        raise HTTPException(status_code=404, detail="Property not found")
    return variables


async def bulk_update_variables(
    session: AsyncSession,
    slug: str,
    data: VariableBulkUpdate,
    branch_id: uuid.UUID | None = None,
) -> None:
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    variables = await _load_variables_by_ids(session, project_id, branch_id, data.variable_ids)
    if data.variable_type is not None:
        # All or nothing: a bulk type change never leaves some rows retyped and
        # the rest refused, and never drops a schema a person wrote.
        mismatched = []
        for variable in variables:
            try:
                check_schema_matches_type(data.variable_type, variable.json_schema)
            except PropertySchemaError:
                mismatched.append(variable.name)
        if mismatched:
            raise HTTPException(
                status_code=422,
                detail=f"variable_type {data.variable_type.value} does not match the json_schema"
                f" of: {', '.join(sorted(mismatched))}. Change or clear their schemas first.",
            )
    for variable in variables:
        if data.variable_type is not None:
            variable.variable_type = data.variable_type
        if data.description is not None:
            variable.description = data.description
        if data.allowed_values_add or data.allowed_values_remove:
            values = list(variable.allowed_values or [])
            if data.allowed_values_remove:
                removed = set(data.allowed_values_remove)
                values = [value for value in values if value not in removed]
            if data.allowed_values_add:
                seen = set(values)
                for value in data.allowed_values_add:
                    if value not in seen:
                        seen.add(value)
                        values.append(value)
            variable.allowed_values = values
    await session.commit()
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)


async def bulk_delete_variables(
    session: AsyncSession,
    slug: str,
    data: VariableBulkDelete,
    branch_id: uuid.UUID | None = None,
) -> list[tuple[uuid.UUID, str]]:
    """Delete the named variables, returning the (id, name) of each one deleted.

    The return value is for the audit trail, and it is what the REQUEST said only
    by coincidence: ids that name no variable on this branch are silently skipped
    by ``_load_variables_by_ids``, so recording the request body would file a
    delete of rows that were never there.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    variables = await _load_variables_by_ids(session, project_id, branch_id, data.variable_ids)
    deleted = [(variable.id, variable.name) for variable in variables]
    for variable in variables:
        await session.delete(variable)
    await session.commit()
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    return deleted


async def _get_variable_in_branch(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    variable_id: uuid.UUID,
) -> Variable:
    result = await session.execute(
        select(Variable).where(
            Variable.id == variable_id,
            Variable.project_id == project_id,
            Variable.branch_id == branch_id,
        )
    )
    var = result.scalar_one_or_none()
    if not var:
        raise HTTPException(status_code=404, detail="Property not found")
    return var


async def list_event_overrides(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> list[VariableEventValueOverride]:
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    await _get_variable_in_branch(session, project_id, branch_id, variable_id)
    result = await session.execute(
        select(VariableEventValueOverride)
        .where(
            VariableEventValueOverride.project_id == project_id,
            VariableEventValueOverride.branch_id == branch_id,
            VariableEventValueOverride.variable_id == variable_id,
        )
        .order_by(VariableEventValueOverride.created_at)
    )
    return list(result.scalars().all())


async def upsert_event_override(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID,
    event_id: uuid.UUID,
    data: VariableEventOverrideUpsert,
    branch_id: uuid.UUID | None = None,
) -> tuple[VariableEventValueOverride, str]:
    """Upsert one event override, returning it and the VARIABLE's own name.

    The name is returned because the audit row this feeds has
    ``target_type="variable"``: it used to file the EVENT's name against a
    variable target, so a reader could not tell which variable was overridden.
    The event name belongs in the payload, not the target.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    variable = await _get_variable_in_branch(session, project_id, branch_id, variable_id)
    event = await session.execute(
        select(Event).where(
            Event.id == event_id,
            Event.project_id == project_id,
            Event.branch_id == branch_id,
        )
    )
    if not event.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Event not found")
    existing = await session.execute(
        select(VariableEventValueOverride).where(
            VariableEventValueOverride.variable_id == variable_id,
            VariableEventValueOverride.event_id == event_id,
        )
    )
    override = existing.scalar_one_or_none()
    patch = data.model_dump(exclude_unset=True)
    if override is None:
        override = VariableEventValueOverride(
            project_id=project_id,
            branch_id=branch_id,
            variable_id=variable_id,
            event_id=event_id,
            values=None,
            required=False,
        )
        session.add(override)
    if "values" in patch:
        override.values = copy_override_values(patch["values"])
    if "required" in patch:
        override.required = patch["required"]
    await session.commit()
    await session.refresh(override)
    return override, variable.name


async def delete_event_override(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID,
    event_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> tuple[str, str]:
    """Delete one event override, returning the (variable name, event name).

    Both are read BEFORE the delete: after the commit the row is expired and its
    ``event`` relationship is no longer loadable on an async session. The audit
    row this feeds had an EMPTY ``target_name`` and named neither.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    variable = await _get_variable_in_branch(session, project_id, branch_id, variable_id)
    result = await session.execute(
        select(VariableEventValueOverride).where(
            VariableEventValueOverride.variable_id == variable_id,
            VariableEventValueOverride.event_id == event_id,
        )
    )
    override = result.scalar_one_or_none()
    if not override:
        raise HTTPException(status_code=404, detail="Override not found")
    names = (variable.name, override.event_name)
    await session.delete(override)
    await session.commit()
    return names


async def list_event_properties(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> list[EventPropertyResponse]:
    """The event's property list: every variable with an entry for it, by name."""
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    found = (
        await session.execute(
            select(Event.id, Event.required_presence_threshold).where(
                Event.id == event_id,
                Event.project_id == project_id,
                Event.branch_id == branch_id,
            )
        )
    ).first()
    if found is None:
        raise HTTPException(status_code=404, detail="Event not found")
    threshold = effective_threshold(found.required_presence_threshold)
    rows = (
        await session.execute(
            select(VariableEventValueOverride, Variable)
            .join(Variable, Variable.id == VariableEventValueOverride.variable_id)
            .where(
                VariableEventValueOverride.branch_id == branch_id,
                VariableEventValueOverride.event_id == event_id,
            )
            .order_by(Variable.name)
        )
    ).all()
    presence: dict[uuid.UUID, float] = {}
    for variable_id, rate in await session.execute(
        select(VariableValue.variable_id, func.max(VariableValue.presence_rate))
        .where(
            VariableValue.branch_id == branch_id,
            VariableValue.event_id == event_id,
            VariableValue.presence_rate.is_not(None),
        )
        .group_by(VariableValue.variable_id)
    ):
        presence[variable_id] = rate
    return [
        EventPropertyResponse(
            id=entry.id,
            variable_id=variable.id,
            name=variable.name,
            variable_type=variable.variable_type,
            json_schema=variable.json_schema,
            description=variable.description,
            required=entry.required,
            values=copy_override_values(entry.values),
            effective_values=list(
                entry.values if entry.values is not None else variable.allowed_values or []
            ),
            presence_rate=presence.get(variable.id),
            suggested_required=(
                None if variable.id not in presence else presence[variable.id] >= threshold
            ),
        )
        for entry, variable in rows
    ]
