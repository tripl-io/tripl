"""Amazon Athena (engine version 3), on the Trino SQL layer.

Athena engine version 3 is Trino: every statement :class:`TrinoAdapter` builds
runs there as it is, so this class replaces the CONNECTION and nothing else:

* **pyathena instead of the Trino client.** Each statement is a
  ``StartQueryExecution`` in the configured workgroup, polled until it ends, its
  rows read back through ``GetQueryResults``. The access key ID and secret are
  the source's user name and secret; the region comes from the host field and
  always resolves to ``athena.<region>.amazonaws.com``.
* **No session properties.** Athena has no ``query_max_run_time`` to set, so
  the deadline is the client's: a timer stops the query execution
  (``StopQueryExecution``) when the data source's timeout elapses, and the
  workgroup's own limits still apply on top. The session zone is UTC on Athena
  and cannot be changed; every literal and bucket names its zone anyway.
* **pyathena's parameter formatting is never reached.** No parameters are ever
  passed, so a ``%`` in a base query is sent as written.

Everything Athena lacks that Trino has is outside what the adapter emits. Not
verified against a live account: the release-tag workflow
``athena-value-conformance.yml`` runs the value suite once one is configured.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, override

from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.trino import TrinoAdapter

logger = logging.getLogger(__name__)

#: The data catalog Athena reads unless told otherwise (the Glue catalog).
DEFAULT_ATHENA_CATALOG = "AwsDataCatalog"

#: The workgroup every AWS account has.
DEFAULT_ATHENA_WORKGROUP = "primary"


class _Deadline:
    """Stop a running Athena query when the deadline passes."""

    def __init__(self, cursor: Any, seconds: float | None) -> None:
        self._cursor = cursor
        self.fired = False
        self._timer: threading.Timer | None = None
        if seconds is not None:
            self._timer = threading.Timer(seconds, self._stop)
            self._timer.daemon = True
            self._timer.start()

    def _stop(self) -> None:
        self.fired = True
        try:
            if getattr(self._cursor, "query_id", None):
                self._cursor.cancel()
        except Exception:
            logger.warning("Athena: could not stop a query past its deadline", exc_info=True)

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()


class AthenaAdapter(TrinoAdapter):
    """``TrinoAdapter`` for an Amazon Athena workgroup (engine version 3)."""

    engine_label = "Athena"

    _region: str = ""
    _work_group: str | None = None
    _s3_output: str | None = None

    def __init__(  # noqa: PLR0913 - one keyword per Athena connection setting
        self,
        host: str,
        port: int,  # always 443: the Athena API is reached over HTTPS
        database: str,
        username: str = "",
        password: str = "",
        *,
        region: str,
        work_group: str | None = None,
        s3_output_location: str | None = None,
        catalog: str | None = None,
        schema_allowlist: list[str] | None = None,
        timeout_seconds: int | None = None,
        **kwargs: object,
    ) -> None:
        del host, port, kwargs
        if not region:
            raise WarehouseCapabilityError("Athena: the AWS region is required")
        if not username or not password:
            raise WarehouseCapabilityError(
                "Athena: an access key ID (user name) and its secret access key are required"
            )
        self._timeout_seconds = float(timeout_seconds) if timeout_seconds else None
        self._region = region
        # Every account has the ``primary`` workgroup; it needs an output location
        # unless the workgroup itself configures one.
        self._work_group = work_group or DEFAULT_ATHENA_WORKGROUP
        self._s3_output = s3_output_location or None
        self._catalog = catalog or DEFAULT_ATHENA_CATALOG
        # ``database_name`` is the default Glue database: the schema unqualified
        # table names resolve in.
        self._schema = database or None
        self._schema_allowlist = tuple(schema_allowlist or ())
        self._allowed_columns = set()
        self._column_types = {}
        self._described = None
        self._conn = self._connect_athena(username, password)

    def _connect_athena(self, access_key_id: str, secret_access_key: str) -> Any:
        from botocore.config import Config

        from tripl.core.adapters.athena_sql import import_driver

        pyathena = import_driver()
        timeout = self._timeout_seconds
        config = Config(
            connect_timeout=30,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
            user_agent_extra="tripl",
        )
        return pyathena.connect(
            region_name=self._region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            work_group=self._work_group,
            s3_staging_dir=self._s3_output,
            catalog_name=self._catalog,
            schema_name=self._schema or "default",
            poll_interval=0.5 if timeout is None or timeout > 5 else 0.2,
            kill_on_interrupt=True,
            config=config,
        )

    @override
    def _is_timeout(self, exc: Exception) -> bool:
        return False

    def _execute(self, sql: str, deadline: float | None) -> tuple[Any, _Deadline]:
        cursor = self._conn.cursor()
        guard = _Deadline(cursor, deadline)
        try:
            cursor.execute(sql)
        except Exception as exc:
            guard.cancel()
            self._close_cursor(cursor)
            if guard.fired and deadline is not None:
                raise self._timeout_error(deadline, exc) from exc
            raise
        return cursor, guard

    def _close_cursor(self, cursor: Any) -> None:
        try:
            cursor.close()
        except Exception:
            logger.debug("Athena: cursor close failed", exc_info=True)

    @override
    def _run(
        self, sql: str, *, timeout_cap: float | None = None
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Execute one statement under the deadline; every statement goes through here."""
        deadline = self._query_deadline(timeout_cap)
        cursor, guard = self._execute(sql, deadline)
        try:
            rows = [tuple(row) for row in cursor.fetchall()]
            names = [str(column[0]) for column in cursor.description or []]
        finally:
            guard.cancel()
            self._close_cursor(cursor)
        return names, rows

    @override
    def _describe(self, sql: str) -> list[tuple[str, str]]:
        deadline = self._query_deadline()
        cursor, guard = self._execute(sql, deadline)
        try:
            description = list(cursor.description or [])
        finally:
            guard.cancel()
            self._close_cursor(cursor)
        return [(str(column[0]), str(column[1])) for column in description]
