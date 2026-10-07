import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import ColumnElement, and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tripl import cache
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.crypto import encrypt_value
from tripl.middleware.org_context import require_org_id
from tripl.models.data_source import DataSource, DBType, TestStatus
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob
from tripl.schemas.data_source import (
    DATA_SOURCE_SCAN_REFS_LIMIT,
    SSLKEY_STORAGE_KEY,
    ConnectionSettingsError,
    DataSourceConnectionTest,
    DataSourceConnectionTestResponse,
    DataSourceCreate,
    DataSourceResponse,
    DataSourceScanRef,
    DataSourceTestResponse,
    DataSourceUpdate,
    connection_settings_response,
    parse_connection_settings,
)
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import owning_org_id
from tripl.services.search_service import reindex_project_branch

# Defensive cap on ``GET /data-sources``, per organization (it was one global
# LIMIT before F20 PR3; with one organization the two are the same).
_LIST_LIMIT_PER_ORG = 1000


async def list_data_sources(
    session: AsyncSession, *, visible_project_ids: set[uuid.UUID] | None
) -> list[DataSourceResponse]:
    """Every data source the caller may see, with usage counted over the same set.

    ``visible_project_ids`` is ``project_access.member_project_ids`` for the
    caller (``None`` = every project, for scripts; a request always passes the
    caller's set, which for an organization owner/admin is every project of
    that organization). A workspace-global
    source (``project_id`` NULL) is listed for everyone; a source bound to a
    project the caller is not a member of is left out entirely, since a
    non-member must not learn the project exists. The cached list is
    per organization and filtered after the read, so one entry serves every user
    of an organization; the entry is keyed by the bound organization (F20 PR3) so
    no two organizations share it. Only the bound organization's sources are
    read (F20 PR4): another organization's warehouses do not exist here.
    """
    list_key = cache.key_data_sources_list(owning_org_id())
    cached = await cache.get_json(list_key)
    if cached is not None:
        return await _with_usage(
            session,
            _only_visible(
                [DataSourceResponse.model_validate(item) for item in cached],
                visible_project_ids,
            ),
            visible_project_ids=visible_project_ids,
        )

    result = await session.execute(
        select(DataSource)
        .where(DataSource.organization_id == owning_org_id())
        .order_by(DataSource.created_at.desc())
        .limit(_LIST_LIMIT_PER_ORG)
    )
    rows = result.scalars().all()
    responses = [_to_response(ds) for ds in rows]
    await cache.set_json(
        list_key,
        [r.model_dump(mode="json") for r in responses],
        ttl_seconds=300,
    )
    return await _with_usage(
        session,
        _only_visible(responses, visible_project_ids),
        visible_project_ids=visible_project_ids,
    )


async def get_data_source(
    session: AsyncSession, ds_id: uuid.UUID, *, visible_project_ids: set[uuid.UUID] | None
) -> DataSourceResponse:
    """One data source; one bound to a non-member project is "not found"."""
    ds = await _fetch_data_source(session, ds_id)
    if not _is_visible(ds.project_id, visible_project_ids):
        raise HTTPException(status_code=404, detail="Data source not found")
    return (
        await _with_usage(session, [_to_response(ds)], visible_project_ids=visible_project_ids)
    )[0]


def _is_visible(project_id: uuid.UUID | None, visible: set[uuid.UUID] | None) -> bool:
    """A global source, or one bound to a project in the caller's member set."""
    return visible is None or project_id is None or project_id in visible


def _only_visible(
    sources: list[DataSourceResponse], visible: set[uuid.UUID] | None
) -> list[DataSourceResponse]:
    return [source for source in sources if _is_visible(source.project_id, visible)]


