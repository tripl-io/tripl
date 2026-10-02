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
    PostgresSettings,
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

    return ClickHouseAdapter(
        host=ds.host,
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


def _build_synthetic(ds: DataSource, password: str) -> BaseAdapter:
    # The synthetic warehouse is local and in-memory: host/port/credentials are
    # ignored entirely (no socket is ever opened). A per-source seed derived from
    # the DataSource id keeps each demo project's data stable-but-distinct.
    from tripl.core.adapters.synthetic import SyntheticAdapter, _digest_int, stored_spike

    seed = _digest_int("synthetic", str(ds.id)) % (2**31)
    return SyntheticAdapter(
        seed=seed,
        timeout_seconds=_effective_timeout_seconds(ds),
        spike=stored_spike(ds.extra_params),
    )


register_adapter("clickhouse", _build_clickhouse)
register_adapter("postgres", _build_postgres)
register_adapter("bigquery", _build_bigquery)
register_adapter("synthetic", _build_synthetic)
