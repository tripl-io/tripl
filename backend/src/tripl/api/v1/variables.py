import re
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.routing import APIRoute

from tripl.api.deps import BranchIdDep, EditorUserDep, SessionDep
from tripl.api.v1.events import bulk_event_audit_payload
from tripl.models.property_drift import PropertyDriftKind
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue
from tripl.schemas.pagination import Offset
from tripl.schemas.property_drift import (
    PropertyDriftActionRequest,
    PropertyDriftListResponse,
    PropertyDriftResponse,
)
from tripl.schemas.property_events import (
    PropertyEventResponse,
    PropertyEventsBulkDelete,
    PropertyEventsBulkResult,
    PropertyEventsBulkUpsert,
)
from tripl.schemas.variable import (
    VariableBulkDelete,
    VariableBulkUpdate,
    VariableCreate,
    VariableEventOverrideResponse,
    VariableEventOverrideUpsert,
    VariableListResponse,
    VariableResponse,
    VariableUpdate,
    VariableValueContextResponse,
)
from tripl.schemas.variable_value_drift import (
    VariableValueDriftActionRequest,
    VariableValueDriftListResponse,
    VariableValueDriftResponse,
)
from tripl.services import (
    audit_service,
    property_drift_service,
    property_events_service,
    variable_service,
    variable_value_drift_service,
    variable_value_service,
)

router = APIRouter(prefix="/projects/{slug}/variables", tags=["variables"])

DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 5000

# How many ids/names one bulk audit row carries before it is a sample. The same
# ceiling as ``events._BULK_SAMPLE`` and ``metrics_catalog._BULK_SAMPLE``, for the
# same reason: ``payload`` is an uncapped JSON column, nobody reads the 201st id
# off a compliance row, and a bulk delete here has no upper bound either. The
# payload shape below is ``events.bulk_event_audit_payload``'s, spelled locally
# because that helper's fixed keys are named ``event_ids``/``event_names`` and
# filing variables under them would be worse than the duplication.
_BULK_SAMPLE = 200


def _bulk_variable_audit_payload(deleted: list[tuple[uuid.UUID, str]]) -> dict[str, object]:
    """One bulk delete's payload: the true count plus a sample of ids and names.

    ``count`` is the real number of variables deleted, so a reader can tell a
    3-variable delete from a 3000-variable one even when the lists are cut off.
    The row used to carry the REQUEST's raw id list with no names and no count —
    an irreversible delete whose trail named nothing a human recognises, and
    which listed ids that may not have resolved to a variable at all.
    """
    return {
        "count": len(deleted),
        "variable_ids": [str(variable_id) for variable_id, _ in deleted[:_BULK_SAMPLE]],
        "variable_names": [name for _, name in deleted[:_BULK_SAMPLE]],
        "truncated": len(deleted) > _BULK_SAMPLE,
    }