async def _with_usage(
    session: AsyncSession,
    sources: list[DataSourceResponse],
    *,
    visible_project_ids: set[uuid.UUID] | None,
) -> list[DataSourceResponse]:
    """Merge each source's scan and scan-run counts, and its scans, in.

    Every path that hands a source back goes through here, not only list and
    get: the page writes the create / update / test responses straight into its
    list cache, and ``DataSourceResponse`` defaults both counts to 0, so a bare
    ``_to_response`` there would tell the delete confirm that a source with
    scans has none.

    Two grouped queries over the listed ids, run on every read rather than
    cached with the list: scans are created and deleted in projects, and no
    project path invalidates the data-source cache.

    Counts and refs cover only scans in ``visible_project_ids`` (``None`` = all,
    for the org-admin write paths, which answer about one of their
    organization's sources). A shared source read
    by a project the caller is not a member of must not reveal that project's
    name, slug or even its scan volume.
    """
    if not sources:
        return sources
    ids = [source.id for source in sources]
    in_scope: ColumnElement[bool] = ScanConfig.data_source_id.in_(ids)
    if visible_project_ids is not None:
        in_scope = and_(in_scope, ScanConfig.project_id.in_(visible_project_ids))
    scan_counts = dict(
        (
            await session.execute(
                select(ScanConfig.data_source_id, func.count(ScanConfig.id))
                .where(in_scope)
                .group_by(ScanConfig.data_source_id)
            )
        )
        .tuples()
        .all()
    )
    run_counts = dict(
        (
            await session.execute(
                select(ScanConfig.data_source_id, func.count(ScanJob.id))
                .join(ScanJob, ScanJob.scan_config_id == ScanConfig.id)
                .where(in_scope)
                .group_by(ScanConfig.data_source_id)
            )
        )
        .tuples()
        .all()
    )
    # Capped per source in SQL, not in the loop: this runs on every data-source
    # list and detail response, and a source shared by many projects would
    # otherwise ship all its scans over the wire to keep twenty.
    ranked = (
        select(
            ScanConfig.data_source_id.label("data_source_id"),
            ScanConfig.id.label("scan_id"),
            ScanConfig.name.label("scan_name"),
            Project.slug.label("project_slug"),
            Project.name.label("project_name"),
            func.row_number()
            .over(
                partition_by=ScanConfig.data_source_id,
                order_by=(Project.name, ScanConfig.name, ScanConfig.id),
            )
            .label("position"),
        )
        .join(Project, Project.id == ScanConfig.project_id)
        .where(in_scope)
        .subquery()
    )
    scan_refs: dict[uuid.UUID, list[DataSourceScanRef]] = {}
    for data_source_id, scan_id, scan_name, project_slug, project_name in (
        await session.execute(
            select(
                ranked.c.data_source_id,
                ranked.c.scan_id,
                ranked.c.scan_name,
                ranked.c.project_slug,
                ranked.c.project_name,
            )
            .where(ranked.c.position <= DATA_SOURCE_SCAN_REFS_LIMIT)
            .order_by(ranked.c.data_source_id, ranked.c.position)
        )
    ).all():
        scan_refs.setdefault(data_source_id, []).append(
            DataSourceScanRef(
                id=scan_id,
                name=scan_name,
                project_slug=project_slug,
                project_name=project_name,
            )
        )
    return [
        source.model_copy(
            update={
                "scan_count": int(scan_counts.get(source.id, 0) or 0),
                "scan_run_count": int(run_counts.get(source.id, 0) or 0),
                "scans": scan_refs.get(source.id, []),
            }
        )
        for source in sources
    ]


# Fields on a synthetic source that, if edited, would turn it into (or point it
# at) a real warehouse. They are locked: a synthetic source is demo-only and must
# never gain a real host, port, database, or credentials.
_SYNTHETIC_LOCKED_FIELDS = (
    "host",
    "port",
    "database_name",
    "username",
    "password",
    "connection_settings",
)

# Sentinel: "the client did not send connection_settings at all" (leave stored
# settings untouched) as distinct from "the client sent null" (clear them).
_UNSET = object()


