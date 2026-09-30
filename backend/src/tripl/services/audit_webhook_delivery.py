"""Posting one audit event to an organization's webhook (F20, GH #273).

The body is the row as :func:`tripl.services.audit_rows.row_record` shapes it
(the export's eleven fields), JSON. The headers:

* ``X-Tripl-Timestamp`` — Unix seconds when the request was signed;
* ``X-Tripl-Signature`` — ``sha256=<hex>``, the HMAC-SHA256 under the
  webhook's secret of ``t=<timestamp>.<raw body>``. A receiver recomputes it,
  compares in constant time and rejects a timestamp too far from its clock
  (replay);
* ``X-Tripl-Event-Id`` — the audit row's id: the same on every retry, so a
  receiver can drop duplicates.

The rules of the request are :mod:`tripl.services.safe_http`'s: https only;
on a hosted instance the host must be public, re-checked on EVERY send and
connected to the vetted address; a redirect is never followed and counts as a
failure; a 10 second timeout per socket operation and a
:data:`DEADLINE_SECONDS` wall-clock limit on the request (the send-time host
check has its own :data:`TIMEOUT_SECONDS` budget), so a receiver that drips
its answer cannot hold a worker or API thread. The receiver's body is never
read: only the status line counts. A 2xx answer is a delivery; anything else
is retried by the beat task with :data:`BACKOFF` until :data:`MAX_ATTEMPTS`.

Blocking. The network is behind :func:`_send`, the seam tests replace.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from tripl.services import safe_http
from tripl.services.safe_http import HttpResponse

logger = logging.getLogger(__name__)

FIELD: Final = "Webhook URL"
TIMEOUT_SECONDS: Final = 10.0
#: The whole request, connect to status line, whatever the receiver does.
DEADLINE_SECONDS: Final = 15.0
#: The receiver's body is not read at all: only its status counts.
MAX_RESPONSE_BYTES: Final = 0
#: After the Nth failed attempt, the next one waits ``BACKOFF[N-1]`` (the last
#: entry for every later one): 1m, 5m, 30m, 2h, 6h, 6h, 6h.
BACKOFF: Final = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
)
#: A row that failed this many times is ``dead`` and never tried again.
MAX_ATTEMPTS: Final = 8
#: The action of the synthetic event ``POST .../audit/webhook/test`` sends.
TEST_ACTION: Final = "audit.webhook_test"
SECRET_PREFIX: Final = "whsec_"
SIGNATURE_HEADER: Final = "X-Tripl-Signature"
TIMESTAMP_HEADER: Final = "X-Tripl-Timestamp"
EVENT_ID_HEADER: Final = "X-Tripl-Event-Id"


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    status_code: int | None = None
    #: A short fixed text, never the receiver's body or address.
    error: str | None = None


def new_secret() -> str:
    """A fresh signing secret (shown to the owner once)."""
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def retry_delay(attempts: int) -> timedelta:
    """How long to wait after the ``attempts``-th failed attempt (1-based)."""
    return BACKOFF[min(max(attempts, 1), len(BACKOFF)) - 1]


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """``sha256=<hex>`` of HMAC-SHA256(secret, ``t=<timestamp>.<body>``)."""
    message = f"t={timestamp}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def encode_body(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str).encode(
        "utf-8"
    )


def signed_headers(
    secret: str, record: dict[str, Any], body: bytes, *, now: datetime
) -> dict[str, str]:
    timestamp = int(now.timestamp())
    return {
        "Content-Type": "application/json",
        "User-Agent": "tripl-audit-webhook/1",
        SIGNATURE_HEADER: sign(secret, timestamp, body),
        TIMESTAMP_HEADER: str(timestamp),
        EVENT_ID_HEADER: str(record["id"]),
    }


def _send(url: str, headers: dict[str, str], body: bytes) -> HttpResponse:
    """The network. Replaced by tests; everything else in this module is policy."""
    return safe_http.send(
        "POST",
        url,
        headers,
        body,
        field=FIELD,
        timeout=TIMEOUT_SECONDS,
        max_response_bytes=MAX_RESPONSE_BYTES,
        deadline=DEADLINE_SECONDS,
    )


def post_event(url: str, secret: str, record: dict[str, Any], *, now: datetime) -> DeliveryResult:
    """POST ``record`` to ``url``, signed with ``secret``. Never raises for the network."""
    body = encode_body(record)
    headers = signed_headers(secret, record, body, now=now)
    try:
        safe_http.check_https_url(url, field=FIELD, deadline=safe_http.Deadline(TIMEOUT_SECONDS))
        response = _send(url, headers, body)
    except safe_http.PrivateHostError:
        return DeliveryResult(ok=False, error="private address refused")
    except TimeoutError:
        return DeliveryResult(ok=False, error="timeout")
    except OSError as exc:
        logger.info("audit webhook: request failed: %s", type(exc).__name__)
        return DeliveryResult(ok=False, error="unreachable")
    except ValueError:
        return DeliveryResult(ok=False, error="not an https URL")
    if 200 <= response.status < 300:
        return DeliveryResult(ok=True, status_code=response.status)
    if 300 <= response.status < 400:
        return DeliveryResult(ok=False, status_code=response.status, error="redirect refused")
    return DeliveryResult(ok=False, status_code=response.status, error=f"HTTP {response.status}")
