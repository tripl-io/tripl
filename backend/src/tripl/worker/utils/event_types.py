"""Resolve (and, when absent, create) the EventType a scanned group writes into.

Lives here rather than in ``worker.tasks.metrics.generation`` because the scan
task needs it too, and importing a metrics task module for one pure helper would
drag the whole ``collect_metrics`` task graph into ``worker.tasks.scan``'s import
path — the same reasoning that put ``reserved_columns`` in this package.

Both grouped paths go through this one function so that a manual **Run** and a
scheduled collection leave the catalog in the same state. Previously
only the scheduled path created: a manual grouped run merely *looked up* the
event type by name and skipped the group when it was missing, so a Catalog-only
config — which by definition never reaches the scheduler — created zero events
forever, while the dry run cheerfully promised the type "would be added".
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.adapters.base import ColumnInfo
from tripl.core.analyzers.cardinality import _is_json_type
from tripl.core.analyzers.event_generator import lock_project_catalog
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.worker.plan_scope import main_branch_id

# ``worker.tasks._errors`` is a LEAF: it imports ``core.name_template`` and
# nothing else, and ``worker/tasks/__init__.py`` is empty, so naming it here
# imports no task module and the reasoning in the docstring above still holds.
# The import earns its place — a plain ``ValueError`` is scrubbed to "Scan
# failed due to an internal error." by ``user_facing_error``, which is exactly
# the message this guard exists to replace.
from tripl.worker.tasks._errors import ScanError

logger = logging.getLogger(__name__)

# What ``event_types.name`` can hold (``models.event_type``: ``String(100)``),
# and the same bound the API already enforces on a hand-created event type
# (``schemas.event_type.EventTypeCreate``: ``min_length=1, max_length=100``).
# The catalog has ONE rule for what an event type may be called; a path that
# auto-creates must not be allowed to write a row a person could not.
EVENT_TYPE_NAME_MAX_LEN = 100

# How much of an over-long value the refusal quotes back. Deliberately short:
# ``user_facing_error`` caps a curated message at 500 chars from the RIGHT, and
# the actionable tail ("pick a different Event type column") is what gets eaten
# first. 40 chars survives even a value that is all backslashes and newlines,
# whose ``repr`` is four times its length (the same arithmetic as elsewhere).
_NAME_PREVIEW_LEN = 40


def _elide(value: str) -> str:
    if len(value) <= _NAME_PREVIEW_LEN:
        return value
    return value[: _NAME_PREVIEW_LEN - 3] + "..."


def event_type_name_rejection(value: str) -> str | None:
    """Why a warehouse group value cannot name an event type, or None if it can.

    The policy is REJECT, and deliberately not truncate. An event type's name is
    its IDENTITY, not display text:

    * Truncating collides. Two 120-char values that agree on their first 100
      characters become one event type, which then absorbs both groups' events
      and dedups the second group's against the first's — silently, permanently,
      and only on the values long enough to be hard to notice.
    * Truncating with a disambiguating suffix avoids the collision but breaks a
      different invariant: the name written here must equal the group value,
      because other sites look the type up BY that raw value —
      ``metrics.catalog_sync`` re-selects ``EventType.name == et_name`` for drift
      and contract detection before calling this, and the dry run does the same
      to label a type existing rather than new. A name this function reshaped
      matches none of them, so every tick would rediscover the type as new.

    So the value is validated and then stored VERBATIM — a padded ``" home "``
    stays padded — and anything that cannot be stored verbatim is refused.

    Returns the bare reason so both surfaces can use it: the run raises it as a
    ``ScanError`` (``user_facing_error`` prefixes "Scan failed:"), and the dry
    run appends it to ``errors`` unprefixed, the way it already reports a
    ``NameFormatError`` it will not fail the preview over.
    """
    # ``analyze_cardinality_grouped`` maps a NULL group cell to ``""``, so the
    # blank case is reachable on every warehouse, not hypothetical — and before
    # this it created ONE nameless event type per project that quietly collected
    # every NULL row. No screen in the product can render it and no user could
    # have created it.
    if not value.strip():
        return (
            "The Event type column produced a blank value, which cannot name an "
            "event type. Exclude those rows in the scan's query, or pick a "
            "different Event type column."
        )
    if len(value) > EVENT_TYPE_NAME_MAX_LEN:
        return (
            f"The Event type column produced a {len(value)}-character value, "
            f"longer than the {EVENT_TYPE_NAME_MAX_LEN} characters an event "
            f"type name can hold: {_elide(value)!r}. Shorten it in the scan's "
            "query, or pick a different Event type column."
        )
    return None


def ensure_event_type_with_fields(
    session: Session,
    project_id: uuid.UUID,
    et_name: str,
    columns: list[ColumnInfo],
    skip_columns: set[str],
) -> EventType:
    """Find or auto-create an EventType with FieldDefinitions for all columns."""
    # BEFORE the lookup, not after: a blank name would otherwise find the blank
    # event type an earlier run created and keep feeding it.
    #
    # Refusing fails the whole grouped run, which is the honest verdict — on
    # PostgreSQL an over-long value already killed it, just with "Scan failed due
    # to an internal error." for a message. What changes is that the reason is
    # now sayable, that SQLite and PostgreSQL agree, and that the dry run refuses
    # the same values in advance (``tasks.scan_dry_run._dry_run_targets``) so the
    # operator reads it there first instead of finding out from a failed job.
    rejection = event_type_name_rejection(et_name)
    if rejection is not None:
        raise ScanError(rejection)
    # Before the first shared insert; see ``lock_project_catalog``.
    lock_project_catalog(session, project_id)

    # Scans and metrics collection target the main plan; a working branch
    # deep-copies event types under the same names, so the lookup must be
    # branch-scoped — and the created row must land on main, which it does
    # because ``EventType.branch_id``'s column default resolves to it.
    et = session.execute(
        select(EventType).where(
            EventType.project_id == project_id,
            EventType.branch_id == main_branch_id(session, project_id),
            EventType.name == et_name,
        )
    ).scalar_one_or_none()

    if et is None:
        et = EventType(
            id=uuid.uuid4(),
            project_id=project_id,
            name=et_name,
            display_name=et_name,
            description="Auto-created from metrics collection",
        )
        session.add(et)
        session.flush()
        logger.info(f"Auto-created event type {et_name!r}")

    existing_fds = {fd.name for fd in et.field_definitions}
    for col in columns:
        if col.name in skip_columns:
            continue
        if col.name in existing_fds:
            continue
        fd = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=et.id,
            name=col.name,
            display_name=col.name,
            field_type="json" if _is_json_type(col.type_name) else "string",
            is_required=False,
            description=f"Auto-created ({col.type_name})",
        )
        session.add(fd)

    session.flush()
    session.refresh(et)
    return et