def _validated_settings(db_type: str, raw: object) -> BaseModel | None:
    """Validate a raw settings payload against the warehouse it is destined for.

    ``raw`` is the dict of keys the client actually sent (the request model's
    nested union already rejected keys that belong to no warehouse at all). This
    is where a setting that exists but does not apply to *this* db_type — a
    BigQuery ``location`` on a PostgreSQL source — becomes a 422 instead of a
    silently stored no-op.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):  # pragma: no cover - pydantic guarantees a mapping
        raise HTTPException(status_code=422, detail="connection_settings must be an object")
    try:
        return parse_connection_settings(db_type, raw)
    except ConnectionSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _require_databricks_warehouse(db_type: str, stored: dict[str, Any] | None) -> None:
    """A Databricks or Snowflake source is unusable without its warehouse: say so on save.

    The settings models already require ``http_path`` / ``warehouse`` whenever
    settings are sent; this covers the request that sends none at all, which
    would otherwise store a source every connection to fails.
    """
    if db_type == DBType.databricks.value and not (stored or {}).get("http_path"):
        raise HTTPException(
            status_code=422,
            detail=(
                "A Databricks data source needs connection_settings.http_path, the SQL "
                "warehouse's HTTP path (e.g. /sql/1.0/warehouses/1234abcd)."
            ),
        )
    if db_type == DBType.snowflake.value and not (stored or {}).get("warehouse"):
        raise HTTPException(
            status_code=422,
            detail=(
                "A Snowflake data source needs connection_settings.warehouse, the "
                "virtual warehouse its queries run on (e.g. COMPUTE_WH)."
            ),
        )


def _settings_to_storage(
    settings: BaseModel | None, *, previous: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Serialize validated settings for the ``extra_params`` JSON column.

    The private key never lands in the column as plaintext: it is Fernet-encrypted
    under ``sslkey_encrypted``, exactly like ``password_encrypted``. An omitted
    ``sslkey`` keeps the stored key (same UX as an omitted password); an explicit
    empty string clears it.
    """
    if settings is None:
        return None
    stored: dict[str, Any] = settings.model_dump(exclude_none=True, mode="python")
    secret = stored.pop("sslkey", None)

    if secret is None:
        carried = (previous or {}).get(SSLKEY_STORAGE_KEY)
        if carried:
            stored[SSLKEY_STORAGE_KEY] = carried
    else:
        plaintext = secret.get_secret_value()
        if plaintext:
            stored[SSLKEY_STORAGE_KEY] = encrypt_value(plaintext)

    return stored or None


async def create_data_source(session: AsyncSession, data: DataSourceCreate) -> DataSourceResponse:
    # Synthetic sources are local demo data created ONLY by the demo seeder (which
    # constructs the ORM row directly, bypassing this service). The user-facing
    # create path must never mint one.
    if data.db_type == DBType.synthetic:
        raise HTTPException(
            status_code=422,
            detail="Synthetic data sources are created only by demo projects, not directly.",
        )

    # The bound organization owns the source; with none bound this raises rather
    # than defaulting (F20 PR5). Names are unique per organization.
    organization_id = require_org_id()
    if await _name_taken(session, organization_id, data.name):
        raise HTTPException(status_code=409, detail="Data source with this name already exists")

    raw_settings = data.model_dump(exclude_unset=True).get("connection_settings")
    settings = _validated_settings(data.db_type.value, raw_settings)
    stored_settings = _settings_to_storage(settings, previous=None)
    _require_databricks_warehouse(data.db_type.value, stored_settings)

    ds = DataSource(
        # The bound organization, where every data-source route looks it up.
        organization_id=organization_id,
        name=data.name,
        db_type=data.db_type,
        host=data.host,
        port=data.port,
        database_name=data.database_name,
        username=data.username,
        password_encrypted=encrypt_value(data.password),
        timeout_seconds=data.timeout_seconds,
        json_path_discovery=data.json_path_discovery,
        extra_params=stored_settings,
    )
    session.add(ds)
    await session.commit()
    await session.refresh(ds)
    await cache.delete_prefix(cache.prefix_data_sources())
    # Org-admin write path: an owner/admin sees every project's usage of it.
    return (await _with_usage(session, [_to_response(ds)], visible_project_ids=None))[0]


async def update_data_source(
    session: AsyncSession, ds_id: uuid.UUID, data: DataSourceUpdate
) -> DataSourceResponse:
    ds = await _fetch_data_source(session, ds_id)
    update_dict = data.model_dump(exclude_unset=True)
    _reject_synthetic_conversion(ds, update_dict)

    # Read now: the rollback below expires the row, and an async session cannot
    # lazy-load it back.
    organization_id = ds.organization_id
    if (
        "name" in update_dict
        and update_dict["name"] != ds.name
        and await _name_taken(session, organization_id, update_dict["name"], exclude_id=ds.id)
    ):
        raise HTTPException(status_code=409, detail="Data source with this name already exists")

    # Handle password separately
    if "password" in update_dict:
        password = update_dict.pop("password")
        if password is not None:
            ds.password_encrypted = encrypt_value(password)

    # Settings are validated against the db_type the row will *have* after this
    # update, not the one it had before.
    raw_settings = update_dict.pop("connection_settings", _UNSET)
    if raw_settings is not _UNSET:
        db_type = str(update_dict.get("db_type") or ds.db_type)
        settings = _validated_settings(db_type, raw_settings)
        previous = ds.extra_params if isinstance(ds.extra_params, dict) else None
        ds.extra_params = _settings_to_storage(settings, previous=previous)

    for key, value in update_dict.items():
        setattr(ds, key, value)
    _require_databricks_warehouse(
        str(ds.db_type), ds.extra_params if isinstance(ds.extra_params, dict) else None
    )

    new_name = update_dict.get("name")
    try:
        await session.commit()
    except IntegrityError as exc:
        # the pre-check above is a plain SELECT holding nothing, so two
        # concurrent renames to the same free name both pass it and the loser meets
        # uq_data_sources_organization_name at the commit. Rolled back, the name is
        # looked up again: if another source now holds it, this is that race and gets
        # the pre-check's 409. Any other integrity failure is re-raised.
        await session.rollback()
        if new_name is not None and await _name_taken(
            session, organization_id, new_name, exclude_id=ds_id
        ):
            raise HTTPException(
                status_code=409, detail="Data source with this name already exists"
            ) from exc
        raise
    await session.refresh(ds)
    await cache.delete_prefix(cache.prefix_data_sources())
    # Org-admin write path: an owner/admin sees every project's usage of it.
    return (await _with_usage(session, [_to_response(ds)], visible_project_ids=None))[0]


