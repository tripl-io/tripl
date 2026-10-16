"""Create the demo warehouse database, its reader role and its schema (idempotent).

``python -m tripl.migrate`` runs :func:`provision` after the core migrations.
It never fails the migration: a step tripl's role is not allowed to take is
logged as a warning naming what an administrator must do, and every demo then
runs on the in-memory engine until it is done.

What it takes, and why:

* ``CREATE DATABASE <main>_demo`` needs CREATEDB (the compose superuser has it).
  The database uses the ``C`` collation, so PostgreSQL sorts text by code point
  exactly as Python does (the in-memory engine's JSON key lists agree with it).
  Skipped when ``DEMO_WAREHOUSE_URL`` names a database provisioned by hand.
* The reader role needs CREATEROLE. It logs in, owns nothing, holds at most 20
  connections, and its defaults are read-only with a 15 s statement timeout,
  a 30 s idle-in-transaction timeout, 16 MB of work memory and 256 MB of
  temporary files. ``temp_file_limit`` is superuser-only, which is the point:
  the session cannot lift it, and without a superuser it is skipped with a
  warning. Skipped entirely when ``DEMO_WAREHOUSE_READER_PASSWORD`` says the
  role was provisioned by hand.
* In the demo database: CONNECT and TEMPORARY are revoked from PUBLIC and
  CONNECT granted to the reader alone; ``public`` loses its PUBLIC privileges;
  the reader gets USAGE on the ``warehouse`` schema and SELECT on its two
  tables, nothing else (not their partitions, so the SQL editor's table list
  stays at two; reading through the parent checks only the parent's grant).

Nothing is granted or revoked in tripl's own database. The reader can open a
connection there (PostgreSQL grants CONNECT to PUBLIC by default) but reads
nothing: tripl grants no table privilege to PUBLIC. Revoking PUBLIC's CONNECT
on the main database would also lock out every role an operator created for
their own purposes (a backup user, a read replica's monitor), so it is left
alone on purpose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg
from psycopg import sql

from tripl.config import settings
from tripl.core.demo_warehouse import connection, schema

logger = logging.getLogger(__name__)

#: ``ALTER ROLE <reader> SET`` defaults. ``temp_file_limit`` last: it is the one
#: that needs a superuser.
READER_ROLE_SETTINGS: tuple[tuple[str, str], ...] = (
    ("default_transaction_read_only", "on"),
    ("statement_timeout", "15s"),
    ("idle_in_transaction_session_timeout", "30s"),
    ("work_mem", "16MB"),
    ("search_path", schema.WAREHOUSE_SCHEMA),
    ("temp_file_limit", "256MB"),
)
READER_CONNECTION_LIMIT = 20
CONNECT_TIMEOUT_SECONDS = 10


@dataclass
class ProvisionResult:
    """What :func:`provision` managed to do; ``warnings`` say what it could not."""

    database_ready: bool = False
    reader_ready: bool = False
    schema_ready: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.database_ready and self.reader_ready and self.schema_ready

    def warn(self, message: str, *args: object) -> None:
        text = message % args if args else message
        self.warnings.append(text)
        logger.warning("demo_warehouse.provision %s", text)


def _privileges(conn: psycopg.Connection[tuple[object, ...]]) -> tuple[bool, bool, bool]:
    """``(superuser, createdb, createrole)`` of the connected role."""
    row = conn.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if row is None:
        return False, False, False
    return bool(row[0]), bool(row[1]), bool(row[2])


def _ensure_database(result: ProvisionResult) -> None:
    writer = connection.writer_connection()
    main = connection.main_connection()
    with psycopg.connect(
        **main.kwargs(connect_timeout=CONNECT_TIMEOUT_SECONDS),  # type: ignore[arg-type]
        autocommit=True,
    ) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (writer.database,)
        ).fetchone()
        if exists is None:
            superuser, createdb, _ = _privileges(conn)
            if not (superuser or createdb):
                result.warn(
                    "the database role lacks CREATEDB, so the demo warehouse database %r was "
                    "not created; an administrator can create it (owned by this role, "
                    "LC_COLLATE 'C') and run migrate again. Demos use the in-memory engine.",
                    writer.database,
                )
                return
            # CREATE DATABASE cannot run in a transaction: autocommit above.
            conn.execute(
                sql.SQL(
                    "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' "
                    "LC_COLLATE 'C' LC_CTYPE 'C'"
                ).format(sql.Identifier(writer.database))
            )
            logger.info("demo_warehouse.provision created database %s", writer.database)
    result.database_ready = True


def _ensure_reader(result: ProvisionResult) -> None:
    password = connection.reader_password()
    if password is None:
        result.warn(
            "SECRET_KEY is empty, so no reader password can be derived; set SECRET_KEY "
            "or DEMO_WAREHOUSE_READER_PASSWORD. Demos use the in-memory engine."
        )
        return
    if not connection.reader_is_managed():
        result.reader_ready = True
        return
    role = sql.Identifier(connection.READER_ROLE)
    # Roles are cluster-wide; through the warehouse database, so a
    # DEMO_WAREHOUSE_URL on another server gets the role on THAT server.
    writer = connection.writer_connection()
    with psycopg.connect(
        **writer.kwargs(connect_timeout=CONNECT_TIMEOUT_SECONDS),  # type: ignore[arg-type]
        autocommit=True,
    ) as conn:
        superuser, _, createrole = _privileges(conn)
        if not (superuser or createrole):
            result.warn(
                "the database role lacks CREATEROLE, so the reader role %s was not managed; "
                "create it by hand and set DEMO_WAREHOUSE_READER_PASSWORD.",
                connection.READER_ROLE,
            )
            return
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (connection.READER_ROLE,)
        ).fetchone()
        verb = "ALTER" if exists else "CREATE"
        # Re-applied on every run, so a rotated SECRET_KEY heals the password.
        conn.execute(
            sql.SQL(
                "{} ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                "NOINHERIT CONNECTION LIMIT {} PASSWORD {}"
            ).format(
                sql.SQL(verb), role, sql.Literal(READER_CONNECTION_LIMIT), sql.Literal(password)
            )
        )
        for name, value in READER_ROLE_SETTINGS:
            try:
                conn.execute(
                    sql.SQL("ALTER ROLE {} SET {} TO {}").format(
                        role, sql.Identifier(name), sql.Literal(value)
                    )
                )
            except psycopg.errors.InsufficientPrivilege:
                result.warn(
                    "could not set %s on %s (it needs a superuser); the reader runs "
                    "without that limit.",
                    name,
                    connection.READER_ROLE,
                )
    result.reader_ready = True


def _ensure_schema_and_grants(result: ProvisionResult) -> None:
    writer = connection.writer_connection()
    reader = sql.Identifier(connection.READER_ROLE)
    database = sql.Identifier(writer.database)
    with psycopg.connect(
        **writer.kwargs(connect_timeout=CONNECT_TIMEOUT_SECONDS),  # type: ignore[arg-type]
        autocommit=True,
    ) as conn:
        for ddl in schema.schema_ddl():
            conn.execute(ddl)
        hardening: list[sql.Composed] = [
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database),
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier("public")),
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(
                sql.Identifier(schema.META_SCHEMA)
            ),
            sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(
                sql.Identifier(schema.WAREHOUSE_SCHEMA)
            ),
        ]
        for revoke in hardening:
            try:
                conn.execute(revoke)
            except psycopg.errors.InsufficientPrivilege:
                result.warn("could not run %r in the demo database.", revoke.as_string(conn))
        if not result.reader_ready:
            return
        grants = [
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, reader),
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema.WAREHOUSE_SCHEMA), reader
            ),
            *(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    sql.Identifier(schema.WAREHOUSE_SCHEMA, table), reader
                )
                for table in schema.TABLE_COLUMNS
            ),
        ]
        for grant in grants:
            conn.execute(grant)
    result.schema_ready = True


def provision() -> ProvisionResult:
    """Create or repair the warehouse database, reader role, schema and grants.

    Returns what is ready; never raises (a failure is a warning, and demos fall
    back to the in-memory engine).
    """
    result = ProvisionResult()
    try:
        if settings.demo_warehouse_url:
            # Provisioned by hand: the database exists (or connecting fails below).
            result.database_ready = True
        else:
            _ensure_database(result)
        if not result.database_ready:
            return result
        _ensure_reader(result)
        _ensure_schema_and_grants(result)
    except psycopg.Error as exc:
        result.warn("stopped: %s: %s", type(exc).__name__, exc)
    if result.ready:
        logger.info(
            "demo_warehouse.provision ready database=%s", connection.writer_connection().database
        )
    return result
