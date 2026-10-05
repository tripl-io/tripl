"""Every secret the database stores encrypted, and re-encrypting them all.

A stored secret is a value written through :func:`tripl.crypto.encrypt_value`
that outlives the request: a whole column (``*_encrypted``) or a key inside a
JSON document. :data:`CORE_SLOTS` lists Community's; an extension adds the ones
its own tables hold through ``Extension.stored_secret_slots``, and
:func:`stored_secret_slots` returns both.

:func:`rewrap_stored_secrets` visits every non-empty stored value and replaces
it with what a ``transform`` (ciphertext in, ciphertext out) returns: moving
secrets to another key is "decrypt with whatever key you accept, encrypt with
the current one". It is the primitive a key-rotation command is built on.

Not here: values encrypted only for a moment (the sign-in state cookie of
``instance_login``); they expire on their own.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import Column, Table, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models import Base
from tripl.models.app_setting import AI_SETTINGS_KEY, SERVICE_SETTINGS_KEY, TRACKER_DEFAULTS_KEY
from tripl.schemas.data_source import SSLKEY_STORAGE_KEY
from tripl.services._app_settings_fields import SECRET_FIELDS as SETTINGS_SECRET_FIELDS
from tripl.services.org_tracker_defaults_service import SECRET_FIELDS as TRACKER_SECRET_FIELDS

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500


@dataclass(frozen=True)
class ColumnSecret:
    """A text column whose every non-empty value is a stored secret."""

    table: str
    column: str

    @property
    def name(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class JsonSecret:
    """String values under ``keys`` of a JSON column holding a dict.

    ``where_column`` / ``where_values`` restrict it to the rows whose
    ``where_column`` is one of ``where_values`` (``app_settings.key``, say);
    ``label`` tells two slots on the same column apart in counts.
    """

    table: str
    column: str
    keys: frozenset[str]
    label: str
    where_column: str | None = None
    where_values: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        return f"{self.table}.{self.column}[{self.label}]"


SecretSlot = ColumnSecret | JsonSecret

_ALERT_DESTINATION_COLUMNS = (
    "webhook_url_encrypted",
    "bot_token_encrypted",
    "target_url_encrypted",
    "webhook_header_value_encrypted",
    "jira_api_token_encrypted",
    "linear_api_key_encrypted",
    "pagerduty_routing_key_encrypted",
    "teams_webhook_url_encrypted",
)

#: Community's stored secrets.
CORE_SLOTS: tuple[SecretSlot, ...] = (
    *(ColumnSecret("alert_destinations", column) for column in _ALERT_DESTINATION_COLUMNS),
    ColumnSecret("data_sources", "password_encrypted"),
    ColumnSecret("project_tracker_configs", "api_token_encrypted"),
    # A PostgreSQL source's client private key (datasource_service).
    JsonSecret(
        "data_sources", "extra_params", frozenset({SSLKEY_STORAGE_KEY}), label=SSLKEY_STORAGE_KEY
    ),
    # The secret fields of the operator's and every organization's settings
    # overrides, and of the first cut's legacy ``ai`` document.
    JsonSecret(
        "app_settings",
        "value",
        SETTINGS_SECRET_FIELDS,
        label="settings",
        where_column="key",
        where_values=frozenset({SERVICE_SETTINGS_KEY, AI_SETTINGS_KEY}),
    ),
    # An organization's issue-tracker defaults.
    JsonSecret(
        "app_settings",
        "value",
        TRACKER_SECRET_FIELDS,
        label=TRACKER_DEFAULTS_KEY,
        where_column="key",
        where_values=frozenset({TRACKER_DEFAULTS_KEY}),
    ),
    # An organization's own photo storage: its service-account JSON.
    JsonSecret(
        "photo_storage_configs", "value", frozenset({"credentials_json"}), label="credentials_json"
    ),
)


def stored_secret_slots() -> tuple[SecretSlot, ...]:
    """Community's slots, then every installed extension's."""
    from tripl.extensions import extensions

    slots: list[SecretSlot] = list(CORE_SLOTS)
    for extension in extensions():
        slots.extend(extension.stored_secret_slots())
    names = [slot.name for slot in slots]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise ValueError(f"stored secret slots listed twice: {', '.join(duplicated)}")
    return tuple(slots)


@dataclass(frozen=True)
class SlotCounts:
    """What a rewrap did to one slot's values."""

    #: Values the transform changed (written back, or would be on a dry run).
    rewritten: int = 0
    #: Values the transform returned as they were.
    unchanged: int = 0
    #: Values the transform could not read (it raised ``InvalidToken``); left as they are.
    unreadable: int = 0


