"""The operator's embedding provider is checked the way an organization's is.

``OrgSearchSettingsUpdate`` has only ever accepted ``"openai"``, the one
provider ``embedding_service`` can call. The operator's save took any string,
and ``embedding_service.can_embed`` then turned semantic search off, without a
word, for every organization inheriting that value.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.app_setting import SERVICE_SETTINGS_KEY, AppSetting
from tripl.tests.conftest import TestSessionLocal

# The current route and the legacy alias both take ServiceSettingsUpdate.
_ROUTES = ("/api/v1/platform/settings", "/api/v1/settings")


async def _stored_overrides() -> dict[str, object]:
    async with TestSessionLocal() as session:
        row = await session.scalar(select(AppSetting).where(AppSetting.key == SERVICE_SETTINGS_KEY))
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize("provider", ["cohere", "OpenAI", ""])
@pytest.mark.asyncio
async def test_an_unsupported_provider_is_refused_and_not_stored(
    client: AsyncClient, route: str, provider: str
) -> None:
    resp = await client.patch(route, json={"ai": {"search_embedding_provider": provider}})

    assert resp.status_code == 422, resp.text
    assert "search_embedding_provider" not in await _stored_overrides()


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.asyncio
async def test_the_supported_provider_and_a_reset_are_accepted(
    client: AsyncClient, route: str
) -> None:
    saved = await client.patch(route, json={"ai": {"search_embedding_provider": "openai"}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["ai"]["search_embedding_provider"] == "openai"

    # null clears the override, so the field falls back to the environment.
    cleared = await client.patch(route, json={"ai": {"search_embedding_provider": None}})
    assert cleared.status_code == 200, cleared.text
    assert "search_embedding_provider" not in await _stored_overrides()
