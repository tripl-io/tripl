"""Incident summary service (F14, #267): caching, regeneration, AI-off and failures.

The LLM is always mocked (``FakeLlm``); ``llm_service.is_enabled`` is patched to
switch AI on or off.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, tzinfo

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.incident_summary import IncidentSummary
from tripl.models.project import Project
from tripl.schemas.incident_summary import IncidentSummaryResponse
from tripl.services import incident_summary_service
from tripl.services._incident_summary_output import RESPONSE_FORMAT, UNKNOWN_CAUSE_TEXT
from tripl.tests._incident_summary_seed import (
    BUCKET,
    GOOD_REPLY,
    SAMPLE_VALUE,
    SENSITIVE_VALUE,
    FakeLlm,
    SeededIncident,
    add_item,
    add_sensitive_attribution,
    enable_ai,
    registered_user,
    seed_incident,
)
from tripl.tests.conftest import TestSessionLocal

pytestmark = pytest.mark.asyncio


async def _get(seeded: SeededIncident) -> IncidentSummaryResponse:
    async with TestSessionLocal() as session:
        return await incident_summary_service.get_summary(session, seeded.slug, seeded.group_id)


async def _ensure(seeded: SeededIncident, *, force: bool = False) -> IncidentSummaryResponse:
    user = await registered_user()
    async with TestSessionLocal() as session:
        return await incident_summary_service.ensure_summary(
            session, seeded.slug, seeded.group_id, user, force=force
        )


async def _rows() -> list[IncidentSummary]:
    async with TestSessionLocal() as session:
        return list((await session.execute(select(IncidentSummary))).scalars())


async def test_ai_off_is_disabled_without_llm_or_fact_queries(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-svc-off")
    enable_ai(monkeypatch, enabled=False)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)

    async def _no_facts(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("facts gathered while AI is off")

    monkeypatch.setattr(incident_summary_service, "gather_incident_facts", _no_facts)

    got = await _get(seeded)
    ensured = await _ensure(seeded)
    forced = await _ensure(seeded, force=True)

    for response in (got, ensured, forced):
        assert response.state == "disabled"
        assert response.disabled_reason == "ai_off"
        assert response.current_facts_hash is None
        assert response.summary is None
    assert llm.calls == []
    assert await _rows() == []


async def test_demo_project_is_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-svc-demo")
    async with TestSessionLocal() as session:
        project = await session.get(Project, seeded.project_id)
        assert project is not None
        project.is_demo = True
        await session.commit()
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)

    ensured = await _ensure(seeded, force=True)
    assert (ensured.state, ensured.disabled_reason) == ("disabled", "demo")
    assert llm.calls == []


async def test_missing_then_generated_then_cached(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-svc-cache")
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)

    missing = await _get(seeded)
    assert missing.state == "missing"
    assert missing.current_facts_hash
    assert llm.calls == []

    ready = await _ensure(seeded)
    assert ready.state == "ready"
    assert ready.summary is not None
    assert ready.summary.facts_hash == missing.current_facts_hash
    assert ready.summary.sentences[0].fact_ids == [1]
    assert ready.summary.sentences[-1].text == UNKNOWN_CAUSE_TEXT
    assert ready.summary.cause_known is False
    assert len(llm.calls) == 1
    assert llm.calls[0]["response_format"] == RESPONSE_FORMAT
    assert llm.calls[0]["system"] == incident_summary_service.SYSTEM_PROMPT

    again = await _ensure(seeded)
    got = await _get(seeded)
    assert again.state == "ready"
    assert got.state == "ready"
    assert got.summary == ready.summary
    assert len(llm.calls) == 1
    assert len(await _rows()) == 1


async def test_stale_regenerates_on_a_fact_change(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-svc-stale")
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)
    first = await _ensure(seeded)
    assert first.summary is not None

    await add_item(seeded, bucket=BUCKET + timedelta(hours=1), actual=10)
    stale = await _get(seeded)
    assert stale.state == "stale"
    assert stale.summary == first.summary
    assert stale.current_facts_hash != first.summary.facts_hash

    fresh = await _ensure(seeded)
    assert fresh.state == "ready"
    assert fresh.summary is not None
    assert fresh.summary.facts_hash == stale.current_facts_hash
    assert len(llm.calls) == 2
    rows = await _rows()
    assert [row.facts_hash for row in rows] == [stale.current_facts_hash]


async def test_force_regenerates_an_unchanged_summary(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-svc-force")
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)
    await _ensure(seeded)
    forced = await _ensure(seeded, force=True)
    assert forced.state == "ready"
    assert len(llm.calls) == 2
    assert len(await _rows()) == 1


async def test_failure_keeps_the_previous_body(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-svc-fail")
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)
    good = await _ensure(seeded)
    assert good.summary is not None

    await add_item(seeded, bucket=BUCKET + timedelta(hours=1), actual=10)
    for reply in (None, "not json and no citations", '{"sentences": []}'):
        llm.reply = reply
        failed = await _ensure(seeded)
        assert failed.state == "failed"
        assert failed.summary is None
        assert failed.current_facts_hash is not None

    rows = await _rows()
    assert [row.facts_hash for row in rows] == [good.summary.facts_hash]
    stale = await _get(seeded)
    assert stale.state == "stale"
    assert stale.summary == good.summary


async def test_a_slower_older_generation_does_not_overwrite_a_newer_one(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-svc-race")
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)
    newer = await _ensure(seeded)
    assert newer.summary is not None

    # A generation that started before the stored one was written: the stored
    # row is newer than its start, so it must be kept, not overwritten.
    earlier = newer.summary.generated_at - timedelta(minutes=5)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Clock:
            return cls.fromtimestamp(earlier.timestamp(), tz)

    monkeypatch.setattr(incident_summary_service, "datetime", _Clock)
    llm.reply = GOOD_REPLY.replace("dropped 75%", "fell sharply")
    older = await _ensure(seeded, force=True)
    assert len(llm.calls) == 2
    assert older.state == "ready"
    assert older.summary == newer.summary
    rows = await _rows()
    assert len(rows) == 1
    assert "fell sharply" not in str(rows[0].sentences)


async def test_first_failure_stores_nothing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-svc-first-fail")
    enable_ai(monkeypatch)
    FakeLlm(None).install(monkeypatch)
    failed = await _ensure(seeded)
    assert failed.state == "failed"
    assert await _rows() == []
    assert (await _get(seeded)).state == "missing"


async def test_sensitive_values_never_reach_the_prompt(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-svc-sensitive", sample_value=SAMPLE_VALUE)
    await add_sensitive_attribution(seeded)
    enable_ai(monkeypatch)
    llm = FakeLlm(GOOD_REPLY).install(monkeypatch)

    await _ensure(seeded)
    assert len(llm.calls) == 1
    prompt = llm.calls[0]["user"]
    assert "(redacted)" in prompt
    assert SENSITIVE_VALUE not in prompt
    assert SAMPLE_VALUE not in prompt
    assert "[1] (incident)" in prompt


async def test_unknown_group_is_404_even_when_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await seed_incident(client, "sum-svc-404")
    enable_ai(monkeypatch, enabled=False)
    other = replace(seeded, group_id=uuid.uuid4())
    with pytest.raises(HTTPException) as caught:
        await _get(other)
    assert caught.value.status_code == 404