def _table(name: str) -> Table:
    table = Base.metadata.tables.get(name)
    if table is None:
        raise LookupError(f"stored secret slot names an unknown table {name!r}")
    return table


def _primary_key(table: Table) -> Column[Any]:
    columns = list(table.primary_key.columns)
    if len(columns) != 1:
        raise LookupError(f"table {table.name!r} needs a single-column primary key to rewrap")
    return columns[0]


def _column(table: Table, name: str) -> Column[Any]:
    if name not in table.c:
        raise LookupError(f"stored secret slot names an unknown column {table.name}.{name}")
    return table.c[name]


def _apply(
    transform: Callable[[str], str], value: str, slot: SecretSlot, row_id: object
) -> tuple[str, str]:
    """``(new value, outcome)``; never logs the value."""
    try:
        new = transform(value)
    except InvalidToken:
        logger.warning("stored_secrets.unreadable slot=%s row=%s", slot.name, row_id)
        return value, "unreadable"
    return new, ("unchanged" if new == value else "rewritten")


async def _rewrap_slot(
    session: AsyncSession,
    slot: SecretSlot,
    transform: Callable[[str], str],
    *,
    batch_size: int,
    dry_run: bool,
) -> SlotCounts:
    table = _table(slot.table)
    pk = _primary_key(table)
    target = _column(table, slot.column)
    # A rewrap is not an edit: keep ``updated_at`` (and any other on-update
    # column) as it was.
    preserved = {c.name: c for c in table.columns if c.onupdate is not None and c is not target}
    outcomes = {"rewritten": 0, "unchanged": 0, "unreadable": 0}

    base = select(pk, target).where(target.is_not(None)).order_by(pk).limit(batch_size)
    if isinstance(slot, ColumnSecret):
        base = base.where(target != "")
    elif slot.where_column is not None:
        base = base.where(_column(table, slot.where_column).in_(sorted(slot.where_values)))
    if not dry_run:
        # Held until the batch commits, so a concurrent edit of the same row is
        # neither lost nor overwritten with an older value.
        base = base.with_for_update()

    last: object = None
    while True:
        query = base if last is None else base.where(pk > last)
        rows = (await session.execute(query)).all()
        if not rows:
            break
        for row_id, stored in rows:
            if isinstance(slot, ColumnSecret):
                if not isinstance(stored, str) or not stored:
                    continue
                new, outcome = _apply(transform, stored, slot, row_id)
                outcomes[outcome] += 1
                if outcome == "rewritten" and not dry_run:
                    await session.execute(
                        update(table).where(pk == row_id).values({target.name: new, **preserved})
                    )
                continue
            if not isinstance(stored, dict):
                continue
            changed: dict[str, Any] = {}
            for key in sorted(slot.keys):
                value = stored.get(key)
                if not isinstance(value, str) or not value:
                    continue
                new, outcome = _apply(transform, value, slot, row_id)
                outcomes[outcome] += 1
                if outcome == "rewritten":
                    changed[key] = new
            if changed and not dry_run:
                document = {**stored, **changed}
                await session.execute(
                    update(table).where(pk == row_id).values({target.name: document, **preserved})
                )
        last = rows[-1][0]
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return SlotCounts(**outcomes)


async def rewrap_stored_secrets(
    session: AsyncSession,
    transform: Callable[[str], str],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    slots: Sequence[SecretSlot] | None = None,
) -> dict[str, SlotCounts]:
    """Replace every stored secret with ``transform(value)``; counts per slot name.

    ``transform`` takes and returns a stored (encrypted) value; returning it
    unchanged leaves the row alone, and raising ``InvalidToken`` marks the value
    unreadable (logged by slot and row, never the value) and leaves it too.
    Anything else it raises stops the run. Rows are read in primary-key order,
    ``batch_size`` at a time, locked while their batch is rewritten, and each
    batch commits on its own: an interrupted run keeps what it did, and running
    again is safe when ``transform`` leaves values already under the current key
    alone. ``dry_run`` counts what would change and writes nothing.

    ``slots`` defaults to :func:`stored_secret_slots`. Commits (or, on a dry
    run, rolls back) ``session``, so pass one with nothing else pending.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    chosen = stored_secret_slots() if slots is None else tuple(slots)
    counts: dict[str, SlotCounts] = {}
    for slot in chosen:
        counts[slot.name] = await _rewrap_slot(
            session, slot, transform, batch_size=batch_size, dry_run=dry_run
        )
    return counts


__all__ = [
    "CORE_SLOTS",
    "DEFAULT_BATCH_SIZE",
    "ColumnSecret",
    "JsonSecret",
    "SecretSlot",
    "SlotCounts",
    "rewrap_stored_secrets",
    "stored_secret_slots",
]
