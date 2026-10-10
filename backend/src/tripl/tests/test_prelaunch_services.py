"""Shared service helpers the pre-launch clean-up folded copies into.

* ``_project_settings_rows.get_or_create_project_row``: the one get-or-create of
  the per-project settings tables, race fallback included, and the anomaly
  settings read that no longer writes a row.
* ``search_service.reindex_main_branch``: the one "refresh main after a write to
  a project-global entity".
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.services import search_service
from tripl.services._project_settings_rows import get_or_create_project_row
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.tests.conftest import TestSessionLocal


async def _create_project(client: AsyncClient, slug: str) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/projects", json={"name": slug.upper(), "slug": slug, "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _anomaly_rows(project_id: uuid.UUID) -> int:
    async with TestSessionLocal() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ProjectAnomalySettings)
            .where(ProjectAnomalySettings.project_id == project_id)
        )
    return int(count or 0)


# ── get_or_create_project_row ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_lost_first_write_race_returns_the_winners_row(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two first saves both miss the row; the loser's insert trips the unique
    constraint and must hand back the winner's row instead of a 500."""
    project_id = await _create_project(client, "settings-race")
    async with TestSessionLocal() as winner:
        winner.add(ProjectAnomalySettings(project_id=project_id, sigma_threshold=6.0))
        await winner.commit()

    async with TestSessionLocal() as session:
        real_scalar = session.scalar
        reads = 0

        async def first_read_predates_the_winner(*args: Any, **kwargs: Any) -> Any:
            nonlocal reads
            reads += 1
            if reads == 1:
                return None
            return await real_scalar(*args, **kwargs)

        monkeypatch.setattr(session, "scalar", first_read_predates_the_winner)
        row = await get_or_create_project_row(session, ProjectAnomalySettings, project_id)

    assert reads == 2
    assert row.sigma_threshold == 6.0
    assert await _anomaly_rows(project_id) == 1


@pytest.mark.asyncio
async def test_get_or_create_returns_the_stored_row_without_writing(
    client: AsyncClient,
) -> None:
    project_id = await _create_project(client, "settings-existing")
    async with TestSessionLocal() as session:
        created = await get_or_create_project_row(session, ProjectAnomalySettings, project_id)
    async with TestSessionLocal() as session:
        again = await get_or_create_project_row(session, ProjectAnomalySettings, project_id)
    assert again.id == created.id
    assert await _anomaly_rows(project_id) == 1


# ── anomaly settings: GET reads, PATCH writes ────────────────────────────────


@pytest.mark.asyncio
async def test_unsaved_anomaly_settings_read_exactly_what_a_first_save_stores(
    client: AsyncClient,
) -> None:
    """The defaults a GET builds by hand must be the column defaults a first save
    writes, or a project's settings would change the moment anyone saved them."""
    project_id = await _create_project(client, "anomaly-parity")
    url = "/api/v1/projects/anomaly-parity/anomaly-settings"

    before = await client.get(url)
    assert before.status_code == 200
    assert await _anomaly_rows(project_id) == 0

    saved = await client.patch(url, json={})
    assert saved.status_code == 200
    assert await _anomaly_rows(project_id) == 1

    unsaved, stored = before.json(), saved.json()
    for key in ("id", "created_at", "updated_at"):
        assert unsaved.pop(key) is None
        assert stored.pop(key) is not None
    assert unsaved == stored


# ── reindex_main_branch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reindex_main_branch_rebuilds_the_projects_main_branch(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = await _create_project(client, "reindex-main")
    calls: list[tuple[uuid.UUID, uuid.UUID, str | None]] = []

    async def fake_reindex(
        _session: Any, *, project_id: uuid.UUID, branch_id: uuid.UUID, slug: str | None = None
    ) -> search_service.ReindexOutcome:
        calls.append((project_id, branch_id, slug))
        return search_service.ReindexOutcome(documents_indexed=3, embeddings_scheduled=False)

    monkeypatch.setattr(search_service, "reindex_project_branch", fake_reindex)
    async with TestSessionLocal() as session:
        main_branch_id = await resolve_branch_id(session, project_id, None)
        outcome = await search_service.reindex_main_branch(session, project_id, slug="reindex-main")

    assert calls == [(project_id, main_branch_id, "reindex-main")]
    assert outcome.documents_indexed == 3
