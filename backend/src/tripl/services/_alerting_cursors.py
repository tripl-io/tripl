"""Opaque keyset cursors for the alert inbox and the delivery lists.

Offset paging over a list that refetches every minute can SKIP a row: when an
incident on page 1 sorts down past the page seam between the first request and
"Load more", every later row shifts up by one and the first row of page 2 is
never served. A cursor names the last row the reader holds instead of a
position, so the next page starts strictly after it whatever moved above.

The cursor is opaque on purpose — base64 of a tagged, pipe-joined key — so a
client cannot come to depend on its shape, and the tag stops an inbox cursor
from being replayed against the delivery log (and vice versa). Anything that
does not decode is a 422, never a silent first page.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException

# The inbox sort key, as ``_inbox_sort_key`` builds it: effective openness,
# activity instant, correlation group id.
InboxCursorKey = tuple[bool, datetime, str]

_INBOX_TAG = "inbox1"
_DELIVERY_TAG = "delivery1"


def _encode(parts: list[str]) -> str:
    raw = "|".join(parts).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _invalid() -> HTTPException:
    return HTTPException(status_code=422, detail="Invalid cursor")


def _decode(cursor: str, *, tag: str, arity: int) -> list[str]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except binascii.Error, UnicodeDecodeError, ValueError:
        raise _invalid() from None
    parts = raw.split("|")
    if len(parts) != arity + 1 or parts[0] != tag:
        raise _invalid()
    return parts[1:]


def _parse_instant(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise _invalid() from None


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise _invalid() from None


def encode_inbox_cursor(key: InboxCursorKey) -> str:
    is_open, activity, group_id = key
    return _encode([_INBOX_TAG, "1" if is_open else "0", activity.isoformat(), group_id])


def decode_inbox_cursor(cursor: str) -> InboxCursorKey:
    is_open, activity, group_id = _decode(cursor, tag=_INBOX_TAG, arity=3)
    if is_open not in ("0", "1"):
        raise _invalid()
    instant = _parse_instant(activity)
    # The sort key compares UTC-aware instants; a naive one would raise on the
    # first comparison rather than page.
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return (is_open == "1", instant, str(_parse_uuid(group_id)))


def encode_delivery_cursor(created_at: datetime, delivery_id: uuid.UUID) -> str:
    return _encode([_DELIVERY_TAG, created_at.isoformat(), str(delivery_id)])


def decode_delivery_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    created_at, delivery_id = _decode(cursor, tag=_DELIVERY_TAG, arity=2)
    return (_parse_instant(created_at), _parse_uuid(delivery_id))
