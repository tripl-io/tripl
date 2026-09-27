import asyncio
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.data_source import DataSource
from tripl.models.user import User
from tripl.schemas.data_source_schema import (
    ColumnSchema,
    DataSourceSchemaResponse,
    TableSchema,
)
from tripl.services import project_access
from tripl.services.data_source_scope import scanning_project_ids_for
from tripl.services.datasource_service import _fetch_data_source

logger = logging.getLogger(__name__)


async def authorize_schema_access(session: AsyncSession, ds_id: uuid.UUID, user: User) -> None:
    """A catalog takes an editing role in a project that may use the source.

    Another organization's source, or one bound to a project the caller is not a
    member of, does not exist for them: the same 404 an unknown id gets. A
    viewer member is refused with 403, the answer the project's own mutation
    gate gives.

    An organization-wide source (no ``project_id``) takes org owner/admin, or an
    editing role in a project the source is in scope for
    (``data_source_scope``): a project that scans it, or — when nobody scans it
    — any project. Plain org membership is not enough: a member who only views
    projects must not open a live connection to the warehouse and read its
    catalog.
    """
    source = await _fetch_data_source(session, ds_id)
    if source.project_id is None:
        if await project_access.is_org_admin(session, user, source.organization_id):
            return
        scanning = await scanning_project_ids_for(session, source)
        candidates = scanning or await project_access.member_project_ids(
            session, user, source.organization_id
        )
        roles = await project_access.member_roles(session, user, candidates)
        if not any(project_access.can_edit(role) for role in roles.values()):
            raise HTTPException(status_code=403, detail="No access to this data source's schema")
        return
    role = await project_access.member_role(session, user, source.project_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Data source not found")
    if not project_access.can_edit(role):
        raise HTTPException(status_code=403, detail="No access to this project's data source")


def _run_schema_introspection(ds: DataSource) -> DataSourceSchemaResponse:
    """Open a sync adapter, read the catalog, return the schema. Always closes."""
    from tripl.core.adapters.registry import build_adapter

    adapter = build_adapter(ds)
    try:
        tables = adapter.get_schema_tables()
    finally:
        try:
            adapter.close()
        except Exception as exc:  # noqa: BLE001 - close failure must not mask result
            logger.debug("adapter.close() failed after schema introspection: %s", exc)

    return DataSourceSchemaResponse(
        tables=[
            TableSchema(
                name=table.name,
                columns=[
                    ColumnSchema(name=column.name, data_type=column.data_type)
                    for column in table.columns
                ],
            )
            for table in tables
        ]
    )


async def get_schema_tables(session: AsyncSession, ds_id: uuid.UUID) -> DataSourceSchemaResponse:
    ds = await _fetch_data_source(session, ds_id)
    return await asyncio.to_thread(_run_schema_introspection, ds)
