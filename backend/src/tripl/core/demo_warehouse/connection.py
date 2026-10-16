"""Where the demo warehouse is, and who may connect to it.

The warehouse is a separate database on the PostgreSQL server tripl already
uses: ``<main database>_demo`` (``tripl_demo`` in compose), written by tripl's
own role and read through :func:`reader_role` (``<warehouse database>_reader``,
``tripl_demo_reader`` in compose), a login role that can read the two warehouse
tables and nothing else. Roles are cluster-wide, so the name follows the
database: two instances sharing one PostgreSQL server (staging and production,
say) each manage their own reader instead of overwriting one password.

Every connection parameter comes from settings, never from a data source row:
a ``demo`` source's host, port and credential columns are ignored, so nothing a
user can edit reaches a connection that skips the private-address guard.

* ``DEMO_WAREHOUSE_URL`` overrides the writer's DSN (a database provisioned by
  hand, e.g. on managed PostgreSQL); by default it is ``SYNC_DATABASE_URL`` with
  the database name swapped.
* ``DEMO_WAREHOUSE_READER_PASSWORD`` is the password of a reader role
  provisioned by hand; by default it is derived from ``SECRET_KEY`` (HMAC, so
  it is stable, never stored, and changes with the key: ``migrate`` re-applies
  it on every run).

A public demo needs the override to name a server of its own
(:func:`withheld_from_public_demo`): there, strangers' SQL runs on the
warehouse, and PostgreSQL bounds a statement's time but not its memory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from sqlalchemy.engine import make_url

from tripl import tenancy
from tripl.config import settings

DATABASE_SUFFIX = "_demo"
READER_ROLE_SUFFIX = "_reader"
#: Every connection the writer, provisioning and status open runs in UTC: the
#: writer derives day partitions from ``written_through``, which psycopg hands
#: back in the SESSION's time zone. On a server whose default is not UTC a
#: partition boundary would land off UTC midnight and collide with its neighbour.
UTC_OPTIONS = "-c TimeZone=UTC"
#: PostgreSQL's own SCRAM iteration count (``scram_iterations``' default).
SCRAM_ITERATIONS = 4096
_READER_PASSWORD_CONTEXT = b"demo-warehouse-reader"
#: libpq's default port, when a DSN names none.
_DEFAULT_PORT = 5432


@dataclass(frozen=True)
class WarehouseConnection:
    """libpq connection parameters. ``repr`` hides the password."""

    host: str
    port: int
    database: str
    user: str
    password: str
    sslmode: str | None

    def __repr__(self) -> str:
        return (
            f"WarehouseConnection(host={self.host!r}, port={self.port}, "
            f"database={self.database!r}, user={self.user!r}, sslmode={self.sslmode!r})"
        )

    def kwargs(self, *, connect_timeout: int | None = None) -> dict[str, object]:
        """Keyword arguments for ``psycopg.connect`` (a UTC session)."""
        params: dict[str, object] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": self.password,
            "options": UTC_OPTIONS,
        }
        if self.sslmode:
            params["sslmode"] = self.sslmode
        if connect_timeout:
            params["connect_timeout"] = connect_timeout
        return params


def _parse(dsn: str, *, database: str | None = None) -> WarehouseConnection:
    url = make_url(dsn)
    sslmode = url.query.get("sslmode")
    return WarehouseConnection(
        host=url.host or "localhost",
        port=url.port or _DEFAULT_PORT,
        database=database or url.database or "",
        user=url.username or "",
        password=url.password or "",
        sslmode=sslmode if isinstance(sslmode, str) else None,
    )


def main_connection() -> WarehouseConnection:
    """tripl's own database, as the app role (provisioning connects here)."""
    return _parse(settings.sync_database_url)


def main_database_name() -> str:
    return main_connection().database


def writer_connection() -> WarehouseConnection:
    """The warehouse database as its writer (tripl's role, or the override's)."""
    if settings.demo_warehouse_url:
        return _parse(settings.demo_warehouse_url)
    main = main_connection()
    return _parse(settings.sync_database_url, database=f"{main.database}{DATABASE_SUFFIX}")


def on_tripl_server() -> bool:
    """Whether the warehouse database is on the server tripl's own database is on.

    Compared by host and port as configured, so an alias of the same server
    passes for another one: the check catches the default and a copied DSN, not
    a determined operator.
    """
    writer, main = writer_connection(), main_connection()
    return (writer.host, writer.port) == (main.host, main.port)


def withheld_from_public_demo() -> bool:
    """Whether a public demo must leave the warehouse alone (in-memory engine only).

    A public demo runs its visitors' SQL as the reader. ``statement_timeout``,
    ``work_mem`` and ``temp_file_limit`` bound a statement's time, sorts and
    spills, but nothing in PostgreSQL bounds the memory of an aggregate's state
    or of a long value, so one statement can take a gigabyte a column; on the
    server tripl's own database lives on, the kernel's OOM killer would then
    restart that database too. ``auto`` therefore neither provisions, writes nor
    reads a warehouse on tripl's server for a public demo: it needs
    ``DEMO_WAREHOUSE_URL`` on a server of its own, whose memory limit contains
    the damage. ``DEMO_WAREHOUSE_MODE=postgres`` overrides this (the operator's
    explicit choice).
    """
    return settings.demo_warehouse_mode == "auto" and tenancy.public_demo() and on_tripl_server()


#: The in-memory reason :func:`withheld_from_public_demo` gives, for the status.
PUBLIC_DEMO_NEEDS_OWN_SERVER = (
    "a public demo runs its visitors' SQL on the warehouse, so it needs a PostgreSQL "
    "server of its own (DEMO_WAREHOUSE_URL) rather than the one tripl's database is on"
)


def reader_role() -> str:
    """The reader role's name: the warehouse database's name plus ``_reader``."""
    return f"{writer_connection().database}{READER_ROLE_SUFFIX}"


def reader_password() -> str | None:
    """The reader role's password, or ``None`` when none can be had safely.

    Without ``SECRET_KEY`` the derived password would be a constant anyone can
    compute, so the reader is then neither provisioned nor used.
    """
    if settings.demo_warehouse_reader_password:
        return settings.demo_warehouse_reader_password
    if not settings.secret_key:
        return None
    return hmac.new(
        settings.secret_key.encode(), _READER_PASSWORD_CONTEXT, hashlib.sha256
    ).hexdigest()


def reader_connection() -> WarehouseConnection | None:
    """The warehouse database as :func:`reader_role`; ``None`` without a password."""
    password = reader_password()
    if password is None:
        return None
    writer = writer_connection()
    return WarehouseConnection(
        host=writer.host,
        port=writer.port,
        database=writer.database,
        user=reader_role(),
        password=password,
        sslmode=writer.sslmode,
    )


def reader_is_managed() -> bool:
    """Whether ``migrate`` manages the reader role (it does unless one was provisioned by hand)."""
    return not settings.demo_warehouse_reader_password


def scram_verifier(password: str, *, salt: bytes | None = None) -> str:
    """A SCRAM-SHA-256 verifier of ``password``, as ``pg_authid`` stores it.

    ``ALTER ROLE ... PASSWORD`` with a verifier instead of the password keeps
    the plaintext out of the server's statement log (``log_statement=ddl``) and
    out of ``pg_stat_activity``: PostgreSQL recognises the format and stores it
    as given. The password is ASCII (a hex digest, or what an operator set), so
    SASLprep leaves it unchanged.
    """
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, SCRAM_ITERATIONS)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode()

    return f"SCRAM-SHA-256${SCRAM_ITERATIONS}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"
