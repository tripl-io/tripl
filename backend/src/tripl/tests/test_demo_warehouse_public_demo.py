"""A public demo keeps its visitors' SQL off the server tripl's database is on.

``connection.withheld_from_public_demo``: PostgreSQL bounds a statement's time
but not its memory, so ``auto`` neither provisions, writes nor reads a
warehouse there for a public demo; one on a server of its own serves it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import psycopg
import pytest

from tripl.config import settings
from tripl.core.demo_warehouse import connection, engine, provision, status, writer
from tripl.tests._tenancy import use_public_demo, use_single_team

_MAIN = "postgresql+psycopg://tripl:pw@db/tripl"
_OWN_SERVER = "postgresql://tripl_demo:pw@demo-warehouse-db:5432/tripl_demo"


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "sync_database_url", _MAIN)
    monkeypatch.setattr(settings, "demo_warehouse_url", "")
    monkeypatch.setattr(settings, "demo_warehouse_mode", "auto")
    status.clear_cache()
    yield
    status.clear_cache()


def _refuse_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("connected to a PostgreSQL server")

    monkeypatch.setattr(psycopg, "connect", refuse)


def test_a_public_demo_never_touches_a_warehouse_on_tripls_server(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    use_public_demo(monkeypatch)
    _refuse_connections(monkeypatch)
    assert connection.withheld_from_public_demo()

    current = status.warehouse_status(refresh=True)
    assert current.engine == "memory"
    assert current.reason == connection.PUBLIC_DEMO_NEEDS_OWN_SERVER
    assert engine.engine_of(engine.build_demo_adapter()) == "memory"
    assert writer.write() is None
    with caplog.at_level(logging.WARNING, logger=provision.__name__):
        assert not provision.provision().ready
    assert "DEMO_WAREHOUSE_URL" in caplog.text


def test_a_warehouse_on_a_server_of_its_own_serves_a_public_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_public_demo(monkeypatch)
    monkeypatch.setattr(settings, "demo_warehouse_url", _OWN_SERVER)
    assert not connection.on_tripl_server()
    assert not connection.withheld_from_public_demo()


@pytest.mark.parametrize(
    "url",
    [
        # Another database, even another role, on the same host and port.
        "postgresql://someone:pw@db:5432/tripl_demo",
        "postgresql://someone:pw@db/elsewhere",
    ],
)
def test_an_override_on_tripls_own_server_is_still_withheld(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    use_public_demo(monkeypatch)
    monkeypatch.setattr(settings, "demo_warehouse_url", url)
    assert connection.on_tripl_server()
    assert connection.withheld_from_public_demo()


def test_only_auto_mode_on_a_public_demo_is_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    use_single_team(monkeypatch)
    assert not connection.withheld_from_public_demo()
    use_public_demo(monkeypatch)
    # DEMO_WAREHOUSE_MODE=postgres is the operator's explicit choice.
    monkeypatch.setattr(settings, "demo_warehouse_mode", "postgres")
    assert not connection.withheld_from_public_demo()
