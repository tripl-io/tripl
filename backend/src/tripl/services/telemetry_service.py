"""Opt-in anonymous usage telemetry: one small ping a day.

Off unless the operator turns it on (``TELEMETRY_ENABLED=true``; ``tripl
install`` asks once). Then the ``send-telemetry`` beat task POSTs one JSON
document a day to ``TELEMETRY_ENDPOINT``. What it holds is everything there is
(``website/docs/run/telemetry.md`` lists it, and **Settings → Platform →
Runtime** shows the last one sent):

* a random instance id, made the first time and kept in ``app_settings``;
* the version, edition and deployment mode;
* which warehouse engines the instance's data sources use;
* bucketed counts — projects, event types, accounts, scans in the last day —
  never the exact number;
* how many days ago the instance first made its id.

Never names, slugs, emails, hosts, queries or warehouse data. A public demo
sends nothing. The request has a short timeout, follows no redirect, and a
failure is logged and forgotten: telemetry never gets in the way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import extensions, tenancy
from tripl.config import settings
from tripl.models.app_setting import AppSetting
from tripl.models.data_source import DataSource
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.models.scan_job import ScanJob
from tripl.models.user import User
from tripl.services import safe_http

logger = logging.getLogger(__name__)

SETTING_KEY = "telemetry"
SCHEMA_VERSION = 1
TIMEOUT_SECONDS = 5.0
#: Bucket floors: a count is reported as the bucket it falls in, never itself.
_BUCKETS: tuple[tuple[int, str], ...] = (
    (1000, "1000+"),
    (101, "101-1000"),
    (11, "11-100"),
    (1, "1-10"),
    (0, "0"),
)


def bucket(count: int) -> str:
    """``count`` as a range: ``0``, ``1-10``, ``11-100``, ``101-1000`` or ``1000+``."""
    return next(label for floor, label in _BUCKETS if count >= floor)


def _version() -> str:
    try:
        return version("tripl-server")
    except PackageNotFoundError:
        return "unknown"


def edition() -> str:
    return "enterprise" if extensions.extensions() else "community"


def inactive_reason() -> str | None:
    """Why nothing is sent, or None when the ping goes out."""
    if not settings.telemetry_enabled:
        return "disabled"
    if not settings.telemetry_endpoint:
        return "no endpoint"
    if tenancy.public_demo():
        return "public demo"
    return None


def _operator_row() -> Any:
    return select(AppSetting).where(
        AppSetting.key == SETTING_KEY, AppSetting.organization_id.is_(None)
    )


async def _state(session: AsyncSession, now: datetime) -> AppSetting:
    """The stored state, made (id, first seen) the first time. No commit."""
    row: AppSetting | None = await session.scalar(_operator_row())
    if row is None:
        row = AppSetting(
            key=SETTING_KEY,
            organization_id=None,
            value={"instance_id": str(uuid.uuid4()), "installed_at": now.isoformat()},
        )
        session.add(row)
        await session.flush()
    return row


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int(await session.scalar(stmt) or 0)


async def build_payload(session: AsyncSession, now: datetime) -> dict[str, Any]:
    """Everything one ping holds. Makes the instance id the first time; no commit."""
    state = dict((await _state(session, now)).value or {})
    installed = datetime.fromisoformat(str(state.get("installed_at") or now.isoformat()))
    if installed.tzinfo is None:
        installed = installed.replace(tzinfo=UTC)
    engines = sorted(
        str(engine)
        for engine in (await session.scalars(select(DataSource.db_type).distinct())).all()
    )
    since = now - timedelta(days=1)
    return {
        "schema": SCHEMA_VERSION,
        "instance_id": str(state["instance_id"]),
        "version": _version(),
        "edition": edition(),
        "deployment_mode": settings.deployment_mode,
        "warehouse_engines": engines,
        "projects": bucket(await _count(session, select(func.count(Project.id)))),
        "event_types": bucket(await _count(session, select(func.count(EventType.id)))),
        "users": bucket(await _count(session, select(func.count(User.id)))),
        "scans_last_day": bucket(
            await _count(session, select(func.count(ScanJob.id)).where(ScanJob.created_at >= since))
        ),
        "days_since_install": max(0, (now - installed).days),
    }


def post(endpoint: str, payload: dict[str, Any]) -> bool:
    """POST ``payload``; whether the receiver took it. Never raises. Blocking."""
    try:
        answer = safe_http.send(
            "POST",
            endpoint,
            {"Content-Type": "application/json", "User-Agent": f"tripl/{payload['version']}"},
            json.dumps(payload, sort_keys=True).encode(),
            field="TELEMETRY_ENDPOINT",
            timeout=TIMEOUT_SECONDS,
            max_response_bytes=4096,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail anything
        logger.info("telemetry.not_sent error=%s", type(exc).__name__)
        return False
    if not 200 <= answer.status < 300:
        logger.info("telemetry.not_sent status=%s", answer.status)
        return False
    return True


async def send(session: AsyncSession, now: datetime | None = None) -> dict[str, Any]:
    """The daily ping: build, POST, remember what was sent. Commits."""
    reason = inactive_reason()
    if reason is not None:
        return {"sent": False, "reason": reason}
    now = now or datetime.now(UTC)
    payload = await build_payload(session, now)
    delivered = await asyncio.to_thread(post, settings.telemetry_endpoint, payload)
    row = await _state(session, now)
    row.value = {
        **dict(row.value or {}),
        "last_payload": payload,
        "last_attempt_at": now.isoformat(),
        "last_delivered": delivered,
    }
    await session.commit()
    return {"sent": delivered}


async def status(session: AsyncSession) -> dict[str, Any]:
    """What the operator sees: on or off, where to, and the last ping. No writes."""
    row: AppSetting | None = await session.scalar(_operator_row())
    value = dict(row.value or {}) if row is not None else {}
    reason = inactive_reason()
    return {
        "enabled": reason is None,
        "reason": reason,
        "endpoint": settings.telemetry_endpoint,
        "instance_id": value.get("instance_id"),
        "last_attempt_at": value.get("last_attempt_at"),
        "last_delivered": value.get("last_delivered"),
        "last_payload": value.get("last_payload"),
    }
