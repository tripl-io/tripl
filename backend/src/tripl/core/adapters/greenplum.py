"""Greenplum (6 and 7) and its forks — Cloudberry, Greengage, WarehousePG.

Greenplum speaks the PostgreSQL wire protocol and SQL of the release it forked:
Greenplum 6 is PostgreSQL 9.4, Greenplum 7 is PostgreSQL 12. Everything
``PostgresAdapter`` emits runs there unchanged — ``jsonb`` and ``LATERAL`` since 9.4,
``FILTER (WHERE ...)`` since 9.4, ``GROUPING SETS`` as a Greenplum extension —
except two things PostgreSQL 14 added, and this class replaces only those:

* ``date_bin``: buckets come from epoch arithmetic instead
  (:func:`~tripl.core.adapters.postgres.epoch_bucket_expression`), the same grid
  ``floor_to_bucket`` draws.
* ``'Infinity'::numeric``: the range contract no longer casts an infinity at all,
  on any server of the family (see ``PostgresAdapter._contract_bad_condition``).

Greenplum 6's regex engine also lacks lookbehind, which needs nothing here: a
pattern the server refuses is dropped per expectation by the regex probe every
adapter puts in front of a regex contract. Verified against the
``woblerr/greenplum`` 6.27.1 and 7.1.0 images.
"""

from __future__ import annotations

from typing import override

from tripl.core.adapters.postgres import (
    _SYSTEM_SCHEMAS,
    PostgresAdapter,
    _format_server_version,
    _quote_ident,
    epoch_bucket_expression,
)

#: Greenplum 6 reports PostgreSQL 9.4 (``server_version`` 90426 on 6.27).
GREENPLUM_MIN_SERVER_VERSION = 90400


class GreenplumAdapter(PostgresAdapter):
    """``PostgresAdapter`` for a Greenplum-family coordinator."""

    engine_label = "Greenplum"
    min_server_version = GREENPLUM_MIN_SERVER_VERSION
    min_server_version_reason = "Greenplum 6, the oldest release tripl supports, is built on"
    min_server_upgrade_target = "Greenplum 6"
    #: Greenplum's own catalogs, which ``information_schema.columns`` lists beside
    #: the user's tables.
    system_schemas = (*_SYSTEM_SCHEMAS, "gp_toolkit", "pg_aoseg", "pg_bitmapindex")

    @override
    def server_name(self, server_version: int) -> str:
        return f"This Greenplum server (PostgreSQL {_format_server_version(server_version)})"

    @override
    def _bucket_expression(self, time_column: str, interval_code: str) -> str:
        return epoch_bucket_expression(
            _quote_ident(self._validate_column(time_column)), interval_code
        )
