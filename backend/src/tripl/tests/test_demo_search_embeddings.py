"""Keyless demo semantic search: fixture loader, recipes, and generator script.

Everything here runs on SQLite with zero network. The Postgres-only pieces
(the ``CAST(... AS vector)`` UPDATE and the semantic SQL leg) are exercised
only through their dialect guards, mirroring how the rest of the suite treats
pgvector-dependent paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import settings
from tripl.models.plan_branch import PlanBranch
from tripl.models.search_document import SearchDocument
from tripl.schemas.search import SearchResult
from tripl.services._search_documents import (
    EMBED_TEXT_MAX_CHARS,
    build_documents,
    embed_text_for,
)
from tripl.services.app_settings_service import AiConfig, env_ai_config
from tripl.services.demo import search_embeddings
from tripl.services.demo.scenario import DEMO_RECIPE_VERSION
from tripl.services.demo.search_embeddings import (
    apply_demo_embedding_fixture,
    demo_query_embedding,
    embed_text_key,
    load_demo_embedding_fixture,
    normalize_demo_query,
)
from tripl.services.search_service import (
    _apply_demo_search_embeddings,
    _embedding_state_reusable,
)
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import search as search_tasks

# Queries the product copy tells demo users to try (frontend/src/demo/tourSteps.ts
# and scenarioModel.ts). They must stay in the generator's query list so the
# shipped fixture always carries their vectors.
_ADVERTISED_DEMO_QUERIES = ("purchase funnel", "money back")

_DIMS = settings.search_embedding_dimensions


def _write_synthetic_fixture(
    path: Path,
    *,
    recipe_version: str = DEMO_RECIPE_VERSION,
    dimensions: int | None = None,
    doc_items: dict[str, list[float]] | None = None,
    query_items: dict[str, list[float]] | None = None,
    identity_items: dict[str, str] | None = None,
) -> None:
    dims = dimensions if dimensions is not None else _DIMS
    doc_items = doc_items or {}
    query_items = query_items or {}
    if identity_items is not None:
        search_embeddings.identity_sidecar_path(path).write_text(
            json.dumps(identity_items), encoding="utf-8"
        )
    manifest = {
        "recipe_version": recipe_version,
        "model": "text-embedding-3-small",
        "dimensions": dims,
        "doc_keys": list(doc_items),
        "query_keys": list(query_items),
    }
    np.savez_compressed(
        path,
        manifest=np.array(json.dumps(manifest)),
        doc_vectors=np.asarray(list(doc_items.values()), dtype=np.float16).reshape(
            len(doc_items), dims
        ),
        query_vectors=np.asarray(list(query_items.values()), dtype=np.float16).reshape(
            len(query_items), dims
        ),
    )


@pytest.fixture
def fixture_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the loader at a per-test fixture location and reset its cache."""
    path = tmp_path / "search_embeddings_v4.npz"
    monkeypatch.setattr(search_embeddings, "_FIXTURE_PATH", path)
    search_embeddings._parse_fixture.cache_clear()
    yield path
    search_embeddings._parse_fixture.cache_clear()