async def _name_taken(
    session: AsyncSession,
    organization_id: uuid.UUID,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    """Whether ``name`` names a source of ``organization_id`` (other than ``exclude_id``).

    Per organization since F20 PR5, like ``uq_data_sources_organization_name``.
    """
    statement = select(DataSource.id).where(
        DataSource.organization_id == organization_id, DataSource.name == name
    )
    if exclude_id is not None:
        statement = statement.where(DataSource.id != exclude_id)
    return (await session.execute(statement.limit(1))).first() is not None


async def _refresh_main_search_indexes(session: AsyncSession, project_ids: list[uuid.UUID]) -> None:
    """Refresh the MAIN search index of every project a deleted source touched.

    Mirrors ``scan_service._refresh_main_search_index`` and the alerting one:
    scan configs and alert rules are project-global rather than branch-scoped, so
    only the MAIN branch is refreshed eagerly and feature-branch indexes pick the
    change up on their next rebuild.

    Plural, and driven by the doomed scan configs rather than by ``ds.project_id``,
    because a data source is workspace-global by default — ``DataSource.project_id``
    is NULL for a shared source and names no project at all — so the configs that
    cascade away with it can belong to several projects at once. No slug is passed
    because the data-source routes are workspace-level and carry none; the
    reindexer resolves it from the project id.

    Unlike the alerting helper these imports are NOT deferred: the cycle it
    documents (``search_service`` -> ``project_service`` -> ``alerting_service``)
    never reaches this module — nothing in ``search_service``'s import graph
    imports ``datasource_service`` — so this can import at the top like
    ``scan_service`` does.
    """
    for project_id in project_ids:
        try:
            main_branch_id = await resolve_branch_id(session, project_id, None)
            await reindex_project_branch(session, project_id=project_id, branch_id=main_branch_id)
        except Exception:
            await session.rollback()
            # Fault-isolated per project, because this runs AFTER the destructive
            # commit: the source is already gone, nothing will retry, and one
            # project's rebuild failing must not strand the projects behind it in
            # the loop nor turn a delete that already succeeded into a 500. Same
            # treatment the worker path gives this call (worker/search_reindex.py).
            # A stale index is a search result that 404s on click; a swallowed
            # delete would be worse.
            logger.exception(
                "Search reindex failed for project %s after a data-source delete", project_id
            )


async def delete_data_source(session: AsyncSession, ds_id: uuid.UUID) -> None:
    from tripl.services._alerting_destinations import disable_rules_bound_to_scan

    ds = await _fetch_data_source(session, ds_id, with_scan_configs=True)
    # Deleting a source takes its scan configs with it (``DataSource.scan_configs``
    # is delete-orphan), which never passes through ``delete_scan_config`` and so
    # never reached the unbind step. The FK is ON DELETE SET NULL and NULL means
    # "every scan in the project", so a rule someone had narrowed to a scan of
    # this source would silently re-widen and start paging on every OTHER scan —
    # the exact failure the scan-delete path exists to prevent.
    doomed_configs = list(ds.scan_configs)
    for config in doomed_configs:
        await disable_rules_bound_to_scan(session, config.id)
    # Read the owning projects BEFORE the delete: the delete evicts these instances
    # from the identity map and marks them deleted, so afterwards they no longer
    # stand for live rows and must not be read. NOT because the read would raise —
    # both session factories are ``expire_on_commit=False`` (database.py,
    # tests/conftest.py), so ``project_id`` would in fact still answer off the
    # loaded ``__dict__``, exactly as ``event_service`` spells out one door over.
    # The ordering is the discipline, and it survives a flip of that flag.
    affected_projects = list(dict.fromkeys(config.project_id for config in doomed_configs))
    await session.delete(ds)
    await session.commit()
    await cache.delete_prefix(cache.prefix_data_sources())
    # The same cascade that skipped the unbind above also skipped the index
    # refresh ``delete_scan_config`` does, so a deleted scan stayed findable in
    # the command palette and /search. One whole-branch reindex per
    # project covers both document kinds this delete moved: the ``scan_config``
    # documents are gone, and the alert rules just unbound above are rebuilt
    # without the scan name their subtitle used to carry.
    await _refresh_main_search_indexes(session, affected_projects)


async def _fetch_data_source(
    session: AsyncSession, ds_id: uuid.UUID, *, with_scan_configs: bool = False
) -> DataSource:
    # Fenced to the bound organization (F20 PR4): another organization's source
    # is "not found", the same 404 an unknown id gets, for every caller —
    # get, update, delete, test and schema introspection.
    query = select(DataSource).where(
        DataSource.id == ds_id, DataSource.organization_id == owning_org_id()
    )
    if with_scan_configs:
        query = query.options(selectinload(DataSource.scan_configs))
    result = await session.execute(query)
    ds = result.scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail="Data source not found")
    return ds


