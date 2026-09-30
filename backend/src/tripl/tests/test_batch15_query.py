"""Regressions for search query provenance, display offsets, and confidence."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tripl.schemas.search import SearchResult
from tripl.services import _search_query as query_module


@pytest.mark.asyncio
async def test_semantic_sql_filters_embedding_provenance() -> None:
    class Result:
        def mappings(self) -> Result:
            return self

        def all(self) -> list[object]:
            return []

    session = SimpleNamespace(execute=AsyncMock(return_value=Result()))
    await query_module.postgres_semantic_search(
        session,
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        embedding=[0.1, 0.2],
        embedding_model="provider-fingerprint",
        entity_types=None,
        include_archived=False,
        limit=10,
    )
    statement, params = session.execute.await_args.args
    assert "d.embedding_model = :embedding_model" in str(statement)
    assert params["embedding_model"] == "provider-fingerprint"


@pytest.mark.asyncio
async def test_query_embed_uses_short_timeout_and_current_provenance(monkeypatch) -> None:
    config = SimpleNamespace(search_embeddings_enabled=True)
    monkeypatch.setattr(
        query_module.app_settings_service,
        "get_search_embedding_config",
        AsyncMock(return_value=config),
    )
    monkeypatch.setattr(query_module, "embedding_provenance", lambda cfg: "current-provider")
    monkeypatch.setattr(query_module, "postgres_lexical_search", AsyncMock(return_value=[]))
    embed_calls: list[dict[str, object]] = []

    def fake_embed(text: str, **kwargs: object) -> list[float]:
        embed_calls.append(kwargs)
        return [0.1] * 1536

    semantic_calls: list[dict[str, object]] = []

    async def fake_semantic(*args: object, **kwargs: object) -> list[SearchResult]:
        semantic_calls.append(kwargs)
        return []

    monkeypatch.setattr(query_module, "embed_query", fake_embed)
    monkeypatch.setattr(query_module, "postgres_semantic_search", fake_semantic)
    await query_module.postgres_search(
        SimpleNamespace(),
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        query="screen",
        entity_types=None,
        include_archived=False,
        limit=10,
    )
    assert embed_calls == [{"config": config, "timeout": 3}]
    assert semantic_calls[0]["embedding_model"] == "current-provider"


def test_snippet_and_highlight_use_raw_offsets_after_whitespace_and_casefold() -> None:
    body = "\n".join(f"    * detail {i}" for i in range(40)) + "\n    Straße target"
    snippet = query_module.snippet(body, "strasse target", length=100)
    assert "Straße target" in snippet
    assert "Straße target" in query_module.highlights("", body, "strasse")[0]


def test_semantic_cosine_cannot_make_non_identity_hit_certain() -> None:
    result = SearchResult(
        id=uuid.uuid4(),
        entity_type="event",
        entity_id=uuid.uuid4(),
        title="Another event",
        route_path="/events/another",
        score=2.4,
    )
    result.record_semantic_cosine(0.97)
    assert query_module.finalize_results([result], 1)[0].confidence == 0.8
    result.record_identity_match(identity=True)
    assert query_module.finalize_results([result], 1)[0].confidence == 0.97
