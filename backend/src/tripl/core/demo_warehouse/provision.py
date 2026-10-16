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
* The reader role (``<warehouse database>_reader``) needs CREATEROLE. It logs
  in, owns nothing, holds at most 10 connections (the server's
  ``max_connections`` is shared with tripl's own pool), and its defaults are
  read-only with a 15 s statement timeout, a 30 s idle-in-transaction timeout,
  16 MB of work memory, no parallel workers and 256 MB of temporary files.
  ``temp_file_limit`` is superuser-only, which is the point: the session
  cannot lift it, and without a superuser it is skipped with a warning (the
  status then says the reader runs without it). The other defaults a session
  CAN change; the adapter re-applies them before every statement
  (:mod:`tripl.core.adapters.demo_postgres`). The password is sent as a
  SCRAM-SHA-256 verifier, never in plain text. Skipped entirely when
  ``DEMO_WAREHOUSE_READER_PASSWORD`` says the role was provisioned by hand.
* In the demo database: CONNECT and TEMPORARY are revoked from PUBLIC and
  CONNECT granted to the reader alone; ``public`` loses its PUBLIC privileges
  (checked afterwards: on PostgreSQL 14 a non-owner's REVOKE only warns); the
  reader gets USAGE on the ``warehouse`` schema and SELECT on its two tables,
  nothing else (not their partitions, so the SQL editor's table list stays at
  two; reading through the parent checks only the parent's grant). PUBLIC
  loses EXECUTE on the functions that reach other sessions — cancelling or
  terminating a backend, advisory locks — and SELECT on ``pg_stat_activity``,
  inside the demo database only: every visitor shares the one reader role, and
  sessions of one role may see and cancel each other's queries. PUBLIC loses
  EXECUTE on ``set_config``, ``pg_sleep*``, the functions that run a query
  given as text and the large-object functions there too
  (:data:`REVOKED_FUNCTION_PATTERNS`). That needs a superuser (the functions
  belong to the bootstrap superuser), and a REVOKE without one only warns, so
  the reader's privileges are checked
  afterwards: whatever it can still use is a warning naming it, the warehouse
  status lists it (``reader_limits_missing``), and the adapter's own denylist
  still refuses those calls.

Nothing is granted or revoked in tripl's own database. The reader can open a
connection there (PostgreSQL grants CONNECT to PUBLIC by default) but reads
nothing: tripl grants no table privilege to PUBLIC. Revoking PUBLIC's CONNECT
on the main database would also lock out every role an operator created for
their own purposes (a backup user, a read replica's monitor), so it is left
alone on purpose.
"""

from __future__ import annotations

import contextlib
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
    ("hash_mem_multiplier", "1"),
    ("max_parallel_workers_per_gather", "0"),
    ("search_path", schema.WAREHOUSE_SCHEMA),
    ("temp_file_limit", "256MB"),
)
READER_CONNECTION_LIMIT = 10
#: Functions PUBLIC may not execute in the demo database, as anchored POSIX
#: regular expressions over ``pg_proc.proname`` in ``pg_catalog`` (every
#: overload). Each one either acts on OTHER sessions of the shared reader role
#: (cancel, terminate, read their activity, statistics, progress or locks, hold
#: or wait on advisory locks, sleep on a connection: the views over them,
#: ``pg_locks`` and the ``pg_stat_*`` family, call them as the reader and are
#: refused with them), changes the session's settings (``set_config``),
#: runs a query given as text, which no check of the statement's own text can
#: see into (the ``*_to_xml`` family, ``ts_stat``, ``ts_rewrite``), or reaches
#: large objects. The adapter's SQL check refuses the same names; this is the
#: layer that holds for whatever text reaches the server. The adapter's ``SET``
#: pins need none of these: ``SET`` is a statement, not a call.
REVOKED_FUNCTION_PATTERNS: tuple[str, ...] = (
    "set_config",
    "pg_cancel_backend",
    "pg_terminate_backend",
    "pg_stat_get_.*",
    "pg_lock_status",
    "pg_blocking_pids",
    "pg_safe_snapshot_blocking_pids",
    "pg_(try_)?advisory_.*",
    "pg_sleep.*",
    "pg_notify",
    "(query|cursor|table|schema|database)_to_xml.*",
    "ts_stat",
    "ts_rewrite",
    "lo_.*",
    "loread",
    "lowrite",
)
#: One expression for the whole list, for ``proname ~ %s``.
REVOKED_FUNCTIONS_RE = "^(" + "|".join(REVOKED_FUNCTION_PATTERNS) + ")$"
#: Views PUBLIC may not read in the demo database (other sessions' query text
#: and locks), besides the functions under them.
REVOKED_VIEWS: tuple[str, ...] = ("pg_stat_activity", "pg_locks")
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
    role_name = connection.reader_role()
    role = sql.Identifier(role_name)
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
                role_name,
            )
            return
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        # Re-applied on every run, so a rotated SECRET_KEY heals the password.
        # A SCRAM verifier, not the password: the statement may be logged.
        conn.execute(
            sql.SQL(
                "{} ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                "NOINHERIT CONNECTION LIMIT {} PASSWORD {}"
            ).format(
                sql.SQL(verb),
                role,
                sql.Literal(READER_CONNECTION_LIMIT),
                sql.Literal(connection.scram_verifier(password)),
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
                    role_name,
                )
    result.reader_ready = True


def _ensure_schema_and_grants(result: ProvisionResult) -> None:
    writer = connection.writer_connection()
    reader_name = connection.reader_role()
    reader = sql.Identifier(reader_name)
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
        _revoke_session_functions(conn, result, reader_name)
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
        _check_public_schema(conn, result, reader_name)
    result.schema_ready = True


def _revoke_session_functions(
    conn: psycopg.Connection[tuple[object, ...]], result: ProvisionResult, reader: str
) -> None:
    """Take from PUBLIC what lets one reader session reach another (demo db only).

    Checked afterwards, not trusted: a REVOKE by a role that does not own the
    function (only a superuser owns the catalog's) raises nothing, it only
    warns that nothing was revoked.
    """
    signatures = conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'pg_catalog' AND p.proname ~ %s",
        (REVOKED_FUNCTIONS_RE,),
    ).fetchall()
    statements: list[sql.Composed] = [
        sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM PUBLIC").format(sql.SQL(str(signature)))
        for (signature,) in signatures
    ]
    statements.extend(
        sql.SQL("REVOKE SELECT ON {} FROM PUBLIC").format(sql.Identifier("pg_catalog", view))
        for view in REVOKED_VIEWS
    )
    for statement in statements:
        # Refused or not, what is still reachable is reported below.
        with contextlib.suppress(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
    try:
        reachable = reader_reachable_session_functions(conn, reader)
    except psycopg.Error as exc:
        result.warn("could not check %s's privileges: %s", reader, type(exc).__name__)
        return
    if reachable:
        result.warn(
            "%s can still use %s in the demo database: revoking them from PUBLIC needs a "
            "superuser (or the extension's owner). The demo adapter still refuses the "
            "catalog's in the statements it sends; an administrator should run, "
            "connected to the demo database, REVOKE EXECUTE ON FUNCTION <each> FROM "
            "PUBLIC and REVOKE SELECT ON <each view> FROM PUBLIC.",
            reader,
            ", ".join(reachable),
        )


def reader_reachable_session_functions(
    conn: psycopg.Connection[tuple[object, ...]], reader: str
) -> tuple[str, ...]:
    """What of :data:`REVOKED_FUNCTION_PATTERNS` and :data:`REVOKED_VIEWS` ``reader`` can use.

    Also every function outside the catalog it can call: the warehouse defines
    none, so one there is an extension's (``dblink``, ``pg_stat_statements``),
    installed after provisioning and listed nowhere else.

    ``conn`` is on the demo database (privileges on the catalog are per
    database). Signatures for functions, qualified names for views; empty when
    the role does not exist (it is joined, not cast, so a missing role is no
    error).
    """
    rows = conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "JOIN pg_roles r ON r.rolname = %s "
        "WHERE n.nspname = 'pg_catalog' AND p.proname ~ %s "
        "AND has_function_privilege(r.oid, p.oid, 'EXECUTE') "
        "UNION ALL "
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "JOIN pg_roles r ON r.rolname = %s "
        "WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_' "
        "AND has_schema_privilege(r.oid, n.oid, 'USAGE') "
        "AND has_function_privilege(r.oid, p.oid, 'EXECUTE') "
        "UNION ALL "
        "SELECT 'pg_catalog.' || c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_roles r ON r.rolname = %s "
        "WHERE n.nspname = 'pg_catalog' AND c.relname = ANY(%s) "
        "AND has_table_privilege(r.oid, c.oid, 'SELECT') "
        "ORDER BY 1",
        (reader, REVOKED_FUNCTIONS_RE, reader, reader, list(REVOKED_VIEWS)),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _check_public_schema(
    conn: psycopg.Connection[tuple[object, ...]], result: ProvisionResult, reader: str
) -> None:
    """Warn when the reader can still create in ``public`` or use temporary tables.

    On PostgreSQL 14 a REVOKE on a schema the writer does not own only raises a
    WARNING, so the step above can look done while PUBLIC keeps CREATE.
    """
    try:
        row = conn.execute(
            "SELECT has_schema_privilege(%s, 'public', 'CREATE'), "
            "has_database_privilege(%s, current_database(), 'TEMPORARY')",
            (reader, reader),
        ).fetchone()
    except psycopg.Error as exc:  # a hand-provisioned role under another name
        result.warn("could not check %s's privileges: %s", reader, type(exc).__name__)
        return
    if row is None:
        return
    can_create, can_temp = bool(row[0]), bool(row[1])
    if can_create:
        result.warn(
            "%s can still CREATE in the demo database's public schema; an administrator "
            "should run REVOKE CREATE ON SCHEMA public FROM PUBLIC there.",
            reader,
        )
    if can_temp:
        result.warn(
            "%s can still create temporary tables in the demo database; an administrator "
            "should run REVOKE TEMPORARY ON DATABASE <demo database> FROM PUBLIC.",
            reader,
        )


def provision() -> ProvisionResult:
    """Create or repair the warehouse database, reader role, schema and grants.

    Returns what is ready; never raises (a failure is a warning, and demos fall
    back to the in-memory engine).
    """
    result = ProvisionResult()
    if connection.withheld_from_public_demo():
        logger.warning(
            "demo_warehouse.provision skipped: %s; demos use the in-memory engine",
            connection.PUBLIC_DEMO_NEEDS_OWN_SERVER,
        )
        return result
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