@router.post("/bulk-update", status_code=204)
async def bulk_update_variables(
    session: SessionDep,
    slug: str,
    data: VariableBulkUpdate,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> None:
    await variable_service.bulk_update_variables(session, slug, data, branch_id)
    await audit_service.record(
        session,
        user=current_user,
        action="variable.bulk_update",
        target_type="variable",
        target_id=None,
        project_slug=slug,
        payload=data.model_dump(mode="json", exclude_none=True),
    )


@router.post("/bulk-delete", status_code=204)
async def bulk_delete_variables(
    session: SessionDep,
    slug: str,
    data: VariableBulkDelete,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> None:
    deleted = await variable_service.bulk_delete_variables(session, slug, data, branch_id)
    await audit_service.record(
        session,
        user=current_user,
        action="variable.bulk_delete",
        target_type="variable",
        target_id=None,
        project_slug=slug,
        payload=_bulk_variable_audit_payload(deleted),
    )


# Registered before the /{variable_id} routes, like "drifts" below.
@router.get("/property-drifts", response_model=PropertyDriftListResponse)
async def list_property_drifts(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    kind: PropertyDriftKind | None = None,
    active_only: bool = False,
) -> PropertyDriftListResponse:
    """New, missing-required and type-changed properties a scan saw (F23)."""
    return await property_drift_service.list_property_drifts(
        session,
        slug,
        variable_id=variable_id,
        event_id=event_id,
        kind=kind,
        active_only=active_only,
    )


# No ``BranchIdDep``, for the reason ``apply_value_drift_action`` gives: the
# drift is detected on main and accepting it writes to main.
@router.post("/property-drifts/{drift_id}/action", response_model=PropertyDriftResponse)
async def apply_property_drift_action(
    session: SessionDep,
    slug: str,
    drift_id: uuid.UUID,
    data: PropertyDriftActionRequest,
    current_user: EditorUserDep,
) -> PropertyDriftResponse:
    result = await property_drift_service.apply_property_drift_action(
        session, slug, drift_id, data, current_user
    )
    await audit_service.record(
        session,
        user=current_user,
        action="variable.property_drift_action",
        target_type="variable",
        target_id=result.variable_id,
        target_name=result.variable_name,
        project_slug=slug,
        payload={
            **data.model_dump(mode="json", exclude_none=True),
            "kind": result.kind.value,
            **({"event_name": result.event_name} if result.event_name else {}),
        },
    )
    return result


# Registered before the /{variable_id} routes so the literal "drifts" segment
# never gets parsed as a variable id.
@router.get("/drifts", response_model=VariableValueDriftListResponse)
async def list_value_drifts(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
) -> VariableValueDriftListResponse:
    return await variable_value_drift_service.list_value_drifts(
        session, slug, variable_id, event_id
    )


# The one audited handler in this module with no ``BranchIdDep``, and
# deliberately so. Value drifts are only ever detected against main
# (event_generator resolves ``main_branch_id`` and passes it to
# ``detect_variable_value_drifts``), and ``apply_drift_action`` writes to
# ``variable.branch_id`` — main — whatever the request is scoped to. Declaring
# the dependency would bind the caller's branch and stamp the audit row with a
# branch the write never touched, which is the misattribution the audit
# chip exists to end. No chip is the correct rendering here: the write is on main.
@router.post("/drifts/{drift_id}/action", response_model=VariableValueDriftResponse)
async def apply_value_drift_action(
    session: SessionDep,
    slug: str,
    drift_id: uuid.UUID,
    data: VariableValueDriftActionRequest,
    current_user: EditorUserDep,
) -> VariableValueDriftResponse:
    result = await variable_value_drift_service.apply_drift_action(
        session, slug, drift_id, data, current_user
    )
    await audit_service.record(
        session,
        user=current_user,
        action="variable.drift_action",
        target_type="variable",
        target_id=result.variable_id,
        target_name=result.variable_name,
        project_slug=slug,
        payload=data.model_dump(mode="json", exclude_none=True),
    )
    return result


@router.get("", response_model=VariableListResponse)
async def list_variables(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    offset: Offset = 0,
    # Ceiling sized above the largest known project's variable count so a
    # single-page fetch stays possible; the default keeps unaware clients off
    # the multi-hundred-KB payload.
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    usage: Literal["all", "used", "unused"] = Query(
        "all",
        description=(
            "Narrow to the variables nothing refers to ('unused' — exactly the "
            "set the retirement sweep would take) or to their complement "
            "('used'). Declared as an enum rather than a free string so an "
            "unknown value is a 422 and not a 500."
        ),
    ),
) -> VariableListResponse:
    items, total = await variable_service.list_variables(
        session, slug, branch_id, offset=offset, limit=limit, usage=usage
    )
    return VariableListResponse(
        items=[VariableResponse.model_validate(item) for item in items],
        total=total,
    )


@router.post("", response_model=VariableResponse, status_code=201)
async def create_variable(
    session: SessionDep,
    slug: str,
    data: VariableCreate,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> Variable:
    v = await variable_service.create_variable(session, slug, data, branch_id)
    await audit_service.record(
        session,
        user=current_user,
        action="variable.create",
        target_type="variable",
        target_id=v.id,
        target_name=v.name,
        project_slug=slug,
        payload=data.model_dump(),
    )
    return v


@router.get("/{variable_id}/values", response_model=list[VariableValueContextResponse])
async def list_variable_values(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID,
    branch_id: BranchIdDep,
) -> list[VariableValue]:
    return await variable_value_service.list_variable_values(session, slug, variable_id, branch_id)


@router.delete("/{variable_id}/values", status_code=204)
async def clear_variable_values(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
    context_id: Annotated[
        uuid.UUID | None,
        Query(
            description=(
                "Clear one context row instead of all of them. The id is the `id` on"
                " VariableValueContextResponse — the same value /values already returns,"
                " so a client can scope the clear to a single (event, field)."
            ),
        ),
    ] = None,
) -> None:
    """Drop the variable's observed contexts and keep the variable.

    Deleting the variable was the only reset available and it takes the
    description, documented values, bindings, overrides and drift triage with
    it — none of which a scan rebuilds.
    """
    name, removed = await variable_service.clear_variable_values(
        session, slug, variable_id, branch_id, context_id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="variable.values_clear",
        target_type="variable",
        target_id=variable_id,
        target_name=name,
        project_slug=slug,
        payload={
            "removed": removed,
            **({"context_id": str(context_id)} if context_id else {}),
        },
    )


@router.get("/{variable_id}/event-overrides", response_model=list[VariableEventOverrideResponse])
async def list_event_overrides(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID,
    branch_id: BranchIdDep,
) -> list[VariableEventValueOverride]:
    return await variable_service.list_event_overrides(session, slug, variable_id, branch_id)


@router.get("/{variable_id}/events", response_model=list[PropertyEventResponse])
async def list_property_events(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID,
    branch_id: BranchIdDep,
) -> list[PropertyEventResponse]:
    """The events whose property list carries this property, with each entry's
    required flag, override and last measured presence (F23.8)."""
    return await property_events_service.list_property_events(session, slug, variable_id, branch_id)


# Registered before "/{variable_id}/event-overrides/{event_id}" so "bulk" is
# not read as an event id.
@router.post("/{variable_id}/event-overrides/bulk", response_model=PropertyEventsBulkResult)
async def bulk_upsert_event_overrides(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID,
    data: PropertyEventsBulkUpsert,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> PropertyEventsBulkResult:
    """Add the property to many events' lists, or apply one patch to each entry:
    the single PUT's semantics, all or nothing (F23.8)."""
    variable_name, touched, result = await property_events_service.bulk_upsert_property_events(
        session, slug, variable_id, data, branch_id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="variable.override_bulk_set",
        target_type="variable",
        target_id=variable_id,
        target_name=variable_name,
        project_slug=slug,
        payload=bulk_event_audit_payload(
            [event_id for event_id, _ in touched],
            event_names=[name for _, name in touched],
            extra={
                **data.model_dump(mode="json", exclude_unset=True, exclude={"event_ids"}),
                "created": result.created,
            },
        ),
    )
    return result


@router.post("/{variable_id}/event-overrides/bulk-delete", response_model=PropertyEventsBulkResult)
async def bulk_delete_event_overrides(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID,
    data: PropertyEventsBulkDelete,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> PropertyEventsBulkResult:
    """Take the property off many events' lists (F23.8). Events that do not
    carry it are skipped, and the audit row names only the entries removed."""
    variable_name, removed, result = await property_events_service.bulk_delete_property_events(
        session, slug, variable_id, data, branch_id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="variable.override_bulk_delete",
        target_type="variable",
        target_id=variable_id,
        target_name=variable_name,
        project_slug=slug,
        payload=bulk_event_audit_payload(
            [event_id for event_id, _ in removed],
            event_names=[name for _, name in removed],
        ),
    )
    return result


@router.put(
    "/{variable_id}/event-overrides/{event_id}",
    response_model=VariableEventOverrideResponse,
)
async def upsert_event_override(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID,
    event_id: uuid.UUID,
    data: VariableEventOverrideUpsert,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> VariableEventValueOverride:
    override, variable_name = await variable_service.upsert_event_override(
        session, slug, variable_id, event_id, data, branch_id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="variable.override_set",
        target_type="variable",
        target_id=variable_id,
        # The VARIABLE's name: the target is a variable, and filing the event's
        # name here made the row read as though the event were the thing changed.
        # The event is in the payload, where it belongs.
        target_name=variable_name,
        project_slug=slug,
        payload={
            "event_id": str(event_id),
            "event_name": override.event_name,
            **data.model_dump(exclude_unset=True),
        },
    )
    return override


@router.delete("/{variable_id}/event-overrides/{event_id}", status_code=204)
async def delete_event_override(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID,
    event_id: uuid.UUID,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> None:
    variable_name, event_name = await variable_service.delete_event_override(
        session, slug, variable_id, event_id, branch_id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="variable.override_delete",
        target_type="variable",
        target_id=variable_id,
        # Was filed with no ``target_name`` at all, so the trail read as an
        # anonymous delete.
        target_name=variable_name,
        project_slug=slug,
        payload={"event_id": str(event_id), "event_name": event_name},
    )


@router.patch("/{variable_id}", response_model=VariableResponse)
async def update_variable(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID,
    data: VariableUpdate,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> Variable:
    v, previous_name = await variable_service.update_variable(
        session, slug, variable_id, data, branch_id
    )
    # The raw patch body is an honest payload for every field but one, and that
    # one now carries its own key. A ``name`` change is not confined to this row:
    # ``update_variable`` also rewrites ``${old}`` to ``${new}`` in every event
    # field value on the branch, and this record used to carry only the NEW name
    # — target_name too — so the token that was replaced was not recoverable
    # from the trail. ``previous_name`` is added only on an actual rename, so an
    # ordinary patch is unchanged and its presence is itself the signal that the
    # branch-wide rewrite ran.
    #
    # That was the survivor of a larger miss. While excluding a variable also
    # deleted every observed context and drift row for it, a record reading
    # ``{"excluded_from_scans": true}`` under a generic "variable.update" was
    # the only trace of an irreversible bulk delete; the delete is gone, and now
    # so is the rename gap. Anything else added to ``update_variable`` that
    # touches rows this body does not name needs its own action or its own
    # payload key.
    payload = data.model_dump(exclude_unset=True)
    if previous_name != v.name:
        payload["previous_name"] = previous_name
    await audit_service.record(
        session,
        user=current_user,
        action="variable.update",
        target_type="variable",
        target_id=v.id,
        target_name=v.name,
        project_slug=slug,
        payload=payload,
    )
    return v


@router.delete("/{variable_id}", status_code=204)
async def delete_variable(
    session: SessionDep,
    slug: str,
    variable_id: uuid.UUID,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> None:
    # The service does the branch-scoped indexed lookup, raises the 404 when the
    # variable is missing, and hands back the name for the audit record.
    name = await variable_service.delete_variable(session, slug, variable_id, branch_id)
    await audit_service.record(
        session,
        user=current_user,
        action="variable.delete",
        target_type="variable",
        target_id=variable_id,
        target_name=name,
        project_slug=slug,
    )


_VARIABLE_WORD = re.compile(r"([Vv])ariable(s?)")


def _in_property_words(text: str) -> str:
    """``text`` with the product's word: "variable(s)" becomes "property" / "properties"."""
    return _VARIABLE_WORD.sub(
        lambda match: ("P" if match[1] == "V" else "p") + ("roperties" if match[2] else "roperty"),
        text,
    )


def _properties_router() -> APIRouter:
    """Every route above again under ``/properties``, the name the product uses (F23).

    A property IS a variable (owner decision 1); only the word changed. The
    ``/variables`` paths are marked deprecated in the OpenAPI document and stay
    until the oldest supported CLI and MCP server call ``/properties``, so an
    older client or an agent pinned to them keeps working.

    Each copy is named in the product's word too (``list_variables`` becomes
    ``list_properties``), so the reference shows "List Properties" and the
    operation id has the same shape as every other route's.
    """
    aliased = APIRouter()
    for route in list(router.routes):
        assert isinstance(route, APIRoute)
        aliased.add_api_route(
            route.path.replace("/projects/{slug}/variables", "/projects/{slug}/properties", 1),
            route.endpoint,
            methods=sorted(route.methods or ()),
            response_model=route.response_model,
            status_code=route.status_code,
            summary=None if route.summary is None else _in_property_words(route.summary),
            description=route.description,
            response_description=route.response_description,
            name=_in_property_words(route.name),
            tags=["properties"],
        )
        route.deprecated = True
    return aliased


properties_router = _properties_router()
