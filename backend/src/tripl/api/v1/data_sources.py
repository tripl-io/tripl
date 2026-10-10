import uuid

from fastapi import APIRouter, Depends, Query

from tripl.api.deps import (
    CurrentUserDep,
    OwnerUserDep,
    SessionDep,
    get_editor_user,
    get_owner_user,
    refuse_on_public_demo,
)
from tripl.schemas.data_source import (
    ConnectionSettingsResponse,
    DataSourceConnectionTest,
    DataSourceConnectionTestResponse,
    DataSourceCreate,
    DataSourceResponse,
    DataSourceStatsResponse,
    DataSourceTestResponse,
    DataSourceUpdate,
)
from tripl.schemas.data_source_schema import DataSourceSchemaResponse
from tripl.services import (
    audit_service,
    datasource_schema_service,
    datasource_service,
    metrics_service,
    project_access,
)
from tripl.services.project_access import member_project_ids

router = APIRouter(
    prefix="/data-sources",
    tags=["data-sources"],
)
_owner_required = [Depends(get_owner_user)]
# Reading the warehouse's table and column names is an AUTHORING capability, so
# it stops at editor rather than at "any authenticated user". Three editor
# surfaces genuinely need it — the scan form, the metric form and the fact-table
# form all drive column pickers off it — but a viewer edits none of those, and
# the response is a map of the customer's warehouse. That made it
# a wider disclosure than the host/port this router already redacts, and it is
# reachable by anyone who can register once the instance is in "open" mode.
# Project-owned sources also require membership with edit rights in their owning
# project (a non-member gets 404, as on every other surface of that project);
# workspace-global sources remain available to all editors.
_editor_required = [Depends(get_editor_user)]

# A visitor to a public demo explores the synthetic source the demo generator
# adds; connecting to anything else is the host's network, not theirs.
_own_warehouses_refused = Depends(refuse_on_public_demo("connect to warehouses of your own"))


# What a non-admin legitimately needs from a data source, and nothing else.
#
# Data sources are org-owned and managed by the organization's owners and admins
# (create / update / delete / test are all org owner/admin-only), but the READ
# side was open to every authenticated user — viewers
# included — and handed out the full warehouse connection: host, port, database,
# username, whether a password is stored, TLS material and the last connection
# error. That is an internal network map plus a credential
# inventory, and nothing outside the org-admin Data Sources settings form
# consumes it.
#
# What the rest of the product actually reads off a data source is the *identity*
# of the warehouse a scan or metric points at:
#   id            — the join key from ScanConfig.data_source_id / MetricDefinition
#   project_id    — lets project surfaces hide sources owned by other projects
#                   (sources bound to a project the caller is not a member of
#                   never reach the response at all)
#   name          — the label in the scan/metric pickers and cards
#   db_type       — selects the SQL dialect for the measure/filter editors
#   is_synthetic  — badges a demo's in-memory warehouse
#   last_test_*   — the health pill (status/timestamp only, never the message,
#                   which quotes hosts, ports and driver errors verbatim)
#   created_at / updated_at — ordering and "last changed" labels
#
# Everything else is blanked for everyone who is not an owner or admin of the
# request's organization — a platform admin included, since the operator flag
# grants nothing inside an organization. The response *schema* is unchanged
# on purpose so this is a pure authorization narrowing: no OpenAPI/TS churn, and
# a client that reads a redacted field gets an empty value rather than a crash.
def _redact_connection(ds: DataSourceResponse) -> DataSourceResponse:
    """Strip warehouse connection metadata from a data source for non-admins."""
    return ds.model_copy(
        update={
            "host": "",
            "port": 0,
            "database_name": "",
            "username": "",
            "password_set": False,
            "timeout_seconds": None,
            "json_path_discovery": None,
            "connection_settings": ConnectionSettingsResponse(),
            "last_test_message": None,
        }
    )


def _visible_to(ds: DataSourceResponse, is_admin: bool) -> DataSourceResponse:
    """``ds`` unredacted for an org owner/admin of the bound organization, else redacted.

    ``is_admin`` is computed once per request
    (:func:`tripl.services.project_access.is_org_admin`).
    """
    if is_admin:
        return ds
    return _redact_connection(ds)


@router.get("", response_model=list[DataSourceResponse])
async def list_data_sources(
    session: SessionDep, current_user: CurrentUserDep
) -> list[DataSourceResponse]:
    sources = await datasource_service.list_data_sources(
        session, visible_project_ids=await member_project_ids(session, current_user)
    )
    is_admin = await project_access.is_org_admin(session, current_user)
    return [_visible_to(ds, is_admin) for ds in sources]


