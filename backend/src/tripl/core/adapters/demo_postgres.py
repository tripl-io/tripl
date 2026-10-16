"""The demo warehouse's PostgreSQL engine: ``PostgresAdapter`` as the reader role.

It connects to the demo database (:mod:`tripl.core.demo_warehouse.connection`)
as the reader role (:func:`~tripl.core.demo_warehouse.connection.reader_role`), with every
parameter taken from settings, so the private-address guard has nothing to
vet: the host is tripl's own database server and no user can change it.

The reader role cannot write and can read only the two warehouse tables (see
:mod:`tripl.core.demo_warehouse.provision`). What it CAN do is change its own
session settings: ``SELECT set_config('statement_timeout', '0', false)`` is a
plain query, so a scan's base query could switch off the timeout, the read-only
default or the quoting rule our literals rely on for every statement after it.
So this adapter re-arms all of them before EVERY statement it runs: the cursor
sends the pins and then the statement. A setting changed inside one statement
therefore cannot outlive it. Besides PostgresAdapter's own pins that includes
the memory and parallelism limits (:data:`READER_RESOURCE_PINS`): spilling to
disk is bounded by ``temp_file_limit``, but work done in memory is not, and the
server tripl's own database lives on is the one that would run out.
``temp_file_limit`` needs no re-arming: only a superuser may change it, so the
role's value holds.

It also refuses, before sending, any statement that calls what the shared
reader must not (``check_demo_warehouse_sql``): another session's queries,
advisory locks, ``set_config`` and the like.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql

from tripl.core.adapters.measure_validator import check_demo_warehouse_sql
from tripl.core.adapters.postgres import PostgresAdapter
from tripl.core.demo_warehouse import schema

#: The reader's statement timeout, also set on the role (provision.py).
READER_STATEMENT_TIMEOUT_SECONDS = 15
#: Re-armed before every statement with the session pins. The role carries the
#: same defaults (provision.py), but a session may change them.
READER_RESOURCE_PINS: tuple[tuple[str, str], ...] = (
    ("work_mem", "16MB"),
    ("hash_mem_multiplier", "1"),
    ("max_parallel_workers_per_gather", "0"),
    ("max_parallel_workers", "0"),
    ("idle_in_transaction_session_timeout", "30s"),
)


def _rearming_cursor(pins: str) -> type[psycopg.Cursor[Any]]:
    class RearmingCursor(psycopg.Cursor[Any]):
        """Runs the session pins, then the statement, on every ``execute``."""

        def execute(
            self,
            query: Any,
            params: Any = None,
            *,
            prepare: bool | None = None,
            binary: bool | None = None,
        ) -> RearmingCursor:
            text = query.as_string(self.connection) if isinstance(query, sql.Composable) else query
            check_demo_warehouse_sql(text.decode() if isinstance(text, bytes) else str(text))
            # Never prepared: the pins are several statements, which only the
            # simple query protocol runs (psycopg would prepare a string it
            # has seen five times, and a prepared statement holds one).
            super().execute(pins, prepare=False)
            super().execute(query, params, prepare=prepare, binary=binary)
            return self

    return RearmingCursor


class DemoPostgresAdapter(PostgresAdapter):
    """``PostgresAdapter`` on the demo warehouse, as the reader role."""

    #: Which engine serves a ``demo`` source (shown on its connection status).
    engine = "postgres"

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        username: str,
        password: str,
        timeout_seconds: int | None = None,
        sslmode: str | None = None,
    ) -> None:
        statement_timeout = min(
            timeout_seconds or READER_STATEMENT_TIMEOUT_SECONDS, READER_STATEMENT_TIMEOUT_SECONDS
        )
        super().__init__(
            host=host,
            port=port,
            database=database,
            username=username,
            password=password,
            timeout_seconds=statement_timeout,
            # The in-stack server speaks plaintext on the private network; a
            # DEMO_WAREHOUSE_URL that names a mode keeps it.
            sslmode=sslmode or "disable",
            search_path=schema.WAREHOUSE_SCHEMA,
        )
        pins = "; ".join(
            sql.SQL("SET {} TO {}")
            .format(sql.Identifier(name), sql.Literal(value))
            .as_string(self._conn)
            for name, value in (
                *self._session_settings(statement_timeout, schema.WAREHOUSE_SCHEMA),
                *READER_RESOURCE_PINS,
            )
        )
        self._conn.cursor_factory = _rearming_cursor(pins)