def _load_generator_module() -> ModuleType:
    script_path = (
        Path(__file__).resolve().parents[3] / "scripts" / "generate_demo_search_embeddings.py"
    )
    spec = importlib.util.spec_from_file_location("_demo_embedding_generator", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclass annotation resolution looks the module
    # up in sys.modules while the script body executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_embed_text_helper_matches_worker_recipe() -> None:
    doc = SearchDocument(
        title="Purchase Completed",
        subtitle="Commerce",
        keywords="purchase revenue",
        body="B" * 10_000,
    )
    text = search_tasks._embed_text(doc)
    assert text == embed_text_for(
        title=doc.title,
        subtitle=doc.subtitle,
        keywords=doc.keywords,
        body=doc.body,
    )
    assert text.startswith("Purchase Completed\nCommerce\npurchase revenue\nB")
    assert len(text) == EMBED_TEXT_MAX_CHARS == search_tasks._EMBED_TEXT_MAX_CHARS


def test_normalize_demo_query_casefolds_and_collapses_whitespace() -> None:
    assert normalize_demo_query("  Purchase \t  FUNNEL \n") == "purchase funnel"
    assert normalize_demo_query("Onboarding Drop-Off") == "onboarding drop-off"
    assert normalize_demo_query("") == ""


def test_embed_text_key_is_sha256_of_utf8_text() -> None:
    text = "Trial Started\nCommerce\ntrial subscription\nbody"
    assert embed_text_key(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_loader_returns_none_when_fixture_missing(fixture_path: Path) -> None:
    assert load_demo_embedding_fixture() is None
    assert demo_query_embedding("purchase funnel") is None


def test_loader_rejects_recipe_version_mismatch(fixture_path: Path) -> None:
    _write_synthetic_fixture(fixture_path, recipe_version="999")
    assert load_demo_embedding_fixture() is None


def test_loader_rejects_dimension_mismatch(fixture_path: Path) -> None:
    _write_synthetic_fixture(
        fixture_path,
        dimensions=_DIMS + 1,
        doc_items={"a" * 64: [0.5] * (_DIMS + 1)},
    )
    assert load_demo_embedding_fixture() is None


def test_loader_rejects_unreadable_file(fixture_path: Path) -> None:
    fixture_path.write_bytes(b"not an npz archive")
    assert load_demo_embedding_fixture() is None


def test_loader_rejects_truncated_or_corrupt_zip(fixture_path: Path) -> None:
    """Files with valid zip magic raise BadZipFile (not ValueError) from np.load.

    Simulates an interrupted generator run / partial deploy; the loader's
    never-raise contract must hold, or every demo search and provisioning
    request 500s.
    """
    _write_synthetic_fixture(fixture_path, query_items={"purchase funnel": [0.25] * _DIMS})
    raw = fixture_path.read_bytes()
    fixture_path.write_bytes(raw[: len(raw) * 3 // 5])
    assert load_demo_embedding_fixture() is None

    search_embeddings._parse_fixture.cache_clear()
    fixture_path.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    assert load_demo_embedding_fixture() is None


def test_loader_roundtrips_synthetic_fixture(fixture_path: Path) -> None:
    doc_key = "d" * 64
    _write_synthetic_fixture(
        fixture_path,
        doc_items={doc_key: [0.5] * _DIMS},
        query_items={"purchase funnel": [0.25] * _DIMS},
    )
    fixture = load_demo_embedding_fixture()
    assert fixture is not None
    assert fixture.dimensions == _DIMS
    assert fixture.model == "text-embedding-3-small"
    # 0.5 / 0.25 are exactly representable in float16, so the roundtrip is exact.
    assert fixture.doc_vectors[doc_key] == [0.5] * _DIMS

    # Query lookup normalizes before matching; misses return None.
    assert demo_query_embedding("  Purchase   FUNNEL ") == [0.25] * _DIMS
    assert demo_query_embedding("nonexistent query") is None


async def test_apply_helper_is_noop_on_sqlite(fixture_path: Path) -> None:
    _write_synthetic_fixture(
        fixture_path,
        query_items={"purchase funnel": [0.25] * _DIMS},
    )
    async with TestSessionLocal() as session:
        applied = await _apply_demo_search_embeddings(
            session,
            project_id=uuid.uuid4(),
            branch_id=uuid.uuid4(),
            ai_config=env_ai_config(),
        )
    assert applied == 0


class _StubResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _StubSession:
    """Duck-typed AsyncSession: the first execute is the row SELECT, every
    subsequent parameterized execute is a stamping UPDATE."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows
        self.update_params: list[dict[str, object]] = []

    async def execute(
        self, statement: object, params: dict[str, object] | None = None
    ) -> _StubResult:
        if params is None:
            return _StubResult(self._rows)
        self.update_params.append(params)
        return _StubResult([])


def _doc_row(
    *,
    title: str,
    subtitle: str = "",
    keywords: str = "",
    body: str = "",
    embedding_status: str = "pending",
    entity_type: str = "event",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        entity_type=entity_type,
        title=title,
        subtitle=subtitle,
        keywords=keywords,
        body=body,
        embedding_status=embedding_status,
    )


async def test_apply_fixture_stamps_only_matching_non_ready_rows(fixture_path: Path) -> None:
    matched = _doc_row(
        title="Buy Button Click",
        subtitle="UI Interaction",
        keywords="buy click",
        body="User taps the buy button",
    )
    already_ready = _doc_row(
        title="Buy Button Click",
        subtitle="UI Interaction",
        keywords="buy click",
        body="User taps the buy button",
        embedding_status="ready",
    )
    unmatched = _doc_row(title="Not In Fixture", embedding_status="disabled")
    matched_key = embed_text_key(
        embed_text_for(
            title=matched.title,
            subtitle=matched.subtitle,
            keywords=matched.keywords,
            body=matched.body,
        )
    )
    _write_synthetic_fixture(fixture_path, doc_items={matched_key: [0.5] * _DIMS})

    session = _StubSession([matched, already_ready, unmatched])
    applied = await apply_demo_embedding_fixture(
        cast(AsyncSession, session),
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
    )

    # Idempotency: 'ready' rows are skipped; unmatched rows are left untouched.
    assert applied == 1
    assert len(session.update_params) == 1
    params = session.update_params[0]
    assert params["document_id"] == matched.id
    assert params["embedding_model"] == "text-embedding-3-small"
    embedding_param = params["embedding"]
    assert isinstance(embedding_param, str)
    assert embedding_param.startswith("[0.50000000,")


class _FakePostgresDemoSession:
    """Postgres-flavored session for exercising _apply_demo_search_embeddings
    guards; any row access is a test failure."""

    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def scalar(self, statement: object) -> bool:
        return True

    async def execute(self, statement: object, params: dict[str, object] | None = None) -> None:
        raise AssertionError("must not touch rows when the guard should skip stamping")


async def test_apply_helper_skips_stamping_when_live_model_differs(fixture_path: Path) -> None:
    """Live embeddings under a non-fixture model: stamping would mix vector
    spaces and hide the pending rows from the embedding worker."""
    _write_synthetic_fixture(fixture_path, query_items={"purchase funnel": [0.25] * _DIMS})
    ai_config = replace(
        env_ai_config(),
        search_embeddings_enabled=True,
        search_embedding_model="text-embedding-3-large",
    )
    applied = await _apply_demo_search_embeddings(
        cast(AsyncSession, _FakePostgresDemoSession()),
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        ai_config=ai_config,
    )
    assert applied == 0


async def test_apply_helper_skips_stamping_when_live_model_matches_fixture(
    fixture_path: Path,
) -> None:
    """Live embeddings under the fixture's own model still must not stamp.

    Live rows and live queries match on the embedding provenance hash, which
    the fixture cannot carry. A stamped row would never match a live query,
    and the stale-search sweep would pick its branch on every run.
    """
    _write_synthetic_fixture(fixture_path, query_items={"purchase funnel": [0.25] * _DIMS})
    ai_config = replace(
        env_ai_config(),
        search_embeddings_enabled=True,
        search_embedding_model=load_demo_embedding_fixture().model,
    )
    applied = await _apply_demo_search_embeddings(
        cast(AsyncSession, _FakePostgresDemoSession()),
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        ai_config=ai_config,
    )
    assert applied == 0


def test_embedding_state_reusable_keeps_fixture_stamped_rows_when_disabled() -> None:
    fixture_model = "text-embedding-3-small"
    # Keyless demo: a stamped row ('ready' under the fixture's model) survives.
    assert _embedding_state_reusable(
        status="ready",
        model=fixture_model,
        enabled=False,
        current_model=fixture_model,
        demo_fixture_model=fixture_model,
    )
    # A ready row under any other model is still dropped.
    assert not _embedding_state_reusable(
        status="ready",
        model="text-embedding-3-large",
        enabled=False,
        current_model=fixture_model,
        demo_fixture_model=fixture_model,
    )
    # Without demo fixture context the pre-existing rule is unchanged.
    assert not _embedding_state_reusable(
        status="ready",
        model=fixture_model,
        enabled=False,
        current_model=fixture_model,
        demo_fixture_model=None,
    )
    assert _embedding_state_reusable(
        status="disabled",
        model=None,
        enabled=False,
        current_model=fixture_model,
        demo_fixture_model=fixture_model,
    )


async def test_postgres_search_demo_fallback_uses_canned_query_vector(
    fixture_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_synthetic_fixture(fixture_path, query_items={"purchase funnel": [0.25] * _DIMS})
    from tripl.services import _search_query

    semantic_calls: list[list[float]] = []

    async def fake_lexical(session: AsyncSession, **kwargs: object) -> list[SearchResult]:
        return []

    async def fake_semantic(
        session: AsyncSession, *, embedding: list[float], **kwargs: object
    ) -> list[SearchResult]:
        semantic_calls.append(embedding)
        return []

    monkeypatch.setattr(_search_query, "postgres_lexical_search", fake_lexical)
    monkeypatch.setattr(_search_query, "postgres_semantic_search", fake_semantic)

    async def run(query: str, *, project_is_demo: bool) -> bool:
        async with TestSessionLocal() as session:
            _, semantic_used = await _search_query.postgres_search(
                session,
                project_id=uuid.uuid4(),
                branch_id=uuid.uuid4(),
                query=query,
                entity_types=None,
                include_archived=False,
                limit=10,
                project_is_demo=project_is_demo,
            )
        return semantic_used

    # Canned-vector hit: normalization applies, semantic leg runs, flag flips.
    assert await run("  Purchase   FUNNEL ", project_is_demo=True) is True
    assert semantic_calls == [[0.25] * _DIMS]

    # Non-demo projects never consult the fixture.
    semantic_calls.clear()
    assert await run("purchase funnel", project_is_demo=False) is False
    assert semantic_calls == []

    # Fixture misses and sub-3-character queries stay lexical-only.
    assert await run("unknown demo query", project_is_demo=True) is False
    assert await run("pu", project_is_demo=True) is False
    assert semantic_calls == []


# Bounds on the fake lexical leg's wait below. Nothing here measures a duration:
# they pin the ORDERING, so the lexical leg is provably still suspended at the
# moment its sibling fails.
_EMBED_FAILURE_TIMEOUT_SECONDS = 5.0
_EMBED_FAILURE_POLL_SECONDS = 0.005


async def test_postgres_search_serves_lexical_results_when_the_embed_leg_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An embed leg that raises degrades to lexical instead of 500ing.

    ``embed_texts`` degrades to ``[]`` for everything it anticipates, but the
    ``asyncio.gather`` here carried no ``return_exceptions``, so anything it did
    NOT anticipate propagated the instant it was raised — and the lexical
    coroutine was left running SQL on the request's AsyncSession while the
    request unwound. Both halves are asserted: the response, and that the
    stranded leg is awaited rather than abandoned.
    """
    from tripl.services import _search_query, app_settings_service

    hit = SearchResult(
        id=uuid.uuid4(),
        entity_type="event",
        entity_id=uuid.uuid4(),
        title="purchase_completed",
        route_path="/p/demo/events/purchase_completed",
        score=5.0,
    )
    embed_failed = threading.Event()
    lexical_completed: list[bool] = []

    async def fake_lexical(session: AsyncSession, **kwargs: object) -> list[SearchResult]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _EMBED_FAILURE_TIMEOUT_SECONDS
        while not embed_failed.is_set():
            if loop.time() > deadline:
                pytest.fail("the embedding leg had not failed while the lexical leg was running")
            await asyncio.sleep(_EMBED_FAILURE_POLL_SECONDS)
        lexical_completed.append(True)
        return [hit]

    def failing_embed(query: str, *, config: AiConfig, timeout: float) -> list[float]:
        embed_failed.set()
        raise ValueError("the provider answered 200 with a body that is not JSON")

    async def fake_semantic(
        session: AsyncSession, *, embedding: list[float], **kwargs: object
    ) -> list[SearchResult]:
        pytest.fail("the semantic leg ran without an embedding")

    enabled = replace(env_ai_config(), search_embeddings_enabled=True)

    async def fake_ai_config(session: AsyncSession, **_kwargs: object) -> AiConfig:
        return enabled

    monkeypatch.setattr(_search_query, "postgres_lexical_search", fake_lexical)
    monkeypatch.setattr(_search_query, "postgres_semantic_search", fake_semantic)
    monkeypatch.setattr(_search_query, "embed_query", failing_embed)
    monkeypatch.setattr(app_settings_service, "get_search_embedding_config", fake_ai_config)

    async with TestSessionLocal() as session:
        results, semantic_used = await _search_query.postgres_search(
            session,
            project_id=uuid.uuid4(),
            branch_id=uuid.uuid4(),
            query="purchase",
            entity_types=None,
            include_archived=False,
            limit=10,
        )

    assert [item.title for item in results] == ["purchase_completed"]
    assert semantic_used is False
    assert lexical_completed == [True]


async def test_generator_script_end_to_end_with_fake_embedder(
    fixture_path: Path,
) -> None:
    generator = _load_generator_module()

    calls: list[int] = []

    def fake_embedder(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        vectors: list[list[float]] = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
            value = ((seed % 128) - 64) / 128.0
            vectors.append([value] * _DIMS)
        return vectors

    result = await generator.generate_fixture(
        fixture_path,
        embedder=fake_embedder,
        model="fake-embedding-model",
        batch_size=32,
    )
    assert result.doc_count > 0
    assert result.query_count == len(generator.DEMO_SEARCH_QUERIES)
    assert result.dimensions == _DIMS
    assert all(batch <= 32 for batch in calls)

    # The written fixture must round-trip through the runtime loader.
    search_embeddings._parse_fixture.cache_clear()
    fixture = load_demo_embedding_fixture()
    assert fixture is not None
    assert fixture.model == "fake-embedding-model"
    assert len(fixture.doc_vectors) == result.doc_count
    assert set(fixture.query_vectors) == {
        normalize_demo_query(query) for query in generator.DEMO_SEARCH_QUERIES
    }
    for query in generator.DEMO_SEARCH_QUERIES:
        embedding = demo_query_embedding(query)
        assert embedding is not None
        assert len(embedding) == _DIMS


async def test_apply_fixture_falls_back_to_identity_when_a_scan_rewrote_the_text(
    fixture_path: Path,
) -> None:
    """A scan-mutated document keeps its vector via the identity fallback.

    The demo runs the REAL scan/collection pipeline, which rewrites the very text
    the fixture key is derived from (observed field values replace ``${var}``
    templates, catalog sync appends auto-created field descriptors). Keyed on
    embed text alone, one scan orphaned EVERY document and left the whole index
    ``embedding_status='disabled'`` with no provider to re-embed it, so the
    demo's own advertised "money back" query stopped returning any event.
    """
    pristine = _doc_row(
        title="Refund Processed",
        subtitle="Purchase",
        keywords="refund purchase",
        body="A refund was issued to the user. product_id ${product_id}",
    )
    pristine_key = embed_text_key(
        embed_text_for(
            title=pristine.title,
            subtitle=pristine.subtitle,
            keywords=pristine.keywords,
            body=pristine.body,
        )
    )
    # Same entity, post-scan body: literal observed values plus an auto-created
    # field descriptor. Its embed-text key is NOT in the fixture.
    rescanned = _doc_row(
        title="Refund Processed",
        subtitle="Purchase",
        keywords="refund purchase s29_5",
        body="A refund was issued to the user. product_id prod_lifetime "
        "session_id Auto-created (String)",
    )
    identity = search_embeddings.identity_key(
        rescanned.entity_type, rescanned.title, rescanned.subtitle
    )

    _write_synthetic_fixture(
        fixture_path,
        doc_items={pristine_key: [0.5] * _DIMS},
        identity_items={identity: pristine_key},
    )
    fixture = load_demo_embedding_fixture()
    assert fixture is not None
    assert fixture.identity_vectors == {identity: [0.5] * _DIMS}

    session = _StubSession([rescanned])
    applied = await apply_demo_embedding_fixture(
        cast(AsyncSession, session),
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
    )

    assert applied == 1, "a scan-rewritten demo document must keep its precomputed vector"
    assert session.update_params[0]["document_id"] == rescanned.id


def test_loader_survives_a_missing_or_broken_identity_sidecar(fixture_path: Path) -> None:
    """The fallback is an enhancement: without it the fixture still loads."""
    doc_key = "d" * 64
    _write_synthetic_fixture(fixture_path, doc_items={doc_key: [0.5] * _DIMS})
    fixture = load_demo_embedding_fixture()
    assert fixture is not None
    assert fixture.identity_vectors == {}

    search_embeddings.identity_sidecar_path(fixture_path).write_text("{ not json", encoding="utf-8")
    search_embeddings._parse_fixture.cache_clear()
    fixture = load_demo_embedding_fixture()
    assert fixture is not None
    assert fixture.identity_vectors == {}


def test_advertised_demo_queries_are_in_generator_query_list() -> None:
    """The tour/scenario copy hardcodes queries; the generator list must keep
    covering them or the advertised flow silently loses its semantic leg."""
    generator = _load_generator_module()
    normalized = {normalize_demo_query(query) for query in generator.DEMO_SEARCH_QUERIES}
    advertised = {normalize_demo_query(query) for query in _ADVERTISED_DEMO_QUERIES}
    assert advertised <= normalized


async def test_shipped_fixture_covers_current_demo_documents() -> None:
    """CI freshness gate: every demo document still resolves to a fixture vector.

    Skips until a maintainer generates the real fixture; once it ships, this
    fails whenever the demo drifts past what the fixture can still resolve,
    pointing at the regeneration script.

    It used to require an exact embed-text hit for EVERY document. The ranking
    fixes (one dropped harvested values from variable keywords, another
    added the spaced alias of every identifier) changed the indexed text on
    purpose, so the embed-text key of every document those fixes touch has moved
    and will keep missing until a maintainer regenerates the fixture with a real
    embedding key. Asserting it wholesale would have made this a permanent red
    that says nothing about the demo.

    THE EMBED-TEXT GATE IS NARROWED, NOT DELETED
    ---------------------------------------------------------------
    Routing the whole check through ``fixture.vector_for`` was the wrong repair:
    that resolver falls back to ``identity_vectors``, and identity coverage is
    asserted separately below — so "resolved is None" required BOTH lookups to
    miss and could not fail while the identity assertion passed. The freshness
    gate had become arithmetic, not a test.

    So the embed-text half is asserted where it is still exactly true: on TAG
    documents. ``_search_documents._tag_document`` is built from the tag name,
    the event name, the event description and the event-type display name, and
    is the one builder the spaced-alias fix deliberately did NOT touch (appending a spaced
    alias to a tag's keywords would have deleted the 4.0 "keywords ARE the
    query" tier) — nor does the harvested-values fix reach it. Their embed text is therefore
    byte-identical to what was embedded, 21 of the 63 shipped vectors, and any
    drift in the demo's tags, event names or descriptions — or in the embed-text
    recipe itself — turns this red and points at the regeneration script. That is
    the gate this test was written to be, scoped to where it can still be honest.
    """
    if not search_embeddings.DEFAULT_FIXTURE_PATH.is_file():
        pytest.skip("fixture not generated yet — run scripts/generate_demo_search_embeddings.py")

    search_embeddings._parse_fixture.cache_clear()
    fixture = load_demo_embedding_fixture()
    # The loader already rejects recipe/dimension mismatches; a None here means
    # the shipped fixture is stale for the current recipe or configuration.
    assert fixture is not None, (
        "shipped fixture rejected by loader — regenerate via "
        "scripts/generate_demo_search_embeddings.py"
    )
    assert fixture.dimensions == _DIMS
    assert fixture.model == settings.search_embedding_model

    # Query-vector freshness: every generator query (a superset of the queries
    # the tour copy advertises, per the test above) must have a canned vector.
    generator = _load_generator_module()
    expected_queries = {normalize_demo_query(query) for query in generator.DEMO_SEARCH_QUERIES}
    missing_queries = expected_queries - set(fixture.query_vectors)
    assert not missing_queries, (
        "demo queries missing from the embedding fixture (regenerate via "
        f"scripts/generate_demo_search_embeddings.py): {sorted(missing_queries)}"
    )

    from tripl.services import demo_service

    async with TestSessionLocal() as session:
        project = await demo_service.create_demo_project(session, slug="demo-fixture-check")
        branch_ids = list(
            (
                await session.execute(
                    select(PlanBranch.id).where(PlanBranch.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(branch_ids) >= 2  # main + the deep-copied feature branch
        stale_embed_text: list[str] = []
        missing_identities: list[str] = []
        disagreeing: list[str] = []
        unresolvable: list[str] = []
        # identity -> the distinct embed-text keys documents under it produced.
        # More than one means two documents share an identity, which is the only
        # thing that may make the two lookups disagree.
        identity_texts: dict[str, set[str]] = {}
        tag_documents = 0
        for branch_id in branch_ids:
            documents = await build_documents(session, project.id, branch_id, project.slug)
            for doc in documents:
                identity = search_embeddings.identity_key(doc.entity_type, doc.title, doc.subtitle)
                embed_key = embed_text_key(
                    embed_text_for(
                        title=doc.title,
                        subtitle=doc.subtitle,
                        keywords=doc.keywords,
                        body=doc.body,
                    )
                )
                by_embed_text = fixture.doc_vectors.get(embed_key)
                by_identity = fixture.identity_vectors.get(identity)

                # THE EMBED-TEXT FRESHNESS GATE. See the docstring for why it is
                # scoped to tags: they are the documents whose searchable text
                # the ranking fixes provably did not move, so a miss here is real
                # drift rather than the known, deliberate key shift.
                if doc.entity_type == "tag":
                    tag_documents += 1
                    if by_embed_text is None:
                        stale_embed_text.append(f"{doc.entity_type}:{doc.title}")

                if by_identity is None:
                    missing_identities.append(f"{doc.entity_type}:{doc.title}")
                elif by_embed_text is not None and by_embed_text != by_identity:
                    disagreeing.append(f"{doc.entity_type}:{doc.title}|{identity}")
                identity_texts.setdefault(identity, set()).add(embed_key)
                # The sidecar maps identity -> embed-text key, so when both
                # lookups hit they must land on the SAME vector. A disagreement
                # means the sidecar was derived from a different archive than the
                # one shipped beside it, and the fallback would hand a document
                # some other entity's vector — a failure no other assertion here
                # can see, because both halves are individually "present".
                # A DISAGREEMENT IS ONLY ACCEPTABLE WHEN A COLLISION EXPLAINS IT.
                #
                # An earlier note recorded three documents whose two lookups land on
                # different vectors and diagnosed it as the archive and the sidecar
                # having been generated from different demo builds. That diagnosis
                # was wrong: both artifacts were regenerated from ONE build on
                # 2026-08-16 and the same three documents still disagree.
                #
                # The cause is structural. `identity_key` is (entity_type, title,
                # subtitle) and deliberately carries NO branch: a shipped fixture
                # cannot name branch ids, because the demo mints fresh ones on every
                # provisioning. The demo has two branches, and the feature branch
                # edits one event's description, so that event — and the tag
                # documents whose body quotes it — exist twice under one identity
                # with different text. The sidecar maps an identity to one vector,
                # so the other copy necessarily disagrees.
                #
                # That is benign: the loser's fallback vector is the SAME entity
                # from the sibling branch, which is what you would want an
                # approximation to be. What would not be benign is a disagreement
                # with no collision behind it — that really would mean the two
                # artifacts came from different builds, and the fallback would hand
                # a document some unrelated entity's vector.

                if fixture.vector_for(embed_key=embed_key, identity=identity) is None:
                    unresolvable.append(f"{doc.entity_type}:{doc.title}")

    assert tag_documents > 0, (
        "the demo built no tag documents, so the embed-text freshness gate below "
        "measured nothing — the demo recipe changed shape and this test needs rescoping"
    )
    assert not stale_embed_text, (
        "tag documents whose embed text is no longer in the fixture archive. Their "
        "builder is untouched by the ranking fixes, so this is real demo drift "
        "(regenerate via scripts/generate_demo_search_embeddings.py): "
        f"{stale_embed_text}"
    )
    # Identity-key coverage is what keeps semantic search alive AFTER a scan
    # rewrites a document's text — a shipped fixture with no
    # sidecar would silently regress to the pre-fix behaviour.
    assert not missing_identities, (
        "demo documents missing from the fixture's identity sidecar (regenerate via "
        f"scripts/generate_demo_search_embeddings.py): {missing_identities}"
    )
    unexplained = [
        entry for entry in disagreeing if len(identity_texts.get(entry.split("|", 1)[1], ())) < 2
    ]
    assert not unexplained, (
        "the identity sidecar and the fixture archive disagree about these documents' "
        "vectors, and NO second document shares their identity, so a collision does not "
        "explain it — the two artifacts came from different demo builds and the fallback "
        "would hand these documents an unrelated entity's vector (regenerate BOTH via "
        f"scripts/generate_demo_search_embeddings.py): {unexplained}"
    )
    # Stated last and on purpose: this is the production invariant the demo
    # depends on — no document may end up with no vector to stamp — and with the
    # identity assertion above passing it is implied rather than independent. It
    # stays because it is the claim `apply_demo_embedding_fixture` actually
    # makes, so a future change to `vector_for` that breaks resolution without
    # emptying the sidecar is caught here and nowhere else.
    assert not unresolvable, (
        "demo documents the embedding fixture cannot resolve by embed text OR by "
        "identity (regenerate via scripts/generate_demo_search_embeddings.py): "
        f"{unresolvable}"
    )
