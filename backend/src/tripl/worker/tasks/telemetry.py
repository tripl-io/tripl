"""The daily opt-in usage ping (``telemetry_service``): a no-op unless enabled."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.services import telemetry_service
from tripl.worker.celery_app import celery_app
from tripl.worker.db import run_with_async_worker_session


@celery_app.task(name="tripl.worker.tasks.telemetry.send_telemetry")  # type: ignore[untyped-decorator]
def send_telemetry() -> dict[str, Any]:
    reason = telemetry_service.inactive_reason()
    if reason is not None:
        return {"sent": False, "reason": reason}
    result: dict[str, Any] = {}

    async def _run(session: AsyncSession) -> None:
        result.update(await telemetry_service.send(session))

    asyncio.run(run_with_async_worker_session(_run))
    return result
