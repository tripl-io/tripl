"""Cancel a running statement from a timer thread when its deadline passes.

The client-side half of a data source's timeout, for the drivers that do not
stop a statement themselves: the Trino client's ``request_timeout`` bounds one
HTTP request of a statement that polls page after page, and neither the
Databricks connector nor pyathena has a statement deadline at all. Each
engine's server-side half (``query_max_run_time``, ``STATEMENT_TIMEOUT``, the
Athena workgroup's own limit) still applies on top, and holds even if this
worker dies first. Snowflake's connector takes the deadline itself, so the
Snowflake adapter does not use this.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

#: How often a deadline that passed before the statement had a query id looks again.
_RECHECK_SECONDS = 0.25


class StatementDeadline:
    """Cancel ``cursor``'s statement once ``seconds`` pass, until :meth:`disarm`.

    ``fired`` turns true when the deadline passes. That is how the caller tells
    the cancel's error apart from any other: every driver this guards reports a
    cancelled statement by raising (Trino's ``USER_CANCELED``, pyathena's
    ``CANCELLED`` state, the Databricks connector's cancelled operation), so an
    error raised once ``fired`` is set is the deadline's.

    With ``wait_for_query_id``, a deadline that passes before the cursor has a
    query id keeps looking until one appears, and cancels then. Trino and Athena
    need it: until the first response names the query, the Trino client's
    ``cancel`` does nothing and pyathena's has nothing to stop, so a deadline
    that simply fired would be lost and the statement would run on — billed, on
    Athena — up to the server's own limit.
    """

    def __init__(
        self,
        cursor: Any,
        seconds: float | None,
        *,
        engine: str,
        wait_for_query_id: bool = False,
    ) -> None:
        self._cursor = cursor
        self._engine = engine
        self._wait_for_query_id = wait_for_query_id
        self.fired = False
        self._done = False
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        if seconds is not None:
            self._arm(seconds)

    def _arm(self, seconds: float) -> None:
        timer = threading.Timer(seconds, self._expire)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _expire(self) -> None:
        with self._lock:
            if self._done:
                return
            self.fired = True
            if self._wait_for_query_id and not getattr(self._cursor, "query_id", None):
                self._arm(_RECHECK_SECONDS)
                return
        try:
            # Outside the lock: ``cancel`` is a round trip to the warehouse, and
            # ``disarm`` must not wait on it.
            self._cursor.cancel()
        except Exception:
            logger.warning(
                "%s: could not cancel a statement past its deadline", self._engine, exc_info=True
            )

    def disarm(self) -> None:
        """Stop the timer: the statement ended, or the caller gave up on it."""
        with self._lock:
            self._done = True
            if self._timer is not None:
                self._timer.cancel()