def _reject_synthetic_conversion(ds: DataSource, update_dict: dict[str, object]) -> None:
    """Guard synthetic-source identity on update.

    A synthetic source cannot be edited into a real db_type or pointed at a real
    host / given real credentials, and a real source cannot be converted into a
    synthetic one. Everything else (rename, timeout) is allowed.
    """
    new_db_type = update_dict.get("db_type")
    if ds.db_type == DBType.synthetic:
        if new_db_type is not None and new_db_type != DBType.synthetic:
            raise HTTPException(
                status_code=422,
                detail="A synthetic data source's type cannot be changed to a real warehouse.",
            )
        locked = [
            field
            for field in _SYNTHETIC_LOCKED_FIELDS
            if field in update_dict and update_dict[field] is not None
        ]
        if locked:
            raise HTTPException(
                status_code=422,
                detail="A synthetic data source cannot be given a real host or credentials.",
            )
    elif new_db_type == DBType.synthetic:
        raise HTTPException(
            status_code=422,
            detail="A data source cannot be converted to the synthetic type.",
        )


def _to_response(ds: DataSource) -> DataSourceResponse:
    return DataSourceResponse(
        id=ds.id,
        project_id=ds.project_id,
        name=ds.name,
        db_type=ds.db_type,
        is_synthetic=ds.db_type == DBType.synthetic,
        host=ds.host,
        port=ds.port,
        database_name=ds.database_name,
        username=ds.username,
        password_set=bool(ds.password_encrypted),
        timeout_seconds=ds.timeout_seconds,
        json_path_discovery=ds.json_path_discovery,
        connection_settings=connection_settings_response(
            ds.extra_params if isinstance(ds.extra_params, dict) else None
        ),
        last_test_at=ds.last_test_at,
        last_test_status=ds.last_test_status,
        last_test_message=ds.last_test_message,
        created_at=ds.created_at,
        updated_at=ds.updated_at,
    )


logger = logging.getLogger(__name__)


_TIMEOUT_HINTS = ("timed out", "timeout")
_UNREACHABLE_HINTS = (
    "refused",
    "getaddrinfo",
    "could not connect",
    "connection failed",
    "name or service not known",
    "no route to host",
    "network unreachable",
    "could not translate host name",
)
_AUTH_HINTS = ("auth", "password", "access denied", "credential", "permission")
# psycopg's refusal when the SSL mode demands TLS and the server offers none. It
# also contains "connection failed", so it has to be read before the
# unreachable hints: a fresh local PostgreSQL has no TLS and an unset SSL mode
# resolves to `require` for any host but localhost, so this is the first error
# most people meet, and "could not reach" sent them to check a network that works.
_NO_SERVER_TLS_HINTS = ("does not support ssl",)

# Every message below opens with this. A connection probe is not a scan:
# ``worker.tasks._errors.user_facing_error`` GUARANTEES a "Scan failed" prefix
# because ``frontend/src/lib/scanError.ts`` keys on it, which reads as nonsense
# under a data source's Test connection button (that prefix is a
# contract rather than an accident, so the mismatch is now written down).
_TEST_FAILED = "Connection test failed"


