"""Demo provisioning performance budgets.

Guards against a silent cost regression — e.g. a builder switching from batched
``add_all`` to per-row inserts, or an accidental N+1. The budgets are generous
relative to the measured cost (~190 statements, a few seconds here) so they pass
reliably on CI-sized hardware while still catching a multiple-x regression.
"""

import time

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tripl.models import Base
from tripl.services import demo_service

# A full provision issues ~190 SQL statements (batched inserts) locally. 500 leaves
# comfortable headroom yet trips if a builder regresses to per-row writes over the
# ~550 seeded metric buckets (which would be thousands of statements).
_MAX_PROVISION_STATEMENTS = 500
# Wall-clock ceiling. Local provisioning is a few seconds (dominated by the real
# anomaly detector); 45s catches a runaway without flaking on slow CI.
_MAX_PROVISION_SECONDS = 45.0


@pytest.mark.asyncio
async def test_provisioning_stays_within_budget() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = async_sessionmaker(engine, expire_on_commit=False)

    statements = 0

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        nonlocal statements
        statements += 1

    try:
        async with session_local() as session:
            started = time.monotonic()
            response = await demo_service.create_demo_project(session)
            elapsed = time.monotonic() - started
    finally:
        await engine.dispose()

    assert response.generation_status.value == "ready"
    assert statements <= _MAX_PROVISION_STATEMENTS, (
        f"provisioning issued {statements} statements (budget {_MAX_PROVISION_STATEMENTS}); "
        "a builder likely regressed to per-row writes"
    )
    assert elapsed <= _MAX_PROVISION_SECONDS, (
        f"provisioning took {elapsed:.1f}s (budget {_MAX_PROVISION_SECONDS}s)"
    )
