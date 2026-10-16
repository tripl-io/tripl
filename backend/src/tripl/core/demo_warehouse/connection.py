"""Where the demo warehouse is, and who may connect to it.

The warehouse is a separate database on the PostgreSQL server tripl already
uses: ``<main database>_demo`` (``tripl_demo`` in compose), written by tripl's
own role and read through :data:`READER_ROLE`, a login role that can read the
two warehouse tables and nothing else.

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
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from sqlalchemy.engine import make_url

from tripl.config import settings

READER_ROLE = "tripl_demo_reader"
DATABASE_SUFFIX = "_demo"
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
        """Keyword arguments for ``psycopg.connect``."""
        params: dict[str, object] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": self.password,
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
    """The warehouse database as :data:`READER_ROLE`; ``None`` without a password."""
    password = reader_password()
    if password is None:
        return None
    writer = writer_connection()
    return WarehouseConnection(
        host=writer.host,
        port=writer.port,
        database=writer.database,
        user=READER_ROLE,
        password=password,
        sslmode=writer.sslmode,
    )


def reader_is_managed() -> bool:
    """Whether ``migrate`` manages the reader role (it does unless one was provisioned by hand)."""
    return not settings.demo_warehouse_reader_password
