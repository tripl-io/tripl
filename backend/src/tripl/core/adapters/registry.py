from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tripl.core.adapters.base import BaseAdapter
from tripl.crypto import decrypt_value
from tripl.models.data_source import DataSource
from tripl.schemas.data_source import (
    DEFAULT_BIGQUERY_MAXIMUM_BYTES_BILLED,
    DEFAULT_TIMEOUT_SECONDS,
    SSLKEY_STORAGE_KEY,
    BigQuerySettings,
    ClickHouseSettings,
    DatabricksSettings,
    PostgresSettings,
    SnowflakeSettings,
)

AdapterFactory = Callable[[DataSource, str], BaseAdapter]

_REGISTRY: dict[str, AdapterFactory] = {}

# Fallback connect/query budget when a data source leaves timeout_seconds unset.
# Comfortably under Celery's 55/60-min hard limit so a runaway query is cut off
# by the adapter long before the worker is SIGKILLed.
_DEFAULT_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS


def _effective_timeout_seconds(ds: DataSource) -> int:
    timeout = ds.timeout_seconds
    if timeout is None or timeout <= 0:
        return _DEFAULT_TIMEOUT_SECONDS
    return timeout


def _stored_settings(ds: DataSource) -> dict[str, Any]:
    """The raw connection settings stored for this source, minus the secret.

    Each factory below validates this against its own settings model, so a
    setting that does not belong to the warehouse fails the adapter build loudly
    (surfacing as a connection error) instead of being silently ignored — which
    is exactly what PostgreSQL's stored ``extra_params`` used to do.
    """
    raw = dict(ds.extra_params) if isinstance(ds.extra_params, dict) else {}
    raw.pop(SSLKEY_STORAGE_KEY, None)  # storage-only key; not part of the model
    return raw


def _stored_sslkey(ds: DataSource) -> str | None:
    """Decrypt the PostgreSQL client private key, if one is stored."""
    if not isinstance(ds.extra_params, dict):
        return None
    ciphertext = ds.extra_params.get(SSLKEY_STORAGE_KEY)
    if not isinstance(ciphertext, str) or not ciphertext:
        return None
    return decrypt_value(ciphertext) or None


def register_adapter(db_type: str, factory: AdapterFactory) -> None:
    _REGISTRY[db_type] = factory


def supported_db_types() -> list[str]:
    return sorted(_REGISTRY.keys())


def build_adapter(ds: DataSource) -> BaseAdapter:
    factory = _REGISTRY.get(ds.db_type)
    if factory is None:
        msg = f"Unsupported db_type: {ds.db_type}"
        raise ValueError(msg)
    password = decrypt_value(ds.password_encrypted)
    return factory(ds, password)


def vetted_address(ds: DataSource) -> str | None:
    """The address to connect to, after the host is checked; None when unchecked.

    When outbound hosts must be public (``Settings.public_hosts_only``: set, or
    a hosted instance) the host an organization typed must be public: a
    warehouse connection is outbound traffic like a webhook, and a private one
    would let a tenant read the instance's own database, cache or broker (or a
    cloud metadata endpoint) through scans. The name is resolved here, every
    address it gives is vetted, and the driver connects to the vetted one, so a
    short-TTL name cannot answer publicly for the check and privately for the
    connection. Without that setting an operator may point at an internal
    warehouse on purpose.
    """
    from tripl.config import settings
    from tripl.core.adapters.errors import WarehouseCapabilityError
    from tripl.services.safe_http import PrivateHostError, public_address

    if not settings.public_hosts_only:
        return None
    try:
        return public_address(ds.host, ds.port, field="host")
    except PrivateHostError:
        raise WarehouseCapabilityError(
            "this instance only connects to warehouses on the public internet, "
            "and the host resolves to a private or internal address."
        ) from None


def _build_clickhouse(ds: DataSource, password: str) -> BaseAdapter:
    from tripl.core.adapters.clickhouse import ClickHouseAdapter

    ClickHouseSettings.model_validate(_stored_settings(ds))  # no CH-specific settings today
    timeout = _effective_timeout_seconds(ds)
    # connect_timeout bounds the TCP/handshake; send_receive_timeout bounds the
    # query so a runaway base_query can't run until Celery's hard limit.
    kwargs: dict[str, object] = {
        "connect_timeout": timeout,
        "send_receive_timeout": timeout,
    }

    address = vetted_address(ds)
    if address is not None:
        # Connect to the vetted address; TLS still checks the certificate
        # against the name the source was configured with.
        kwargs["server_host_name"] = ds.host

    return ClickHouseAdapter(
        host=address or ds.host,
        port=ds.port,
        database=ds.database_name,
        username=ds.username,
        password=password,
        json_path_discovery=ds.json_path_discovery,
        **kwargs,
    )


def _build_postgres(ds: DataSource, password: str) -> BaseAdapter:
    from tripl.core.adapters.postgres import PostgresAdapter

    settings = PostgresSettings.model_validate(_stored_settings(ds))

    return PostgresAdapter(
        host=ds.host,
        # libpq connects to hostaddr and keeps host for TLS verification.
        hostaddr=vetted_address(ds),
        port=ds.port,
        database=ds.database_name,
        username=ds.username,
        password=password,
        timeout_seconds=_effective_timeout_seconds(ds),
        # Pass None through when the source pins no mode: the default is *host-aware*
        # (remote -> `require`, localhost -> `prefer`), and only the adapter knows the
        # host. Substituting a static default here made PostgresAdapter's `require`
        # branch unreachable, so every remote warehouse silently fell back to `prefer`
        # — which negotiates TLS if offered and quietly accepts PLAINTEXT if not.
        sslmode=settings.sslmode,
        sslrootcert=settings.sslrootcert,
        sslcert=settings.sslcert,
        sslkey=_stored_sslkey(ds),
        search_path=settings.search_path,
    )


