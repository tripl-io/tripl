"""Incident summary routes (F14, #267): contract, membership and editor gates."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tripl.api.deps import get_current_user
from tripl.main import app
from tripl.models.user import User
from tripl.tests._incident_summary_seed import (
    GOOD_REPLY,
    FakeLlm,
    enable_ai,
    seed_incident,
)
from tripl.tests._members import PASSWORD_HASH_PLACEHOLDER, persisted_member_user
from tripl.tests.conftest import TestSessionLocal

pytestmark = pytest.mark.asyncio


def _as(user: User) -> None:
    async def _override() -> User:
        return user

    app.dependency_overrides[get_current_user] = _override


async def test_disabled_state_over_http(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-api-off")
    enable_ai(monkeypatch, enabled=False)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)

    got = await client.get(seeded.summary_url)
    posted = await client.post(seeded.summary_url)
    assert (got.status_code, posted.status_code) == (200, 200)
    for body in (got.json(), posted.json()):
        assert body == {
            "correlation_group_id": str(seeded.group_id),
            "state": "disabled",
            "disabled_reason": "ai_off",
            "current_facts_hash": None,
            "summary": None,
        }
    assert llm.calls == []


async def test_get_missing_then_post_generates_with_cited_facts(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-api-ready")
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)

    missing = await client.get(seeded.summary_url)
    assert missing.status_code == 200, missing.text
    assert missing.json()["state"] == "missing"
    assert llm.calls == []

    ready = await client.post(seeded.summary_url)
    assert ready.status_code == 200, ready.text
    body = ready.json()
    assert body["state"] == "ready"
    summary = body["summary"]
    fact_ids = {fact["id"] for fact in summary["facts"]}
    for sentence in summary["sentences"]:
        assert "[" not in sentence["text"]
        if sentence["generated"]:
            assert sentence["fact_ids"]
            assert set(sentence["fact_ids"]) <= fact_ids
    assert summary["facts"][0]["href"] == (
        f"/o/default/p/{seeded.slug}/alerting?incident={seeded.group_id}"
    )
    assert summary["facts_hash"] == body["current_facts_hash"]

    cached = await client.post(seeded.summary_url)
    assert cached.json()["state"] == "ready"
    assert len(llm.calls) == 1


async def test_provider_failure_is_200_failed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-api-fail")
    enable_ai(monkeypatch)
    FakeLlm(None).install(monkeypatch)

    resp = await client.post(seeded.summary_url)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "failed"
    assert resp.json()["summary"] is None


async def test_regenerate_always_calls_the_llm(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-api-regen")
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)
    await client.post(seeded.summary_url)
    resp = await client.post(f"{seeded.summary_url}/regenerate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "ready"
    assert len(llm.calls) == 2


async def test_another_projects_group_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = await seed_incident(client, "sum-api-mine")
    theirs = await seed_incident(client, "sum-api-theirs")
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)
    cross = f"/api/v1/projects/{mine.slug}/alert-inbox/{theirs.group_id}/summary"
    unknown = f"/api/v1/projects/{mine.slug}/alert-inbox/{uuid.uuid4()}/summary"

    for enabled in (True, False):
        enable_ai(monkeypatch, enabled=enabled)
        for url in (cross, unknown):
            assert (await client.get(url)).status_code == 404
            assert (await client.post(url)).status_code == 404
            assert (await client.post(f"{url}/regenerate")).status_code == 404
    assert llm.calls == []


async def test_viewer_reads_and_ensures_but_cannot_regenerate(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-api-viewer")
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)
    viewer = await persisted_member_user(seeded.project_id, role="viewer")
    _as(viewer)
    try:
        got = await client.get(seeded.summary_url)
        ensured = await client.post(seeded.summary_url)
        regenerated = await client.post(f"{seeded.summary_url}/regenerate")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert (got.status_code, ensured.status_code) == (200, 200)
    assert ensured.json()["state"] == "ready"
    assert regenerated.status_code == 403
    assert len(llm.calls) == 1


async def test_a_read_key_can_get_but_not_generate(
    anon_client: AsyncClient, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-api-read-key")
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)
    issued = await client.post(
        "/api/v1/me/api-keys",
        json={"name": "ci", "scope": "read", "project_slug": seeded.slug},
    )
    assert issued.status_code == 201, issued.text
    headers = {"Authorization": f"Bearer {issued.json()['token']}"}

    got = await anon_client.get(seeded.summary_url, headers=headers)
    ensured = await anon_client.post(seeded.summary_url, headers=headers)
    regenerated = await anon_client.post(f"{seeded.summary_url}/regenerate", headers=headers)
    assert got.status_code == 200
    assert got.json()["state"] == "missing"
    assert (ensured.status_code, regenerated.status_code) == (403, 403)
    assert llm.calls == []


async def test_non_member_gets_404(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seeded = await seed_incident(client, "sum-api-outsider")
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)
    async with TestSessionLocal() as session:
        outsider = User(
            id=uuid.uuid4(),
            email=f"outsider-{uuid.uuid4().hex[:8]}@example.com",
            name="Outsider",
            password_hash=PASSWORD_HASH_PLACEHOLDER,
        )
        session.add(outsider)
        await session.commit()
    _as(outsider)
    try:
        got = await client.get(seeded.summary_url)
        ensured = await client.post(seeded.summary_url)
        regenerated = await client.post(f"{seeded.summary_url}/regenerate")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert [got.status_code, ensured.status_code, regenerated.status_code] == [404, 404, 404]
    assert llm.calls == []