@router.post(
    "",
    response_model=DataSourceResponse,
    status_code=201,
    dependencies=[_own_warehouses_refused],
)
async def create_data_source(
    session: SessionDep,
    data: DataSourceCreate,
    current_user: OwnerUserDep,
) -> DataSourceResponse:
    ds = await datasource_service.create_data_source(session, data)
    await audit_service.record(
        session,
        user=current_user,
        action="data_source.create",
        target_type="data_source",
        target_id=ds.id,
        target_name=ds.name,
        payload=data.model_dump(),
    )
    return ds


@router.post(
    "/test",
    response_model=DataSourceConnectionTestResponse,
    dependencies=[*_owner_required, _own_warehouses_refused],
)
async def test_unsaved_data_source_connection(
    data: DataSourceConnectionTest,
) -> DataSourceConnectionTestResponse:
    """Test a connection before it is saved.

    The create gate (org owner/admin, browser session) and the create body's validation,
    host format included; nothing is stored and no stored secret is read.

    A config Create would refuse is a 422, as it is on Create: invalid
    ``connection_settings``, a missing Databricks ``http_path`` or Snowflake
    ``warehouse``, an empty user name (Trino, Snowflake, Athena, and Databricks
    with ``oauth_m2m`` sign-in), a Trino password over ``http``, or the
    ``synthetic`` type. A valid config is always 200, with ``success`` false when
    the warehouse cannot be reached or refuses the sign-in: that is the answer the
    caller asked for.
    """
    return await datasource_service.test_unsaved_connection(data)


@router.get("/{ds_id}", response_model=DataSourceResponse)
async def get_data_source(
    session: SessionDep, ds_id: uuid.UUID, current_user: CurrentUserDep
) -> DataSourceResponse:
    ds = await datasource_service.get_data_source(
        session, ds_id, visible_project_ids=await member_project_ids(session, current_user)
    )
    return _visible_to(ds, await project_access.is_org_admin(session, current_user))


# Org owner/admin-only: nothing in the product reads this. No frontend caller
# exists (`dataSourcesApi` has no stats method) and no MCP tool exposes it, so it is a
# pure diagnostic surface — there is no role below org admin whose workflow
# it would break, and it reports per-source ingestion volume.
@router.get("/{ds_id}/stats", response_model=DataSourceStatsResponse, dependencies=_owner_required)
async def get_data_source_stats(
    session: SessionDep, ds_id: uuid.UUID, window_hours: int = Query(48, ge=1, le=720)
) -> DataSourceStatsResponse:
    return await metrics_service.get_data_source_stats(session, ds_id, window_hours=window_hours)


@router.get(
    "/{ds_id}/schema", response_model=DataSourceSchemaResponse, dependencies=_editor_required
)
async def get_data_source_schema(
    session: SessionDep, ds_id: uuid.UUID, current_user: CurrentUserDep
) -> DataSourceSchemaResponse:
    await datasource_schema_service.authorize_schema_access(session, ds_id, current_user)
    return await datasource_schema_service.get_schema_tables(session, ds_id)


@router.patch("/{ds_id}", response_model=DataSourceResponse, dependencies=[_own_warehouses_refused])
async def update_data_source(
    session: SessionDep,
    ds_id: uuid.UUID,
    data: DataSourceUpdate,
    current_user: OwnerUserDep,
) -> DataSourceResponse:
    ds = await datasource_service.update_data_source(session, ds_id, data)
    await audit_service.record(
        session,
        user=current_user,
        action="data_source.update",
        target_type="data_source",
        target_id=ds.id,
        target_name=ds.name,
        payload=data.model_dump(exclude_unset=True),
    )
    return ds


@router.delete("/{ds_id}", status_code=204)
async def delete_data_source(
    session: SessionDep,
    ds_id: uuid.UUID,
    current_user: OwnerUserDep,
) -> None:
    # Org owner/admin-only route: the admin sees every source.
    existing = await datasource_service.get_data_source(session, ds_id, visible_project_ids=None)
    name = existing.name
    await datasource_service.delete_data_source(session, ds_id)
    await audit_service.record(
        session,
        user=current_user,
        action="data_source.delete",
        target_type="data_source",
        target_id=ds_id,
        target_name=name,
    )


@router.post(
    "/{ds_id}/test",
    response_model=DataSourceTestResponse,
    dependencies=_owner_required,
)
async def test_data_source_connection(
    session: SessionDep, ds_id: uuid.UUID
) -> DataSourceTestResponse:
    return await datasource_service.test_data_source_connection(session, ds_id)
