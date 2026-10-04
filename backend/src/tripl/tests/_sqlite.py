"""SQLite pragmas that make a test engine enforce what production enforces.

Not a test module — a helper the async ``conftest`` engine and the sync
per-module engines both import, so there is exactly one definition of "the test
database behaves like the real one".
"""

from __future__ import annotations

from sqlalchemy import Engine, event


def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Set ``PRAGMA foreign_keys=ON`` on every connection ``engine`` opens.

    SQLite parses ``FOREIGN KEY`` clauses, stores them, and then ignores them:
    enforcement is off by default and the pragma is per-connection. Production
    is Postgres, which always enforces. A cascade the production database
    enforces and the test database silently does not is a test that cannot
    fail — it asserts the survival of a row that only survives because nothing
    was checking.

    That is not hypothetical. ``_event_generator_merge._merge_event_into_group``
    deletes the merged-away source event, and ``variable_values.event_id`` is
    ``ondelete="CASCADE"``. ``Event`` maps no ``variable_values`` relationship,
    so SQLAlchemy issues no ORM-side cascade either. Without this pragma a test
    that seeds a context, merges, and asserts "the context is still there"
    passes both before and after the fix that actually migrates it
    — the row simply never got deleted in the first place. Turning enforcement
    on is what gives the assertion the power to fail.

    Call this immediately after ``create_engine``, BEFORE ``create_all`` or any
    other statement. In-memory SQLite pools a single connection, so a listener
    registered after the first checkout never runs and the pragma is never set.

    For an async engine, pass ``engine.sync_engine``.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
