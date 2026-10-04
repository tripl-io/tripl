"""Shared error sanitisation for worker tasks.

Worker tasks run against external data warehouses (ClickHouse, Postgres, ...)
through SQLAlchemy and driver libraries. When something fails, the raw exception
text routinely embeds hostnames, ports, driver/library names, and SQLAlchemy
hints. Those details belong in the logs only — they must never reach a
user-facing field (a scan/metrics job's ``error_message``, a data source's
``last_test_message``).

``user_facing_error`` maps any exception to a short, category-specific summary
that is safe to persist verbatim, while the caller logs the full exception
separately. ``ScanError`` opts a message *into* verbatim surfacing for
controlled, user-actionable conditions (a configuration problem, a row-limit
hit, ...).

A second exception is admitted to that curated set: ``core.name_template
.NameFormatError``. It is raised in ``core``, which must never import ``worker``,
so it cannot subclass ``ScanError``; admitting it by type here rather than
wrapping it at each call site means every caller of the name-format code — there
are two today and a third will come — surfaces the real reason without opting in.

A third is ``core.adapters.errors.WarehouseCapabilityError``, on the identical
bargain and for the identical reason. It also lives in ``core``, it is documented
at its definition as a message tripl authored about a configuration the operator
can act on, and ``datasource_service`` and ``metric_preview_service`` already
surface it verbatim on their own paths — the worker was the one consumer that
flattened it. What the operator saw instead was "Scan failed due to an internal
error." on every tick, forever, for conditions like a DATE time column configured
at a 1h interval or a PostgreSQL server too old for ``date_bin`` — each of which
names the setting to change in its own text.
"""

from __future__ import annotations

from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.name_template import NameFormatError


class ScanError(Exception):
    """A failure whose message is safe to surface to end users verbatim.

    Raised for controlled, user-actionable conditions (a configuration problem,
    a hit row limit, ...). Anything that is *not* a ``ScanError`` is treated as
    an internal failure: the raw exception is logged in full but only a generic,
    sanitized summary is persisted on the user-facing field — raw driver/ORM
    exceptions leak hostnames, ports, library names, and SQLAlchemy hints that
    must never reach the user.
    """


# Exceptions whose message tripl authored and is therefore safe verbatim.
# ``ScanError`` is the worker's own; ``NameFormatError`` is raised in ``core``
# (see the module docstring) and carries only the format's placeholder names and
# the columns that were available — no host, port, driver or ORM text.
#
# ``WarehouseCapabilityError`` is the widest entry and the one to be careful with:
# unlike the other two it is raised from the adapters, next to the driver text this
# module exists to keep out. Admitting it is a claim about every present and future
# raise site, so it is a decision, not a convenience. The claim is checkable and was
# checked: the thirteen sites today (eight in ``adapters/postgres.py``, five in
# ``adapters/bigquery.py``) interpolate only values the operator supplied via the form
# (``sslmode``, ``search_path`` names), names rather than values where the value is
# secret (``sslkey``), a declared column type, a server version, and one
# ``json.JSONDecodeError`` whose text reports a syntax position and never the
# document. If a future raise site cannot make that promise, the answer is to wrap
# it in a ``ScanError`` at the worker boundary — not to loosen this tuple.
_CURATED_ERRORS = (ScanError, NameFormatError, WarehouseCapabilityError)

# A curated message is surfaced verbatim; cap its length so an
# accidental novel-length message can't blow up a UI row (mirrors how a data
# source's ``last_test_message`` stays short and displayable).
_MAX_CURATED_LEN = 500

# Every string this module returns starts with this, and that is a CONTRACT
# rather than a house style: ``frontend/src/lib/scanError.ts`` shows a backend
# message verbatim ONLY when it starts with "scan failed", and collapses
# everything else to a bare "Scan failed."
#
# The generic branches below always satisfied it and the curated branch never
# did, so until the prefix fix every author-written ``ScanError`` in this repository
# — some sixty of them, the entire reason the curated set exists — was discarded
# by the UI and rendered as the one message it was written to replace. One raise
# site worked, ``_apply_name_format``, and only because it spells the prefix
# inline.
#
# Guaranteeing it HERE rather than at each raise site is the point: a convention
# sixty call sites must remember is one the sixty-first forgets, and this
# function is the funnel every one of them already passes through.
SCAN_FAILED_PREFIX = "Scan failed"

