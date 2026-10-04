from __future__ import annotations

import pytest

from tripl.core.adapters.registry import (
    build_adapter,
    register_adapter,
    supported_db_types,
)
from tripl.models.data_source import DataSource


def _make_ds(db_type: str) -> DataSource:
    ds = DataSource(
        name="test",
        db_type=db_type,
        host="localhost",
        port=5432,
        database_name="db",
        username="user",
        password_encrypted="",
    )
    return ds


def test_registry_lists_known_adapters() -> None:
    db_types = supported_db_types()
    assert "clickhouse" in db_types
    assert "postgres" in db_types
    assert "bigquery" in db_types


def test_build_adapter_rejects_unknown_db_type() -> None:
    ds = _make_ds("snowflake")
    with pytest.raises(ValueError, match="Unsupported db_type"):
        build_adapter(ds)


def test_register_adapter_can_inject_factory() -> None:
    sentinel = object()

    def factory(ds: DataSource, password: str) -> object:  # type: ignore[override]
        return sentinel

    register_adapter("__test_dummy__", factory)  # type: ignore[arg-type]
    try:
        ds = _make_ds("__test_dummy__")
        result = build_adapter(ds)
        assert result is sentinel
    finally:
        from tripl.core.adapters import registry as registry_module

        registry_module._REGISTRY.pop("__test_dummy__", None)


def test_clickhouse_timeout_maps_to_send_receive_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from tripl.core.adapters import clickhouse as clickhouse_module

    captured: dict[str, object] = {}

    class FakeClickHouseAdapter:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(clickhouse_module, "ClickHouseAdapter", FakeClickHouseAdapter)

    ds = _make_ds("clickhouse")
    ds.timeout_seconds = 75

    result = build_adapter(ds)

    assert isinstance(result, FakeClickHouseAdapter)
    assert captured["send_receive_timeout"] == 75
    assert captured["connect_timeout"] == 75
    assert "timeout_seconds" not in captured


def test_clickhouse_falls_back_to_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from tripl.core.adapters import clickhouse as clickhouse_module
    from tripl.core.adapters.registry import _DEFAULT_TIMEOUT_SECONDS

    captured: dict[str, object] = {}

    class FakeClickHouseAdapter:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(clickhouse_module, "ClickHouseAdapter", FakeClickHouseAdapter)

    ds = _make_ds("clickhouse")
    ds.timeout_seconds = None

    build_adapter(ds)

    assert captured["send_receive_timeout"] == _DEFAULT_TIMEOUT_SECONDS
    assert captured["connect_timeout"] == _DEFAULT_TIMEOUT_SECONDS


def test_postgres_wires_timeout_and_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    from tripl.core.adapters import postgres as postgres_module

    captured: dict[str, object] = {}

    class FakeConn:
        def close(self) -> None:
            return None

    def fake_connect(**kwargs: object) -> FakeConn:
        captured.update(kwargs)
        return FakeConn()

    monkeypatch.setattr(postgres_module.psycopg, "connect", fake_connect)

    ds = _make_ds("postgres")
    ds.host = "db.internal.example.com"
    ds.timeout_seconds = 90

    build_adapter(ds)

    assert captured["connect_timeout"] == 90
    # The session timezone is pinned to UTC through the same libpq `options` channel as
    # statement_timeout. It is not cosmetic: an offset-less timestamp column is compared
    # and binned in the *session* timezone, so a server whose TimeZone is Europe/Berlin
    # would shift every window bound and bucket edge (see tripl.core.bucketing).
    #
    # standard_conforming_strings is pinned for the same reason: `_quote_string` escapes a
    # warehouse-derived value by doubling the quote and nothing else, which is only sound
    # while a backslash is an ordinary character. default_transaction_read_only is defence
    # in depth behind the credential. Assert by EQUALITY, not `in` — the point is that no
    # other GUC appears and no expected one is missing.
    assert captured["options"] == (
        "-c timezone=UTC -c standard_conforming_strings=on "
        "-c default_transaction_read_only=on -c statement_timeout=90000"
    )
    # A REMOTE host that pins no mode gets `require`, not `prefer`. `prefer` negotiates
    # TLS when the server offers it and silently accepts PLAINTEXT when it doesn't, so a
    # stripped connection is indistinguishable from a healthy one. The registry used to
    # substitute a static `prefer` before the adapter could apply this host-aware
    # default, which left every remote warehouse tolerating plaintext.
    assert captured["sslmode"] == "require"
    assert captured["autocommit"] is True


def test_postgres_localhost_skips_tls_and_uses_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tripl.core.adapters import postgres as postgres_module
    from tripl.core.adapters.registry import _DEFAULT_TIMEOUT_SECONDS

    captured: dict[str, object] = {}

    class FakeConn:
        def close(self) -> None:
            return None

    def fake_connect(**kwargs: object) -> FakeConn:
        captured.update(kwargs)
        return FakeConn()

    monkeypatch.setattr(postgres_module.psycopg, "connect", fake_connect)

    ds = _make_ds("postgres")
    ds.host = "localhost"
    ds.timeout_seconds = None

    build_adapter(ds)

    # Local hosts do not force TLS: dev/docker Postgres has no certificate, and the
    # traffic never leaves the machine. `prefer` is libpq's own default (use TLS if
    # the server offers it, plaintext otherwise) — the adapter now passes it
    # EXPLICITLY rather than leaving it unset, because an sslmode it merely ignored
    # is the bug this closes.
    assert captured["sslmode"] == "prefer"
    assert captured["connect_timeout"] == _DEFAULT_TIMEOUT_SECONDS
    assert captured["options"] == (
        "-c timezone=UTC -c standard_conforming_strings=on "
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={_DEFAULT_TIMEOUT_SECONDS * 1000}"
    )