def _build_bigquery(ds: DataSource, password: str) -> BaseAdapter:
    from tripl.core.adapters.bigquery import BigQueryAdapter

    settings = BigQuerySettings.model_validate(_stored_settings(ds))

    return BigQueryAdapter(
        host=ds.host,
        port=ds.port,
        database=ds.database_name,
        username=ds.username,
        password=password,
        location=settings.location,
        # BigQuery used to get no deadline at all: a stuck job could hold a worker
        # until Celery's hard limit. It now shares the data source's budget.
        timeout_seconds=_effective_timeout_seconds(ds),
        # Cost guard — a query whose dry-run estimate exceeds this is refused by
        # BigQuery before it runs.
        maximum_bytes_billed=(
            settings.maximum_bytes_billed or DEFAULT_BIGQUERY_MAXIMUM_BYTES_BILLED
        ),
        dataset_allowlist=settings.dataset_allowlist,
    )


def _build_databricks(ds: DataSource, password: str) -> BaseAdapter:
    from tripl.core.adapters.databricks import DatabricksAdapter
    from tripl.core.adapters.databricks_auth import check_workspace_host

    settings = DatabricksSettings.model_validate(_stored_settings(ds))
    # The outbound rule, in two halves. The address check is the one every
    # warehouse gets, but its answer cannot be handed to the driver: the
    # connector opens its own urllib3 pools from the hostname, with no hook for
    # an address to dial while keeping the TLS name, so it resolves the name
    # again. The domain check is what makes that second lookup trustworthy — see
    # ``check_workspace_host``.
    check_workspace_host(ds.host)
    vetted_address(ds)

    return DatabricksAdapter(
        host=ds.host,
        port=443,
        database=ds.database_name,
        username=ds.username,
        password=password,
        http_path=settings.http_path,
        auth_type=settings.auth_type or "pat",
        schema=settings.schema_name,
        schema_allowlist=settings.schema_allowlist,
        timeout_seconds=_effective_timeout_seconds(ds),
    )


def _build_snowflake(ds: DataSource, password: str) -> BaseAdapter:
    from tripl.core.adapters.snowflake import SnowflakeAdapter
    from tripl.core.adapters.snowflake_sql import resolve_host

    settings = SnowflakeSettings.model_validate(_stored_settings(ds))
    # The outbound rule. ``resolve_host`` turns whatever the host field holds into
    # a ``*.snowflakecomputing.com`` name, so the driver can only ever reach a name
    # Snowflake's DNS answers for (the half Databricks gets from
    # ``check_workspace_host``); the address check then refuses one that resolves
    # privately, which is what a PrivateLink account does. Like the Databricks
    # driver, the connector opens its own connection pools from the hostname.
    target = resolve_host(ds.host)
    _vet_hostname(target.hostname, 443)

    return SnowflakeAdapter(
        host=target.hostname,
        port=443,
        database=ds.database_name,
        username=ds.username,
        password=password,
        account=target.account,
        warehouse=settings.warehouse,
        auth_type=settings.auth_type or "password",
        role=settings.role,
        schema=settings.schema_name,
        schema_allowlist=settings.schema_allowlist,
        timeout_seconds=_effective_timeout_seconds(ds),
    )


def _vet_hostname(hostname: str, port: int) -> None:
    """:func:`vetted_address` for a hostname the factory derived itself."""
    from tripl.config import settings
    from tripl.core.adapters.errors import WarehouseCapabilityError
    from tripl.services.safe_http import PrivateHostError, public_address

    if not settings.public_hosts_only:
        return
    try:
        public_address(hostname, port, field="host")
    except PrivateHostError:
        raise WarehouseCapabilityError(
            "this instance only connects to warehouses on the public internet, "
            "and the host resolves to a private or internal address."
        ) from None


def _build_synthetic(ds: DataSource, password: str) -> BaseAdapter:
    # The synthetic warehouse is local and in-memory: host/port/credentials are
    # ignored entirely (no socket is ever opened). A per-source seed derived from
    # the DataSource id keeps each demo project's data stable-but-distinct. A
    # demo's source also names its seeded spike and its traffic model, so the
    # newest hours a scheduled collection re-reads are the ones the demo stored.
    from tripl.core.adapters.synthetic import SyntheticAdapter, _digest_int, stored_spike
    from tripl.core.adapters.synthetic_traffic import stored_traffic

    seed = _digest_int("synthetic", str(ds.id)) % (2**31)
    return SyntheticAdapter(
        seed=seed,
        timeout_seconds=_effective_timeout_seconds(ds),
        spike=stored_spike(ds.extra_params),
        traffic=stored_traffic(ds.extra_params),
    )


register_adapter("clickhouse", _build_clickhouse)
register_adapter("postgres", _build_postgres)
register_adapter("bigquery", _build_bigquery)
register_adapter("databricks", _build_databricks)
register_adapter("snowflake", _build_snowflake)
register_adapter("synthetic", _build_synthetic)
