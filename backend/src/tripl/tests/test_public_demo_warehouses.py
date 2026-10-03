"""A public instance cannot be used to reach its own network (tripl-sav5.1).

Hosted: a warehouse host must resolve to public addresses only, and the driver
connects to the address that was vetted (Postgres ``hostaddr``, ClickHouse the
address with ``server_host_name`` for TLS); a BigQuery key may only exchange
tokens with Google. Public demo: visitors cannot add, edit or probe a warehouse
of their own at all.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tripl.config import settings
from tripl.core.adapters import registry
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.models.data_source import DataSource
from tripl.services.datasource_service import _run_adapter_test
from tripl.storage.photo_storage import GOOGLE_TOKEN_URI


def _source(db_type: str, host: str, port: int = 5432) -> DataSource:
    return DataSource(
        id=uuid.uuid4(),
        name="wh",
        db_type=db_type,
        host=host,
        port=port,
        database_name="analytics",
        username="reader",
        password_encrypted="",
        extra_params={},
    )


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "hosted")


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    """The keyword arguments each adapter class was built with, no connection made."""
    seen: dict[str, dict[str, Any]] = {}

    def fake(name: str) -> Any:
        def build(**kwargs: Any) -> object:
            seen[name] = kwargs
            return object()

        return build

    import tripl.core.adapters.clickhouse as ch
    import tripl.core.adapters.postgres as pg

    monkeypatch.setattr(pg, "PostgresAdapter", fake("postgres"))
    monkeypatch.setattr(ch, "ClickHouseAdapter", fake("clickhouse"))
    monkeypatch.setattr(registry, "decrypt_value", lambda _value: "pw")
    return seen


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1"]
)
def test_hosted_refuses_a_private_warehouse_host(hosted: None, host: str) -> None:
    with pytest.raises(WarehouseCapabilityError, match="private or internal address"):
        registry.vetted_address(_source("postgres", host))


def test_hosted_refuses_a_name_that_resolves_privately(
    hosted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``postgres`` is the instance's own database inside compose."""
    import tripl.services.safe_http as safe_http

    monkeypatch.setattr(
        safe_http, "_resolve", lambda *_a, **_k: [(0, 0, 0, "", ("172.18.0.3", 5432))]
    )
    with pytest.raises(WarehouseCapabilityError):
        registry.vetted_address(_source("postgres", "postgres"))


def test_postgres_connects_to_the_vetted_address(
    hosted: None, captured: dict[str, dict[str, Any]]
) -> None:
    registry.build_adapter(_source("postgres", "8.8.8.8"))
    assert captured["postgres"]["host"] == "8.8.8.8"
    assert captured["postgres"]["hostaddr"] == "8.8.8.8"


def test_clickhouse_connects_to_the_vetted_address_and_verifies_the_name(
    hosted: None, captured: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    import tripl.services.safe_http as safe_http

    monkeypatch.setattr(safe_http, "_resolve", lambda *_a, **_k: [(0, 0, 0, "", ("8.8.4.4", 8443))])
    registry.build_adapter(_source("clickhouse", "ch.example.com", 8443))
    assert captured["clickhouse"]["host"] == "8.8.4.4"
    assert captured["clickhouse"]["server_host_name"] == "ch.example.com"


def test_self_hosted_keeps_internal_warehouses(captured: dict[str, dict[str, Any]]) -> None:
    """An operator may point at an internal warehouse on purpose."""
    registry.build_adapter(_source("postgres", "postgres"))
    registry.build_adapter(_source("clickhouse", "clickhouse", 8123))
    assert captured["postgres"]["hostaddr"] is None
    assert captured["clickhouse"]["host"] == "clickhouse"
    assert "server_host_name" not in captured["clickhouse"]


def test_the_refusal_reads_as_a_connection_test_answer(
    hosted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "decrypt_value", lambda _value: "pw")
    ok, message = _run_adapter_test(_source("postgres", "127.0.0.1"))
    assert not ok
    assert message.startswith("Connection test failed: this instance only connects")


def test_hosted_bigquery_key_only_exchanges_tokens_with_google(
    hosted: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google.oauth2 import service_account

    from tripl.core.adapters import bigquery

    seen: list[dict[str, Any]] = []

    def capture(info: dict[str, Any]) -> None:
        seen.append(info)
        raise RuntimeError("stop before a client is built")

    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", capture)
    key = {"type": "service_account", "token_uri": "http://169.254.169.254/token"}
    with pytest.raises(RuntimeError, match="stop"):
        bigquery.BigQueryAdapter(
            host="proj", port=443, database="ds", username="", password=json.dumps(key)
        )
    assert seen[0]["token_uri"] == GOOGLE_TOKEN_URI


class TestPublicDemoWarehouses:
    @pytest.fixture(autouse=True)
    def public_demo(self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """After ``client`` has signed up: a public demo takes no password sign-ups."""
        monkeypatch.setattr(settings, "public_demo", True)

    async def test_no_new_warehouse(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/data-sources",
            json={"name": "own", "db_type": "postgres", "host": "postgres", "database_name": "x"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == (
            "This public demo does not connect to warehouses of your own."
        )

    async def test_no_unsaved_probe(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/data-sources/test",
            json={"db_type": "postgres", "host": "postgres", "database_name": "x"},
        )
        assert resp.status_code == 403

    async def test_no_edit_of_a_source(self, client: AsyncClient) -> None:
        resp = await client.patch(f"/api/v1/data-sources/{uuid.uuid4()}", json={"host": "redis"})
        assert resp.status_code == 403