def _friendly_test_error(exc: Exception) -> str:
    """Map a raw connection-probe exception to a safe, user-facing message.

    THE owner of ``DataSource.last_test_message`` wording for the in-request
    connection probe. The unused Celery probe was removed.

    Never echoes host/port/driver/credential internals — those go to logs only.
    """
    # A capability error is a message tripl authored about a configuration the
    # operator can act on ("PostgreSQL 13 is too old: date_bin() needs 14",
    # "BigQuery: host (project_id) is required"). It holds no host, port or
    # credential, and generalizing it away leaves the operator re-checking
    # settings that are all, in fact, correct. Surface it verbatim.
    if isinstance(exc, WarehouseCapabilityError):
        return f"{_TEST_FAILED}: {exc}"

    text = str(exc).lower()
    if any(hint in text for hint in _AUTH_HINTS):
        return f"{_TEST_FAILED}: authentication was rejected — check the credentials."
    if any(hint in text for hint in _TIMEOUT_HINTS):
        return f"{_TEST_FAILED}: the data source did not respond in time."
    if any(hint in text for hint in _NO_SERVER_TLS_HINTS):
        return (
            f"{_TEST_FAILED}: the server does not offer TLS, and this connection requires "
            "it. Enable TLS on the server, or set SSL mode to disable for a server you "
            "reach over a trusted network."
        )
    if any(hint in text for hint in _UNREACHABLE_HINTS):
        return (
            f"{_TEST_FAILED}: could not reach the data source — check the host, port, and network."
        )
    return f"{_TEST_FAILED}. Check the connection settings and try again."


def _run_adapter_test(ds: DataSource) -> tuple[bool, str]:
    """Open a sync adapter, run a probe, return (ok, message). Always closes."""
    from tripl.core.adapters.registry import build_adapter

    try:
        adapter = build_adapter(ds)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Data source adapter build failed for %s", ds.id)
        return False, _friendly_test_error(exc)

    try:
        ok = bool(adapter.test_connection())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Data source connection test failed for %s", ds.id)
        return False, _friendly_test_error(exc)
    finally:
        with contextlib.suppress(Exception):
            adapter.close()

    return (ok, "Connection successful" if ok else "Connection probe returned no rows")


async def test_data_source_connection(
    session: AsyncSession, ds_id: uuid.UUID
) -> DataSourceTestResponse:
    ds = await _fetch_data_source(session, ds_id)
    success, message = await asyncio.to_thread(_run_adapter_test, ds)
    tested_at = datetime.now(UTC)

    ds.last_test_at = tested_at
    ds.last_test_status = TestStatus.success.value if success else TestStatus.failed.value
    ds.last_test_message = message
    await session.commit()
    await session.refresh(ds)
    await cache.delete_prefix(cache.prefix_data_sources())

    return DataSourceTestResponse(
        success=success,
        message=message,
        tested_at=tested_at,
        # Org-admin only: an owner/admin sees every project's usage of it.
        data_source=(await _with_usage(session, [_to_response(ds)], visible_project_ids=None))[0],
    )


async def test_unsaved_connection(
    data: DataSourceConnectionTest,
) -> DataSourceConnectionTestResponse:
    """Probe a config that has not been saved, and store nothing.

    The same adapter probe and the same safe wording as a saved source's test.
    The source is a TRANSIENT row built from the request alone — never added to
    a session — so a failed probe leaves no half-made source behind, and no
    stored secret can be borrowed by naming one.
    """
    if data.db_type == DBType.synthetic:
        raise HTTPException(
            status_code=422,
            detail="Synthetic data sources are created only by demo projects, not directly.",
        )
    raw_settings = data.model_dump(exclude_unset=True).get("connection_settings")
    settings = _validated_settings(data.db_type.value, raw_settings)
    stored_settings = _settings_to_storage(settings, previous=None)
    _require_databricks_warehouse(data.db_type.value, stored_settings)
    ds = DataSource(
        name=data.name,
        db_type=data.db_type,
        host=data.host,
        port=data.port,
        database_name=data.database_name,
        username=data.username,
        password_encrypted=encrypt_value(data.password),
        timeout_seconds=data.timeout_seconds,
        json_path_discovery=data.json_path_discovery,
        extra_params=stored_settings,
    )
    success, message = await asyncio.to_thread(_run_adapter_test, ds)
    return DataSourceConnectionTestResponse(
        success=success, message=message, tested_at=datetime.now(UTC)
    )