# A scan names its events one of two ways: one event type for every row, or the
# values of one column. With neither there is nothing to name anything after, so
# no run of the config can create a single event.
#
# Surfaced verbatim, which is why it is written in the vocabulary of the scan
# form's own controls. It used to read "Either event_type_id or
# event_type_column must be specified" — two database columns no screen in the
# product shows, in a run report whose reader is looking for something to click.
NO_EVENT_NAMING_MSG = (
    "This scan has no Event type and no Event type column, so it cannot name "
    "any events. Set one under the scan's Configuration tab."
)

# Substrings that identify the failure category from an exception's text without
# echoing the (host/port/library-laden) text itself back to the user. Celery's
# soft/hard time-limit exceptions carry no "timeout" word, so match them here too
# — a scan that runs into the task time limit is a data-source timeout in effect.
_TIMEOUT_HINTS = (
    "timeout",
    "timed out",
    "time out",
    "soft time limit",
    "time limit exceeded",
    "timelimitexceeded",
)
_CONNECTION_HINTS = (
    "connection refused",
    "could not connect",
    "connection reset",
    "connection aborted",
    "network is unreachable",
    "no route to host",
    "host is unreachable",
    "name or service not known",
    "getaddrinfo",
    "failed to resolve",
)


def is_transient_send_error(text: str | None) -> bool:
    """Whether persisted error text describes a transient network failure.

    Classifies a stored ``error_message`` for retry eligibility: the alert
    delivery reaper auto-retries a failed delivery only when its recorded
    failure looks like an egress blip that a later attempt can outlive.
    Deliberately matches the same substrings that categorize scan failures
    above — a delivery send and a scan reach the outside world through the
    same egress, so the texts describing its momentary absence are the same.
    """
    if text is None:
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in _TIMEOUT_HINTS + _CONNECTION_HINTS)


def _as_scan_failure(message: str) -> str:
    """A curated message, carrying ``SCAN_FAILED_PREFIX`` whether or not it said so.

    Idempotent, so a raise site that spells the prefix itself is not doubled.
    ``_apply_name_format`` is the one that does, deliberately: it budgets its
    "Available keys" list against the 500-char cap and that arithmetic has to
    include the prefix, which it can only do by owning it (``core`` cannot
    import ``worker``).

    The cap lands last. It exists to bound what a UI row renders, and that is
    this string — not the body it was assembled from.
    """
    text = message.strip()
    if not text.lower().startswith(SCAN_FAILED_PREFIX.lower()):
        text = f"{SCAN_FAILED_PREFIX}: {text}"
    return text[:_MAX_CURATED_LEN]


def user_facing_error(exc: Exception) -> str:
    """Map an exception to a message safe to persist on a user-facing field.

    ``_CURATED_ERRORS`` messages are author-curated and surfaced verbatim. For
    every other exception the raw text is dropped (it carries hostnames, ports,
    driver names, or ORM autoflush hints — log the full exception separately) and
    replaced with a generic, categorized summary.

    Every branch returns a string starting with ``SCAN_FAILED_PREFIX``; see the
    constant for why that is load-bearing rather than cosmetic.
    """
    if isinstance(exc, _CURATED_ERRORS):
        return _as_scan_failure(str(exc))
    text = str(exc).lower()
    if any(hint in text for hint in _TIMEOUT_HINTS):
        return f"{SCAN_FAILED_PREFIX}: the data source did not respond in time."
    if any(hint in text for hint in _CONNECTION_HINTS):
        return f"{SCAN_FAILED_PREFIX}: could not connect to the data source."
    # Self-hosted: the operator *is* support, so a "contact support" line is noise
    # — keep the message to the actionable fact and let them read the logs.
    return f"{SCAN_FAILED_PREFIX} due to an internal error."
