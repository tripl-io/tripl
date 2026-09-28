from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

from tripl.config import settings
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.search_document import SearchDocument
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.schemas.search import SearchResult
from tripl.services import _search_query, app_settings_service, search_service
from tripl.services._search_documents import build_documents
from tripl.services._search_query import (
    _FULL_CONFIDENCE_SCORE,
    _PARTIAL_CONFIDENCE_CEILING,
    _SEMANTIC_SCORE_WEIGHT,
    _SQLITE_ALL_TOKENS,
    _SQLITE_EXACT_KEYWORDS,
    _SQLITE_EXACT_TITLE,
    _SQLITE_KEYWORD_TOKEN,
    _SQLITE_TITLE_PREFIX,
    identifier_form,
)
from tripl.services.app_settings_service import AiConfig, env_ai_config
from tripl.services.search_service import (
    CANDIDATE_WINDOW,
    _finalize_results,
    _sanitize_query,
    _token_boundary_regex,
    fallback_score,
    merge_results,
)
from tripl.tests.conftest import TestSessionLocal


def _result(
    *,
    entity_type: str,
    title: str,
    score: float,
    subtitle: str = "",
) -> SearchResult:
    """A SQLite-path result: scores here are ``fallback_score`` tier values.

    It records the identity flag the same way ``document_to_result`` does, and
    that is not cosmetic (tripl-d5u8): ``finalize_results`` caps a non-identity
    match below the certainty line, so a helper that skipped the flag would drag
    a legitimate exact-title 1.0 down to the partial ceiling and the tests would
    be measuring the helper rather than the code.
    """
    result = SearchResult(
        id=uuid.uuid4(),
        entity_type=entity_type,  # type: ignore[arg-type]
        entity_id=uuid.uuid4(),
        title=title,
        subtitle=subtitle,
        route_path="/",
        score=score,
    )
    result.record_identity_match(identity=score >= _SQLITE_EXACT_KEYWORDS)
    return result


def test_a_matched_event_type_is_not_buried_under_its_own_events() -> None:
    """Ranking is the merged score alone — no event-type boost (tripl-0tt4 item 4).

    ``_finalize_results`` used to multiply every event whose ``subtitle`` named an
    ``event_type`` document in the same candidate set by up to 1.75, while leaving
    the type document itself unmultiplied. This test used to assert that lift.

    It was asserting a defect. Measured on production at the shipped candidate
    window, the only queries that ever pulled a type document into the set were
    the type's own name, and there the multiplier pushed the very document the
    user had named underneath its own members: ``q='pv'`` served "Pageview" at
    rank 100 of 100 on acme-web and on acme-ios, rank 1 without the boost.

    The set below is that production shape in miniature. Under the old boost the
    two Pageviews events banked 3.5 and 3.325 and displaced BOTH the type
    document (3.0) and the higher-scoring event of another type (2.5); the order
    asserted here is what the scores actually say.
    """
    items = [
        _result(entity_type="event_type", title="Pageviews", score=3.0),
        _result(entity_type="event", title="Spot Screen", subtitle="Pageviews", score=2.0),
        _result(entity_type="event", title="Map Screen", subtitle="Pageviews", score=1.9),
        _result(entity_type="event", title="Order Placed", subtitle="Checkout", score=2.5),
    ]

    finalized = _finalize_results(items, limit=10)

    assert [item.title for item in finalized] == [
        "Pageviews",
        "Order Placed",
        "Spot Screen",
        "Map Screen",
    ]
    # tripl-txcz: confidence is a fraction of an absolute reference score, not of
    # the top hit, so the best of a mediocre set is NOT automatically 1.0.
    assert finalized[0].confidence == pytest.approx(3.0 / _FULL_CONFIDENCE_SCORE, abs=1e-4)
    assert all(0.0 <= item.confidence <= 1.0 for item in finalized)


async def test_retrieval_window_does_not_vary_with_the_page_size(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each leg retrieves a fixed window, whatever page size was asked for.

    tripl-0tt4 item 2. ``merge_results`` SUMS the two legs, so a document is paid
    for a semantic match only if it landed inside BOTH windows. A window that
    tracked the page size therefore decided which documents got that bonus, and
    moved the TOP of the list rather than its tail — ``q='экран спота'`` answered
    with a different top-1 at ``limit=5`` than at ``limit=50``.

    The old rule (``+24`` while under 50) was not even monotonic: 49 retrieved 73
    candidates and 50 retrieved 50, so asking for MORE results made the engine
    consider FEWER. Both page sizes are in the table below for that reason.

    This asserts the invariant where it lives — the window handed to the
    retrieval leg — rather than through a ranking, because on SQLite there is no
    semantic leg to fuse and the fault could not reproduce end to end.

    The ``+ 1`` is the truncation probe (tripl-wkwv.3): a leg that came back
    exactly full is otherwise indistinguishable from one that returned
    everything there was. It is a constant, so the invariant this test exists for
    — the window does not track the page size — is untouched.
    """
    await client.post("/api/v1/projects", json={"name": "Window", "slug": "search-window"})

    windows: list[int] = []

    async def fake_sqlite_search(_session: object, **kwargs: object) -> list[SearchResult]:
        windows.append(int(kwargs["limit"]))  # type: ignore[call-overload]
        return []

    monkeypatch.setattr(search_service, "_sqlite_search", fake_sqlite_search)

    async with TestSessionLocal() as session:
        page_sizes = (1, 5, 20, 49, 50, 100)
        for page_size in page_sizes:
            await search_service.search_project(session, "search-window", "spot", limit=page_size)
        assert windows == [CANDIDATE_WINDOW + 1] * len(page_sizes)

        # An internal caller can still request a larger window: the fixed value
        # is a floor, not a ceiling.
        windows.clear()
        await search_service.search_project(session, "search-window", "spot", limit=10000)
        assert windows == [10001]


async def test_index_maintenance_runs_once_per_branch_not_once_per_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read path checks the index once per process, not once per keystroke.

    tripl-2x5d. The palette issues a request per debounce boundary, and every one
    of them probed for "does this branch have any document". On the branch that
    indexes to ZERO documents — a project whose catalog is still empty — the
    probe answers None forever, so the old read path answered it by running a
    full reindex (build, diff, delete, insert, COMMIT) from a GET, every time.

    The session below is the one that fault needs: it reports an empty index on
    every probe, so a per-request check keeps rebuilding and a memoized one does
    not.

    What is counted here is now the ENQUEUE, not an inline build: the rebuild
    moved to ``tripl.worker.tasks.search.reindex_search_branch`` (tripl-zbv0).
    The once-per-branch contract is the same either way.
    """
    queued: list[uuid.UUID] = []
    queue_succeeds = True

    async def fake_queue(_project_id: uuid.UUID, branch_id: uuid.UUID) -> bool:
        queued.append(branch_id)
        return queue_succeeds

    class _EmptyIndexSession:
        """Answers the existence probe the way a never-indexed branch does.

        The postgresql bind matters: the read path does not queue a rebuild a
        non-postgresql worker would refuse to run.
        """

        def __init__(self) -> None:
            self.probes = 0
            self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def scalar(self, *_args: object, **_kwargs: object) -> uuid.UUID | None:
            self.probes += 1
            return None

    monkeypatch.setattr(search_service, "_queue_branch_reindex", fake_queue)
    session = _EmptyIndexSession()
    project_id, branch_id, other_branch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    for _ in range(3):
        await search_service._ensure_index_exists(session, project_id, branch_id)

    assert session.probes == 1, "the existence probe is a per-process cost, not a per-search one"
    assert queued == [branch_id], "an empty branch asked for a rebuild on every search"

    # The memo is per branch: a branch this process has not searched yet is still
    # checked and still queued.
    await search_service._ensure_index_exists(session, project_id, other_branch_id)
    assert session.probes == 2
    assert queued == [branch_id, other_branch_id]

    # A broker outage must not turn the check back into a per-search cost: the
    # memo is recorded on the attempt, not on its outcome. The branch is left to
    # the triggers every other branch relies on (CRUD, post-scan, manual).
    queue_succeeds = False
    broker_down_branch = uuid.uuid4()
    for _ in range(3):
        await search_service._ensure_index_exists(session, project_id, broker_down_branch)
    assert session.probes == 3
    assert queued.count(broker_down_branch) == 1


#: How long the lexical leg below waits for the embedding call to start before
#: calling the two serialized. Generous on purpose: the assertion is about
#: ORDER, and a slow machine must not turn it into a flake.
_EMBED_START_TIMEOUT_SECONDS = 5.0
_EMBED_START_POLL_SECONDS = 0.005


async def test_the_query_embedding_is_fetched_while_the_lexical_leg_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A semantic search costs one round trip, not two in a row (tripl-2x5d).

    ``embed_query`` is a blocking provider POST handed to a thread and the
    lexical leg is SQL on the session; neither needs the other's result. Awaiting
    them in sequence made the palette's read path — measured at over 2.2s — pay
    for both end to end.

    Nothing here measures a duration. The fake lexical leg refuses to finish
    until the embedding call has started, which cannot happen at all unless the
    two are in flight together.
    """
    embed_started = threading.Event()

    async def fake_lexical(_session: object, **_kwargs: object) -> list[SearchResult]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _EMBED_START_TIMEOUT_SECONDS
        while not embed_started.is_set():
            if loop.time() > deadline:
                pytest.fail("the embedding call had not started while the lexical leg was running")
            await asyncio.sleep(_EMBED_START_POLL_SECONDS)
        return []

    def fake_embed(_query: str, *, config: AiConfig, timeout: float) -> list[float]:
        embed_started.set()
        return [0.5] * settings.search_embedding_dimensions

    async def fake_semantic(_session: object, **_kwargs: object) -> list[SearchResult]:
        return []

    enabled = replace(env_ai_config(), search_embeddings_enabled=True)

    async def fake_ai_config(_session: object) -> AiConfig:
        return enabled

    monkeypatch.setattr(_search_query, "postgres_lexical_search", fake_lexical)
    monkeypatch.setattr(_search_query, "postgres_semantic_search", fake_semantic)
    monkeypatch.setattr(_search_query, "embed_query", fake_embed)
    monkeypatch.setattr(app_settings_service, "get_ai_config", fake_ai_config)

    async with TestSessionLocal() as session:
        _, semantic_used = await _search_query.postgres_search(
            session,
            project_id=uuid.uuid4(),
            branch_id=uuid.uuid4(),
            query="session",
            entity_types=None,
            include_archived=False,
            limit=10,
        )

    assert semantic_used is True


async def test_reindex_reports_whether_the_refresh_was_really_queued(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``embeddings_scheduled`` means "handed to the broker" (tripl-0tt4 item 6).

    It used to be answered by re-reading the ``search_embeddings_enabled`` flag,
    so the response said a refresh was queued whenever the feature was switched
    on — including when the enqueue had just raised and been swallowed by the
    ``except`` in ``_queue_embedding_refresh``. An operator reads this field to
    decide whether to go and look at the queue.

    The two cases below are indistinguishable under the old code: both would
    report the config flag, which is ``False`` in this environment.
    """
    await client.post("/api/v1/projects", json={"name": "Queue", "slug": "search-queue"})

    async def _queued(*_a: object, **_k: object) -> bool:
        return True

    monkeypatch.setattr(search_service, "_queue_embedding_refresh", _queued)
    queued = await client.post("/api/v1/projects/search-queue/search/reindex")
    assert queued.status_code == 200
    assert queued.json()["embeddings_scheduled"] is True

    async def _refused(*_a: object, **_k: object) -> bool:
        return False

    monkeypatch.setattr(search_service, "_queue_embedding_refresh", _refused)
    broker_down = await client.post("/api/v1/projects/search-queue/search/reindex")
    assert broker_down.status_code == 200
    assert broker_down.json()["embeddings_scheduled"] is False


def test_finalize_assigns_confidence_without_event_type_match() -> None:
    """Confidence is an absolute property of a result, not of the result set.

    tripl-txcz: the old rule divided every score by the top score, which made the
    best hit of ANY set exactly 1.0 — measured, a keyboard-mash query was served
    at confidence 1.0 on an absolute score of 0.636. The second half of this test
    is the part that could not be true before: the same document keeps the same
    confidence whether or not a stronger result is standing next to it.
    """
    items = [
        _result(entity_type="event", title="Alpha", score=8.0),
        _result(entity_type="event", title="Beta", score=4.0),
    ]

    finalized = _finalize_results(items, limit=10)

    # 8.0 clears the full-confidence score, 4.0 is the fraction of it.
    assert finalized[0].confidence == 1.0
    assert finalized[1].confidence == pytest.approx(4.0 / _FULL_CONFIDENCE_SCORE, abs=1e-4)

    alone = _finalize_results([_result(entity_type="event", title="Beta", score=4.0)], limit=10)
    assert alone[0].confidence == finalized[1].confidence


def _semantic_result(
    *,
    title: str,
    cosine: float,
    result_id: uuid.UUID | None = None,
) -> SearchResult:
    """A row as ``postgres_semantic_search`` returns it: ``score`` IS the cosine."""
    item = _result(entity_type="event", title=title, score=cosine)
    if result_id is not None:
        item.id = result_id
    return item


def test_a_strong_semantic_only_hit_respects_identity_ceiling() -> None:
    """Semantic relevance cannot claim the hit is the named entity.

    ``merge_results`` scores a vector-only hit ``cosine * 2.5``, so its score can
    never exceed 2.5. Dividing that by ``_FULL_CONFIDENCE_SCORE`` reported a
    PERFECT cosine of 1.0 at 0.357. Its cosine contributes to confidence, then
    the identity ceiling prevents a semantic-only hit from claiming certainty.

    The RANKING weight is deliberately unchanged, and the first assertion pins
    that: this is a presentation fix, not a re-weighting.
    """
    merged = merge_results([], [_semantic_result(title="Пейволл", cosine=1.0)], 10)
    assert merged[0].score == pytest.approx(_SEMANTIC_SCORE_WEIGHT)

    finalized = _finalize_results(merged, limit=10)
    assert finalized[0].confidence == _PARTIAL_CONFIDENCE_CEILING
    assert finalized[0].confidence > _SEMANTIC_SCORE_WEIGHT / _FULL_CONFIDENCE_SCORE


def test_semantic_confidence_still_falls_with_the_cosine() -> None:
    """The fix must not turn every semantic hit into a certain one.

    A hit just above ``_SEMANTIC_MIN_COSINE`` is a weak answer and has to read as
    one; replacing the confidence rule with a constant, or with "semantic_used
    means 1.0", fails here.
    """
    weak = _finalize_results(
        merge_results([], [_semantic_result(title="Forcast", cosine=0.4)], 10),
        limit=10,
    )
    assert weak[0].confidence == pytest.approx(0.4, abs=1e-4)

    strong = _finalize_results(
        merge_results([], [_semantic_result(title="Пейволл", cosine=0.9)], 10),
        limit=10,
    )
    assert weak[0].confidence < strong[0].confidence


def test_a_hybrid_hit_keeps_the_stronger_of_its_two_certainties() -> None:
    """Neither leg may drag the other down: confidence is a ``max``, not a blend.

    A document both legs found gets the SUM for ranking, and that sum is still
    well under the certainty line here — but the vector leg is very sure about
    it, and that is what the user is told.
    """
    document_id = uuid.uuid4()
    lexical = _result(entity_type="event", title="purchase_completed", score=3.0)
    lexical.id = document_id

    merged = merge_results(
        [lexical],
        [_semantic_result(title="purchase_completed", cosine=0.8, result_id=document_id)],
        10,
    )
    assert len(merged) == 1, "the two legs found the same document; it must merge, not duplicate"
    # Ranking: the legs are summed on the score scale, exactly as before.
    assert merged[0].score == pytest.approx(3.0 + 0.8 * _SEMANTIC_SCORE_WEIGHT)

    finalized = _finalize_results(merged, limit=10)
    # 5.0 / 7.0 = 0.714 from the score, 0.8 from the cosine — the stronger wins.
    assert finalized[0].confidence == pytest.approx(0.8, abs=1e-4)


def test_a_document_the_lexical_leg_also_found_is_not_labelled_semantic() -> None:
    """Provenance is which leg PRODUCED the row, not which windows held it.

    tripl-wkwv.3. ``q='local_push_scheduled'`` on production answered with the
    event named exactly that, at confidence 1.0 — and a ``semantic`` chip, as did
    nine of its ten rows. The vector leg is a ``LIMIT``-ed kNN scan with a cosine
    floor, so it returns rows for ANY query, and the rows both legs hold are
    exactly the ones the sum lifts to the top. The flag was therefore dense at
    the head of the list and told the reader the wrong story about the one result
    they are most likely to trust.

    The other two assertions are the ones that make this a labelling change and
    nothing else: the ranking sum and tripl-txcz's hybrid ``max`` both survive.
    """
    document_id = uuid.uuid4()
    lexical = _result(entity_type="event", title="local_push_scheduled", score=12.0)
    lexical.id = document_id

    merged = merge_results(
        [lexical],
        [_semantic_result(title="local_push_scheduled", cosine=0.9, result_id=document_id)],
        10,
    )

    assert len(merged) == 1
    assert merged[0].semantic_used is False, "the keyword ladder produced this row"
    assert merged[0].score == pytest.approx(12.0 + 0.9 * _SEMANTIC_SCORE_WEIGHT)
    # Confidence and provenance answer different questions, and disagreeing here
    # is intended: the identity match is still certain (tripl-d5u8), and a hybrid
    # row still keeps the stronger of its two certainties (tripl-txcz).
    assert _finalize_results(merged, limit=10)[0].confidence == 1.0


def test_a_vector_only_hit_is_still_labelled_semantic() -> None:
    """The narrowed rule must not empty the flag out (tripl-wkwv.3).

    A row no keyword matched is exactly the row the chip exists for.
    """
    merged = merge_results([], [_semantic_result(title="Пейволл", cosine=0.9)], 10)

    assert merged[0].semantic_used is True


async def test_the_envelope_and_the_row_may_disagree_about_the_semantic_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production shape end to end: the leg RAN, this row is not its work.

    tripl-wkwv.3. Both flags are spelled ``semantic_used`` and they are two
    different claims — the envelope's is "embeddings answered this request", a
    row's is "no keyword matched this one". Both legs returning the same document
    is the ordinary case, so the two disagreeing is the ordinary response, not a
    fault. An operator diagnosing configuration reads the envelope; a reader
    asking why a row is in front of them reads the row.
    """
    document_id = uuid.uuid4()

    async def fake_lexical(_session: object, **_kwargs: object) -> list[SearchResult]:
        hit = _result(entity_type="event", title="local_push_scheduled", score=12.0)
        hit.id = document_id
        return [hit]

    async def fake_semantic(_session: object, **_kwargs: object) -> list[SearchResult]:
        return [_semantic_result(title="local_push_scheduled", cosine=0.9, result_id=document_id)]

    def fake_embed(_query: str, *, config: AiConfig, timeout: float) -> list[float]:
        return [0.5] * settings.search_embedding_dimensions

    enabled = replace(env_ai_config(), search_embeddings_enabled=True)

    async def fake_ai_config(_session: object) -> AiConfig:
        return enabled

    monkeypatch.setattr(_search_query, "postgres_lexical_search", fake_lexical)
    monkeypatch.setattr(_search_query, "postgres_semantic_search", fake_semantic)
    monkeypatch.setattr(_search_query, "embed_query", fake_embed)
    monkeypatch.setattr(app_settings_service, "get_ai_config", fake_ai_config)

    async with TestSessionLocal() as session:
        merged, semantic_used = await _search_query.postgres_search(
            session,
            project_id=uuid.uuid4(),
            branch_id=uuid.uuid4(),
            query="local_push_scheduled",
            entity_types=None,
            include_archived=False,
            limit=10,
        )

    assert semantic_used is True, "the envelope flag says only that the leg ran"
    assert len(merged) == 1
    assert merged[0].semantic_used is False, "the keyword ladder is why this row is here"


def test_sqlite_prefix_match_is_not_served_as_a_certain_answer() -> None:
    """tripl-txcz on the OTHER dialect: confidence must not depend on the engine.

    ``fallback_score`` paid a bare ``title.startswith(query)`` exactly 7.0, which
    is ``_FULL_CONFIDENCE_SCORE`` — so a one-character query was served at
    confidence 1.0 on the dialect the entire backend suite runs on. Only the two
    identity tiers may reach the certainty line, on either dialect.
    """
    document = SearchDocument(
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        entity_type="event",
        entity_id=uuid.uuid4(),
        title="screen_spot",
        subtitle="Просмотры экранов",
        body="Показ экрана спота",
        keywords="screen_spot pageviews",
        route_path="/",
        content_hash="0" * 64,
    )
    haystack = "screen_spot просмотры экранов показ экрана спота screen_spot pageviews"

    prefix = fallback_score("s", ["s"], haystack, document)
    exact = fallback_score("screen_spot", ["screen_spot"], haystack, document)
    assert prefix == _SQLITE_TITLE_PREFIX
    assert exact == _SQLITE_EXACT_TITLE

    prefix_confidence = _finalize_results(
        [_result(entity_type="event", title="screen_spot", score=prefix)], limit=10
    )[0].confidence
    exact_confidence = _finalize_results(
        [_result(entity_type="event", title="screen_spot", score=exact)], limit=10
    )[0].confidence

    assert exact_confidence == 1.0
    assert prefix_confidence < 1.0
    # A prefix is the weakest evidence in the ladder; it may not read as nearly
    # certain either, which a cosmetic 7.0 -> 6.9 nudge would have left true.
    assert prefix_confidence <= 0.8


def test_sqlite_fallback_folds_a_spaced_query_onto_an_identifier_token() -> None:
    """tripl-h9x2 is expressible without PostgreSQL, so the fallback implements it.

    The Postgres path folds ``screen spot`` into ``screen_spot`` before its
    word-boundary tiers (``token_boundary_regex``). The SQLite scorer used to do
    nothing of the kind, so the same query fell all the way to the
    "every token appears somewhere" tier. Both now go through
    :func:`identifier_form`, which is the point of extracting it.
    """
    assert identifier_form("screen spot") == "screen_spot"
    assert _token_boundary_regex("screen spot") == r"\mscreen_spot\M"

    document = SearchDocument(
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        entity_type="event",
        entity_id=uuid.uuid4(),
        title="Показ экрана спота",
        subtitle="Просмотры экранов",
        body="",
        keywords="screen_spot pageviews",
        route_path="/",
        content_hash="0" * 64,
    )
    haystack = "показ экрана спота просмотры экранов screen_spot pageviews"

    assert fallback_score("screen spot", ["screen", "spot"], haystack, document) == (
        _SQLITE_KEYWORD_TOKEN
    )
    # Without the fold the query lands here instead: both tokens appear as
    # substrings of `screen_spot`, and no stronger tier can fire.
    assert _SQLITE_ALL_TOKENS < _SQLITE_KEYWORD_TOKEN

    # And the fold does not over-fire: `\m..\M` on Postgres matches the whole
    # identifier only, so the Python side must not match a longer one either.
    longer = SearchDocument(
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        entity_type="event",
        entity_id=uuid.uuid4(),
        title="Показ экрана спота",
        subtitle="Просмотры экранов",
        body="",
        keywords="screen_spot_v2 pageviews",
        route_path="/",
        content_hash="0" * 64,
    )
    longer_haystack = "показ экрана спота просмотры экранов screen_spot_v2 pageviews"
    assert fallback_score("screen spot", ["screen", "spot"], longer_haystack, longer) == (
        _SQLITE_ALL_TOKENS
    )


def test_search_document_insert_does_not_write_generated_text_vector() -> None:
    statement = insert(SearchDocument).values(
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        entity_type="event",
        entity_id=uuid.uuid4(),
        title="Checkout Completed",
        route_path="/p/demo/monitoring/event/event-1",
        content_hash="0" * 64,
    )

    compiled = str(statement.compile(dialect=PGDialect_asyncpg()))

    columns = compiled.split(" VALUES ", maxsplit=1)[0]
    assert "text_vector" not in columns


def test_token_boundary_regex_uses_single_postgres_escapes() -> None:
    assert _token_boundary_regex("ecmwf") == r"\mecmwf\M"
    assert _token_boundary_regex("vip_segment") == r"\mvip_segment\M"


def test_token_boundary_regex_folds_a_spaced_query_into_an_identifier() -> None:
    """tripl-h9x2: a query with a space used to return ``None``.

    That silently deleted the 3.5 (keywords) and 3.0 (body) word-boundary tiers
    from every multi-word query, so ``q='screen spot'`` could not reach the event
    named ``screen_spot`` through any tier and ranked 5th behind harvested-value
    variables. Entities here are named in snake_case and typed with spaces, so
    the query is folded into the identifier form instead of being rejected for
    having one.
    """
    assert _token_boundary_regex("screen spot") == r"\mscreen_spot\M"
    assert _token_boundary_regex("  ECMWF   Model ") == r"\mecmwf_model\M"
    # Cyrillic is a token like any other; the old ASCII-only test rejected it.
    assert _token_boundary_regex("экран спота") == r"\mэкран_спота\M"
    # A query that is not identifier-shaped still falls through to LIKE/trigram.
    assert _token_boundary_regex('"exact phrase"') is None
    assert _token_boundary_regex("100%") is None
    assert _token_boundary_regex("spot:reload:bento") is None


def test_sanitize_query_removes_nul_bytes_and_nothing_else() -> None:
    # tripl-q4q7: a NUL survives str.strip() (it is not whitespace) and then
    # aborts in asyncpg, so it has to be removed explicitly.
    assert "\x00".strip() == "\x00"
    assert _sanitize_query("check\x00out") == "checkout"
    assert _sanitize_query("  завершение покупки\x00 ") == "завершение покупки"
    # An all-NUL query degrades to the pre-existing empty-query path.
    assert _sanitize_query("\x00\x00") == ""
    # Everything that carries search meaning is left exactly as it was —
    # stripping is deliberately limited to the one codepoint Postgres text
    # cannot represent.
    meaningful = 'vip_segment 100% "exact phrase"'
    assert _sanitize_query(meaningful) == meaningful


async def _create_event_type(
    client: AsyncClient,
    slug: str,
    *,
    name: str,
    display_name: str,
    description: str = "",
) -> tuple[str, str]:
    et_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": name, "display_name": display_name, "description": description},
    )
    assert et_resp.status_code == 201
    event_type_id = et_resp.json()["id"]
    field_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types/{event_type_id}/fields",
        json={
            "name": "screen",
            "display_name": "Экран",
            "field_type": "string",
            "description": "Screen or page identifier",
        },
    )
    assert field_resp.status_code == 201
    return event_type_id, field_resp.json()["id"]


@pytest.mark.asyncio
async def test_global_search_matches_multilingual_plan_content(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "Search", "slug": "search-ml"})
    event_type_id, field_id = await _create_event_type(
        client,
        "search-ml",
        name="checkout",
        display_name="Checkout / Покупка",
        description="Финальные шаги оформления заказа",
    )
    secret_field_resp = await client.post(
        f"/api/v1/projects/search-ml/event-types/{event_type_id}/fields",
        json={
            "name": "api_token",
            "display_name": "API Token",
            "field_type": "string",
            "sensitivity": "secret",
        },
    )
    assert secret_field_resp.status_code == 201
    secret_field_id = secret_field_resp.json()["id"]
    variable_create = await client.post(
        "/api/v1/projects/search-ml/variables",
        json={"name": "user_id", "description": "Идентификатор пользователя"},
    )
    secret_variable_create = await client.post(
        "/api/v1/projects/search-ml/variables",
        json={"name": "api_token", "description": "Sensitive token"},
    )
    assert variable_create.status_code == 201
    assert secret_variable_create.status_code == 201
    variable_id = uuid.UUID(variable_create.json()["id"])
    secret_variable_id = uuid.UUID(secret_variable_create.json()["id"])
    event_resp = await client.post(
        "/api/v1/projects/search-ml/events",
        json={
            "event_type_id": event_type_id,
            "name": "Checkout Completed",
            "description": "Fires when покупка успешно завершена",
            "tags": ["покупка"],
            "field_values": [
                {"field_definition_id": field_id, "value": "завершение покупки"},
                {"field_definition_id": secret_field_id, "value": "${api_token}"},
            ],
        },
    )
    assert event_resp.status_code == 201
    event_id = uuid.UUID(event_resp.json()["id"])

    async with TestSessionLocal() as session, session.begin():
        variable = await session.get(Variable, variable_id)
        secret_variable = await session.get(Variable, secret_variable_id)
        event = await session.get(Event, event_id)
        assert variable is not None
        assert secret_variable is not None
        assert event is not None
        session.add(
            VariableValue(
                project_id=variable.project_id,
                branch_id=variable.branch_id,
                variable_id=variable.id,
                event_id=event.id,
                field_definition_id=uuid.UUID(field_id),
                source_column="user_id",
                value_kind="low",
                observed_count=1,
                values=["vip_segment"],
            )
        )
        session.add(
            VariableValue(
                project_id=secret_variable.project_id,
                branch_id=secret_variable.branch_id,
                variable_id=secret_variable.id,
                event_id=event.id,
                field_definition_id=uuid.UUID(secret_field_id),
                source_column="api_token",
                value_kind="low",
                observed_count=1,
                values=["sk_live_should_not_leak"],
            )
        )
    await client.patch(
        f"/api/v1/projects/search-ml/variables/{variable_id}",
        json={"description": "Идентификатор пользователя с контекстом"},
    )

    ru_resp = await client.get("/api/v1/projects/search-ml/search?q=завершение покупки")
    assert ru_resp.status_code == 200
    ru_items = ru_resp.json()["items"]
    event_hit = next(
        (
            item
            for item in ru_items
            if item["entity_type"] == "event" and item["title"] == "Checkout Completed"
        ),
        None,
    )
    assert event_hit is not None
    # The event's own description is returned verbatim for display, and every
    # result carries an absolute confidence in [0, 1] (tripl-txcz — it is no
    # longer normalized to the top hit of the response).
    assert event_hit["event_id"] == str(event_id)
    assert event_hit["name"] == "Checkout Completed"
    assert event_hit["implemented"] is False
    variable_contexts = event_hit["variable_values"]
    assert len(variable_contexts) == 1
    assert variable_contexts[0]["variable_id"] == str(variable_id)
    assert variable_contexts[0]["variable_name"] == "user_id"
    assert variable_contexts[0]["field_definition_id"] == field_id
    assert variable_contexts[0]["field_name"] == "screen"
    assert variable_contexts[0]["field_display_name"] == "Экран"
    assert variable_contexts[0]["source_column"] == "user_id"
    assert variable_contexts[0]["value_kind"] == "low"
    assert variable_contexts[0]["observed_count"] == 1
    assert variable_contexts[0]["values"] == ["vip_segment"]
    assert event_hit["description"] == "Fires when покупка успешно завершена"
    # tripl-txcz: the top hit is no longer 1.0 by construction — this query
    # matches a field value rather than a title, so it is served as a strong but
    # not certain answer.
    #
    # Pinned to the TIER and to the tier ORDER, not to a range (tripl-u7wf).
    # `0.0 <= confidence <= 1.0` and `0.0 < confidence <= 1.0` are the entire
    # domain of the field: every confidence this endpoint can return satisfies
    # them — including the 1.0-for-a-nonsense-query that tripl-txcz was filed
    # about — so the assertions this replaces would have passed against the very
    # bug they were added to prevent.
    #
    # Read the three lines below for what each one can actually catch, because
    # the first two deliberately CANNOT catch a re-tuning and that is correct:
    #
    #   - `== _SQLITE_KEYWORD_TOKEN` says WHICH TIER served this hit. Comparing
    #     against the constant rather than a literal means retuning the tier
    #     leaves it green — which is the point, since the claim is about routing
    #     ("a field-value match, not a title match"), not about the number. It
    #     fails when a scoring change moves this query to a different tier.
    #   - the confidence line says confidence is that tier's share of a full
    #     score, i.e. that the ratio is the reported number.
    #   - `< _SQLITE_EXACT_TITLE` is the one that compares two INDEPENDENT
    #     constants, so it is the one a bad re-tuning trips: it fails the moment
    #     a field-value match can score at or above an exact title match, which
    #     is the inversion tripl-txcz's "strong but not certain" wording rests
    #     on.
    assert ru_items[0] == event_hit
    assert ru_items[0]["score"] == pytest.approx(_SQLITE_KEYWORD_TOKEN)
    assert ru_items[0]["confidence"] == pytest.approx(
        _SQLITE_KEYWORD_TOKEN / _FULL_CONFIDENCE_SCORE, abs=1e-4
    )
    assert ru_items[0]["score"] < _SQLITE_EXACT_TITLE

    en_resp = await client.get("/api/v1/projects/search-ml/search?q=checkout")
    assert en_resp.status_code == 200
    assert {item["entity_type"] for item in en_resp.json()["items"]} >= {"event", "event_type"}

    var_resp = await client.get("/api/v1/projects/search-ml/search?q=Идентификатор&types=variable")
    assert var_resp.status_code == 200
    assert [item["entity_type"] for item in var_resp.json()["items"]] == ["variable"]

    value_resp = await client.get("/api/v1/projects/search-ml/search?q=vip_segment&types=variable")
    assert value_resp.status_code == 200
    assert [item["title"] for item in value_resp.json()["items"]] == ["${user_id}"]

    # Querying a concrete observed property/context value should also surface
    # the owning event (not only the variable document).
    event_value_resp = await client.get("/api/v1/projects/search-ml/search?q=vip_segment")
    assert event_value_resp.status_code == 200
    event_value_items = event_value_resp.json()["items"]
    assert any(
        item["entity_type"] == "event" and item["title"] == "Checkout Completed"
        for item in event_value_items
    )


@pytest.mark.asyncio
async def test_event_list_search_is_plain_column_ilike(client: AsyncClient) -> None:
    """The list ``search`` is a plain substring filter over the event's own text
    columns (name/description/source_name) — NOT the semantic/hybrid search,
    which lives only in the global command palette. Field-value content is
    reachable via the dedicated ``field_value`` column filter instead."""
    await client.post("/api/v1/projects", json={"name": "Event Search", "slug": "search-events"})
    event_type_id, field_id = await _create_event_type(
        client,
        "search-events",
        name="page",
        display_name="Page",
    )
    await client.post(
        "/api/v1/projects/search-events/events",
        json={
            "event_type_id": event_type_id,
            "name": "Generic Event",
            "field_values": [{"field_definition_id": field_id, "value": "home_screen"}],
        },
    )
    await client.post(
        "/api/v1/projects/search-events/events",
        json={
            "event_type_id": event_type_id,
            "name": "Other Event",
            "field_values": [{"field_definition_id": field_id, "value": "settings_screen"}],
        },
    )

    # Free-text search matches the event name column...
    by_name = await client.get("/api/v1/projects/search-events/events?search=Generic")
    assert by_name.status_code == 200
    assert by_name.json()["total"] == 1
    assert by_name.json()["items"][0]["name"] == "Generic Event"

    # ...but NOT a field value (that is not one of the event's text columns).
    by_value = await client.get("/api/v1/projects/search-events/events?search=home_screen")
    assert by_value.status_code == 200
    assert by_value.json()["total"] == 0

    # Field-value content is filtered through the dedicated field_value param.
    by_field = await client.get("/api/v1/projects/search-events/events?field_value=home_screen")
    assert by_field.status_code == 200
    assert by_field.json()["total"] == 1
    assert by_field.json()["items"][0]["name"] == "Generic Event"


@pytest.mark.asyncio
async def test_search_filters_archived_and_excludes_sensitive_values(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "Safety", "slug": "search-safe"})
    event_type_id, field_id = await _create_event_type(
        client,
        "search-safe",
        name="security",
        display_name="Security",
    )
    secret_resp = await client.post(
        f"/api/v1/projects/search-safe/event-types/{event_type_id}/fields",
        json={
            "name": "api_secret",
            "display_name": "API Secret",
            "field_type": "string",
            "sensitivity": "secret",
        },
    )
    assert secret_resp.status_code == 201
    secret_field_id = secret_resp.json()["id"]
    await client.post(
        "/api/v1/projects/search-safe/events",
        json={
            "event_type_id": event_type_id,
            "name": "Archived Secret",
            "status": "archived",
            "field_values": [
                {"field_definition_id": field_id, "value": "archived_marker"},
                {"field_definition_id": secret_field_id, "value": "sk_live_should_not_index"},
            ],
        },
    )

    hidden_resp = await client.get("/api/v1/projects/search-safe/search?q=archived_marker")
    assert hidden_resp.status_code == 200
    assert hidden_resp.json()["items"] == []

    archived_resp = await client.get(
        "/api/v1/projects/search-safe/search?q=archived_marker&include_archived=true"
    )
    assert archived_resp.status_code == 200
    assert [item["title"] for item in archived_resp.json()["items"]] == ["Archived Secret"]

    secret_resp = await client.get(
        "/api/v1/projects/search-safe/search?q=sk_live_should_not_index&include_archived=true"
    )
    assert secret_resp.status_code == 200
    assert secret_resp.json()["items"] == []


@pytest.mark.asyncio
async def test_event_keywords_carry_only_curated_text_while_body_keeps_the_harvest(
    client: AsyncClient,
) -> None:
    """tripl-0qld: an EVENT's ``keywords`` is identity text, its ``body`` is evidence.

    WHAT THIS PINS AND WHY IT IS ASSERTED ON THE DOCUMENT, NOT ON A RANKING
    -----------------------------------------------------------------------
    Two tiers of ``_search_query``'s boost ladder read ``d.keywords`` — the 3.5
    literal keyword-token tier and the 3.25 stemmed-identity tier — and the
    second one's docstring states the premise the first one also depends on. For
    EVENT documents the premise was false: ``_event_document`` joined
    ``safe_values`` (including every value a SCAN wrote) and the values-carrying
    ``_variable_context_text`` into ``keywords``, so a value harvested from
    production traffic bought unrelated events a tier above the one a correctly
    named entity can earn.

    A ranking assertion cannot pin this on its own — the relevance harness's
    ``purchase-plural`` case stayed green through the whole inversion because the
    trigram leg covered the 0.25 the ladder gave away. So the claim is asserted
    where it is exact: on the built document's two columns. The ladder-level
    consequence is asserted separately, against a real PostgreSQL, by
    ``tests/relevance/test_keyword_tier_premise.py``.

    FOUR MARKERS, ONE PER RULE
    --------------------------
    * ``authored_marker`` — a value a person typed into the spec
      (``event_service`` records ``is_authored=True``). Curated: stays in
      ``keywords``.
    * ``scanned_marker`` — the same shape of value written by a scan
      (``is_authored=False``). Body only.
    * ``harvest_marker`` — a ``VariableValue`` observed on a bound field. Body
      only, and its BINDING (``properties.probe``) stays in ``keywords``, which
      is what separates "values left" from "the context text was deleted".
    * ``secret_marker`` — authored AND on a ``sensitivity='secret'`` field. In
      neither column: the sensitivity guard is the outer condition and the
      ``is_authored`` test is nested inside it, so curation can never re-admit a
      value the guard excluded.
    """
    await client.post("/api/v1/projects", json={"name": "Curated", "slug": "search-curated"})
    event_type_id, authored_field_id = await _create_event_type(
        client,
        "search-curated",
        name="probe",
        display_name="Probe",
    )
    scanned_field_resp = await client.post(
        f"/api/v1/projects/search-curated/event-types/{event_type_id}/fields",
        json={"name": "scanned_field", "display_name": "Scanned", "field_type": "string"},
    )
    assert scanned_field_resp.status_code == 201, scanned_field_resp.text
    scanned_field_id = scanned_field_resp.json()["id"]
    secret_field_resp = await client.post(
        f"/api/v1/projects/search-curated/event-types/{event_type_id}/fields",
        json={
            "name": "secret_field",
            "display_name": "Secret",
            "field_type": "string",
            "sensitivity": "secret",
        },
    )
    assert secret_field_resp.status_code == 201, secret_field_resp.text
    secret_field_id = secret_field_resp.json()["id"]

    # Posted through the spec API, so event_service stamps is_authored=True on
    # both of these — that is what makes the sensitivity assertion below a test
    # of the NESTING rather than of the authored flag by itself.
    event_resp = await client.post(
        "/api/v1/projects/search-curated/events",
        json={
            "event_type_id": event_type_id,
            "name": "probe_fired",
            "field_values": [
                {"field_definition_id": authored_field_id, "value": "authored_marker"},
                {"field_definition_id": secret_field_id, "value": "secret_marker"},
            ],
        },
    )
    assert event_resp.status_code == 201, event_resp.text
    event_id = uuid.UUID(event_resp.json()["id"])

    variable_resp = await client.post(
        "/api/v1/projects/search-curated/variables",
        json={"name": "probe_property", "description": "Auto-detected from traffic"},
    )
    assert variable_resp.status_code == 201, variable_resp.text
    variable_id = uuid.UUID(variable_resp.json()["id"])

    # The two rows no endpoint writes: a scan-authored field value and a
    # harvested variable context. Both are ordinary production shapes — the
    # scanner and the spec are different pipelines — and neither has an API.
    async with TestSessionLocal() as session, session.begin():
        event = await session.get(Event, event_id)
        assert event is not None
        project_id = event.project_id
        branch_id = event.branch_id
        session.add(
            EventFieldValue(
                event_id=event.id,
                field_definition_id=uuid.UUID(scanned_field_id),
                value="scanned_marker",
                is_authored=False,
            )
        )
        session.add(
            VariableValue(
                project_id=project_id,
                branch_id=branch_id,
                variable_id=variable_id,
                event_id=event.id,
                field_definition_id=uuid.UUID(authored_field_id),
                source_column="properties.probe",
                value_kind="high",
                observed_count=4402,
                values=["harvest_marker"],
            )
        )

    async with TestSessionLocal() as session:
        documents = await build_documents(session, project_id, branch_id, "search-curated")
    document = next(
        doc for doc in documents if doc.entity_type == "event" and doc.title == "probe_fired"
    )

    assert "authored_marker" in document.keywords, (
        "a hand-typed spec value is curated text and belongs in keywords; dropping "
        "every value instead demotes it from the 3.5 keyword-token tier to 3.0"
    )
    assert "authored_marker" in document.body

    assert "scanned_marker" not in document.keywords, (
        "a scan-written field value is not curated text and must not buy the event "
        "the 3.5 keyword-token tier (tripl-0qld)"
    )
    assert "scanned_marker" in document.body, (
        "harvested values stay searchable — they are evidence about the event, and "
        "the 3.0 body-token tier is where they are paid"
    )

    assert "harvest_marker" not in document.keywords, (
        "a VariableValue observed on a bound field is the exact text tripl-gbxj "
        "removed from a VARIABLE's keywords; an EVENT's keywords is the same column"
    )
    assert "harvest_marker" in document.body
    assert "properties.probe" in document.keywords, (
        "the BINDING is still a keyword of the event — only the observed values "
        "left, and a fix that deleted the whole context text would pass the "
        "assertions above while losing real signal"
    )

    assert "secret_marker" not in document.keywords, (
        "sensitivity is the OUTER guard: an authored value on a secret field must "
        "not reach keywords, or the is_authored branch has been hoisted out of it"
    )
    assert "secret_marker" not in document.body


@pytest.mark.asyncio
async def test_search_query_with_nul_byte_behaves_like_the_clean_query(
    client: AsyncClient,
) -> None:
    """tripl-q4q7: ``?q=%00`` used to 500 (asyncpg CharacterNotInRepertoireError).

    Asserted through the HTTP status and body rather than the driver
    exception, so the test still fails for the right reason on SQLite, where
    the NUL never reached a driver: without the sanitiser the scorer sees a
    different query string, and the two response bodies below diverge on
    ``score``, ``highlights`` and ``snippet``.
    """
    await client.post("/api/v1/projects", json={"name": "Nul", "slug": "search-nul"})
    await _create_event_type(
        client,
        "search-nul",
        name="checkout",
        display_name="Checkout",
        description="Fires on the checkout screen",
    )

    clean = await client.get("/api/v1/projects/search-nul/search", params={"q": "checkout"})
    assert clean.status_code == 200
    assert [item["title"] for item in clean.json()["items"]] != []

    laced = await client.get("/api/v1/projects/search-nul/search", params={"q": "check\x00out"})
    assert laced.status_code == 200
    assert laced.json() == clean.json()

    # A query made only of NULs collapses to empty and takes the same
    # 200-with-no-items path a whitespace-only query already took — not a 500,
    # and deliberately not a 422 either (see ``sanitize_query``).
    only_nuls = await client.get("/api/v1/projects/search-nul/search", params={"q": "\x00"})
    assert only_nuls.status_code == 200
    assert only_nuls.json() == {
        "items": [],
        "total": 0,
        "truncated": False,
        "semantic_used": False,
    }


async def test_search_reports_truncation_rather_than_a_full_page(client: AsyncClient) -> None:
    """``total`` cannot say whether hits were dropped, so something else must.

    tripl-wkwv.3. ``total`` is ``len(items)`` computed AFTER the trim, so on a
    full page it equals ``limit`` whether the engine had six more answers or
    none — and this route takes no ``offset``, so an agent reading it as a
    catalog count (the way ``/events`` total genuinely is one) cannot tell a
    complete result set from a clipped one. ``total`` keeps its value and its
    name; ``truncated`` carries the fact it never could.
    """
    await client.post("/api/v1/projects", json={"name": "Trunc", "slug": "search-trunc"})
    event_type_id, _field_id = await _create_event_type(
        client,
        "search-trunc",
        name="checkout",
        display_name="Checkout",
    )
    for step in range(5):
        created = await client.post(
            "/api/v1/projects/search-trunc/events",
            json={"event_type_id": event_type_id, "name": f"checkout_step_{step}"},
        )
        assert created.status_code == 201

    clipped_resp = await client.get(
        "/api/v1/projects/search-trunc/search", params={"q": "checkout", "limit": 2}
    )
    assert clipped_resp.status_code == 200
    clipped = clipped_resp.json()
    assert len(clipped["items"]) == 2
    # The old signal, still exactly as ambiguous as it was — and the new one.
    assert clipped["total"] == 2
    assert clipped["truncated"] is True

    # Same query, room for every hit: nothing was dropped and the route says so,
    # which is what makes the flag worth reading at all.
    whole_resp = await client.get(
        "/api/v1/projects/search-trunc/search", params={"q": "checkout", "limit": 20}
    )
    assert whole_resp.status_code == 200
    whole = whole_resp.json()
    assert whole["total"] == len(whole["items"]) > 2
    assert whole["truncated"] is False


async def test_a_full_retrieval_window_is_not_by_itself_a_dropped_hit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window filling and hits being dropped are two different facts.

    tripl-wkwv.3, found reviewing it. ``truncated`` also fired on
    ``candidate_count >= candidate_limit`` — "the retrieval window filled" —
    and every consumer had already been told the flag means "ranked hits exist
    that this response does not carry". Those coincide except on the boundary
    the HTTP ceiling puts a caller on: at ``limit=100``, the maximum
    ``GET /search`` accepts, ``candidate_limit == capped_limit``, so a query
    matching exactly 100 documents filled the window while the body carried
    every hit either leg can produce. The route called that answer truncated,
    and the documented remedy — raise ``limit`` — was already exhausted.

    Reproduced at a window of ``n`` rather than 100 so the corpus can be five
    events instead of a hundred; ``capped_limit == candidate_limit == n`` is the
    same arithmetic the real ceiling produces. The route now retrieves one row
    PAST the window, so a full window and a complete answer are distinguishable.
    """
    await client.post("/api/v1/projects", json={"name": "Edge", "slug": "search-trunc-edge"})
    event_type_id, _field_id = await _create_event_type(
        client,
        "search-trunc-edge",
        name="checkout",
        display_name="Checkout",
    )
    for step in range(5):
        created = await client.post(
            "/api/v1/projects/search-trunc-edge/events",
            json={"event_type_id": event_type_id, "name": f"checkout_step_{step}"},
        )
        assert created.status_code == 201

    # How many documents this query actually matches, read rather than assumed:
    # the corpus is events plus the event type plus its field.
    whole = (
        await client.get(
            "/api/v1/projects/search-trunc-edge/search",
            params={"q": "checkout", "limit": 100},
        )
    ).json()
    matched = len(whole["items"])
    assert matched > 1
    # ...and that this reading is itself complete, or `matched` would be a page
    # size rather than the size of the eligible set.
    assert whole["truncated"] is False

    # capped_limit == candidate_limit == matched: the page is full, the window is
    # full, and every hit is in the body.
    monkeypatch.setattr(search_service, "CANDIDATE_WINDOW", matched)
    exact = (
        await client.get(
            "/api/v1/projects/search-trunc-edge/search",
            params={"q": "checkout", "limit": matched},
        )
    ).json()
    assert len(exact["items"]) == matched
    assert exact["truncated"] is False, "every matching document is in this response"

    # One row short of the eligible set, same saturated window: now a hit really
    # was dropped, and the flag must still say so.
    monkeypatch.setattr(search_service, "CANDIDATE_WINDOW", matched - 1)
    clipped = (
        await client.get(
            "/api/v1/projects/search-trunc-edge/search",
            params={"q": "checkout", "limit": matched - 1},
        )
    ).json()
    assert len(clipped["items"]) == matched - 1
    assert clipped["truncated"] is True


async def _create_fact_table(
    client: AsyncClient,
    slug: str,
    *,
    name: str = "orders_ft",
    display_name: str = "Orders Fact",
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{slug}/fact-tables",
        json={
            "name": name,
            "display_name": display_name,
            "description": "Orders rowset",
            "sql": "SELECT created_at, amount, user_id FROM orders",
            "timestamp_column": "created_at",
            "columns": [
                {"name": "created_at", "type": "timestamp"},
                {"name": "amount", "type": "number"},
                {"name": "user_id", "type": "string"},
            ],
            "identifier_columns": ["user_id"],
            "row_filters": [{"name": "exclude_test", "sql": "is_test = 0"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_fact_metric(
    client: AsyncClient,
    slug: str,
    fact_table_id: str,
    *,
    name: str = "revenue_total",
    display_name: str = "Revenue Total",
    **extra: object,
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{slug}/metrics",
        json={
            "kind": "fact",
            "name": name,
            "display_name": display_name,
            "composition": "single",
            "fact_table_id": fact_table_id,
            "aggregation": "sum",
            "interval": "1h",
            "measure_column": "amount",
            **extra,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_metric_and_fact_table_creation_indexes_them_for_search(
    client: AsyncClient,
) -> None:
    await client.post("/api/v1/projects", json={"name": "Catalog", "slug": "search-catalog"})
    await _create_event_type(client, "search-catalog", name="pv", display_name="Page View")
    # Seed the index BEFORE the catalog entities exist, so the hits below can
    # only come from the CRUD-triggered reindex. This used to be a GET, which
    # worked only because the read path built an empty branch's index for it —
    # the side effect tripl-zbv0 removed. Reindexing explicitly states the setup
    # the assertions actually need instead of leaning on a read path's side
    # effect; nothing below is weakened.
    await search_service.reindex_branch(
        TestSessionLocal(), "search-catalog", schedule_embeddings=False
    )

    fact_table = await _create_fact_table(client, "search-catalog")
    metric = await _create_fact_metric(
        client, "search-catalog", fact_table["id"], description="Sum of order amounts"
    )

    metric_resp = await client.get(
        "/api/v1/projects/search-catalog/search?q=Revenue Total&types=metric"
    )
    assert metric_resp.status_code == 200
    metric_items = metric_resp.json()["items"]
    assert [item["entity_type"] for item in metric_items] == ["metric"]
    assert metric_items[0]["title"] == "Revenue Total"
    assert metric_items[0]["subtitle"] == "revenue_total"
    assert metric_items[0]["description"] == "Sum of order amounts"
    assert (
        metric_items[0]["route_path"]
        == f"/o/default/p/search-catalog/monitoring/metric/{metric['id']}"
    )

    # The metric is also reachable through its internal name (keywords).
    by_name = await client.get("/api/v1/projects/search-catalog/search?q=revenue_total")
    assert any(item["entity_type"] == "metric" for item in by_name.json()["items"])

    ft_resp = await client.get(
        "/api/v1/projects/search-catalog/search?q=orders_ft&types=fact_table"
    )
    assert ft_resp.status_code == 200
    ft_items = ft_resp.json()["items"]
    assert [item["entity_type"] for item in ft_items] == ["fact_table"]
    assert ft_items[0]["title"] == "Orders Fact"
    assert (
        ft_items[0]["route_path"]
        == f"/o/default/p/search-catalog/metrics/fact-tables/{fact_table['id']}/edit"
    )


@pytest.mark.asyncio
async def test_metric_update_archive_and_delete_are_reflected_in_search(
    client: AsyncClient,
) -> None:
    await client.post("/api/v1/projects", json={"name": "Catalog2", "slug": "search-catalog2"})
    await _create_event_type(client, "search-catalog2", name="pv", display_name="Page View")
    fact_table = await _create_fact_table(client, "search-catalog2")
    metric = await _create_fact_metric(client, "search-catalog2", fact_table["id"])

    renamed = await client.patch(
        f"/api/v1/projects/search-catalog2/metrics/{metric['id']}",
        json={"display_name": "Net Revenue"},
    )
    assert renamed.status_code == 200

    new_title = await client.get(
        "/api/v1/projects/search-catalog2/search?q=Net Revenue&types=metric"
    )
    assert [item["title"] for item in new_title.json()["items"]] == ["Net Revenue"]

    archived = await client.patch(
        f"/api/v1/projects/search-catalog2/metrics/{metric['id']}",
        json={"status": "archived"},
    )
    assert archived.status_code == 200

    hidden = await client.get("/api/v1/projects/search-catalog2/search?q=Net Revenue&types=metric")
    assert hidden.json()["items"] == []

    included = await client.get(
        "/api/v1/projects/search-catalog2/search?q=Net Revenue&types=metric&include_archived=true"
    )
    assert [item["title"] for item in included.json()["items"]] == ["Net Revenue"]

    deleted = await client.delete(f"/api/v1/projects/search-catalog2/metrics/{metric['id']}")
    assert deleted.status_code == 204

    gone = await client.get(
        "/api/v1/projects/search-catalog2/search?q=Net Revenue&types=metric&include_archived=true"
    )
    assert gone.json()["items"] == []


def test_finalize_confidence_prefers_exact_token_value_matches() -> None:
    # Emulates the "ecmwf exists in property values" case:
    # the event that contains an exact token value should rank above
    # near/fuzzy matches when scores reflect exact-token boost.
    items = [
        _result(entity_type="event", title="spot:choose:models", score=6.5),
        _result(entity_type="event", title="map_model_selected_ecmwf", score=5.9),
        _result(entity_type="event", title="ecmwf_model_popup_shown", score=5.6),
    ]

    finalized = _finalize_results(items, limit=10)

    # RANKING is what this test is about, and it is untouched by tripl-d5u8:
    # the cap is applied after the sort, to the badge and never to the position.
    assert [item.title for item in finalized] == [
        "spot:choose:models",
        "map_model_selected_ecmwf",
        "ecmwf_model_popup_shown",
    ]

    # CONFIDENCE COMPRESSES AT THE TOP FOR PARTIAL MATCHES, DELIBERATELY
    # (tripl-d5u8). None of these three is an identity match — the strongest is
    # 6.5, below the 7.2 `keywords == query` tier — so all three clip to the
    # partial ceiling and report the SAME number despite ranking differently
    # (6.5/7.0 = 0.93, 5.9/7.0 = 0.84 and 5.6/7.0 = 0.80 all become 0.80).
    #
    # That is the intended trade and the reason clipping was chosen over scaling
    # the whole partial range into [0, ceiling]: the badge answers "is this what
    # you meant", and above this line the honest answer is "strong, but not
    # certain" for all of them. Scaling would have preserved the ordering in the
    # badge at the cost of deflating every partial match users already see.
    # The ORDER still separates them; the certainty does not claim to.
    assert {item.confidence for item in finalized} == {_PARTIAL_CONFIDENCE_CEILING}
    assert all(item.confidence < 1.0 for item in finalized)


@pytest.mark.asyncio
async def test_scan_configs_and_alert_rules_are_searchable(client: AsyncClient) -> None:
    """Project-scoped configuration is indexed like metrics already were (tripl-dfct).

    Neither entity carries a branch_id, so this follows the decision the codebase
    had already taken for MetricDefinition and FactTable: fold the project's rows
    into every branch's index rather than inventing a branch-independent scope.

    The alert rule is reached through its DESTINATION — AlertRule has no
    project_id of its own — so a rule belonging to another project must not leak
    in. That is what the second project below is for.
    """
    from tripl.models.alert_destination import AlertDestination
    from tripl.models.alert_rule import AlertRule
    from tripl.models.data_source import DataSource
    from tripl.models.project import Project
    from tripl.models.scan_config import ScanConfig

    await client.post("/api/v1/projects", json={"name": "Recall", "slug": "recall"})
    await client.post("/api/v1/projects", json={"name": "Other", "slug": "recall-other"})

    async with TestSessionLocal() as session, session.begin():
        project_id = await session.scalar(select(Project.id).where(Project.slug == "recall"))
        other_id = await session.scalar(select(Project.id).where(Project.slug == "recall-other"))
        assert project_id is not None and other_id is not None
        source = DataSource(
            name="Warehouse for recall",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="u",
            password_encrypted="",
        )
        session.add(source)
        await session.flush()
        session.add(
            ScanConfig(
                project_id=project_id,
                data_source_id=source.id,
                name="Nightly checkout scan",
                base_query="SELECT * FROM warehouse.checkout_events",
                time_column="occurred_at",
                interval="1h",
            )
        )
        destinations = [
            AlertDestination(
                project_id=pid,
                type="slack",
                name=f"Slack {pid}",
                enabled=True,
                webhook_url_encrypted="secret",
            )
            for pid in (project_id, other_id)
        ]
        session.add_all(destinations)
        await session.flush()
        session.add_all(
            [
                AlertRule(
                    destination_id=destinations[0].id,
                    name="Checkout collapse watch",
                    message_template="Investigate the funnel immediately",
                ),
                AlertRule(
                    destination_id=destinations[1].id,
                    name="Checkout rule of another project",
                ),
            ]
        )

    await search_service.reindex_branch(TestSessionLocal(), "recall", schedule_embeddings=False)

    found = await client.get("/api/v1/projects/recall/search?q=checkout&limit=50")
    assert found.status_code == 200
    hits = {(item["entity_type"], item["title"]): item for item in found.json()["items"]}

    assert ("scan_config", "Nightly checkout scan") in hits
    assert ("alert_rule", "Checkout collapse watch") in hits
    # The other project's rule reaches its project only through its destination;
    # a missing join would drag it in here.
    assert ("alert_rule", "Checkout rule of another project") not in hits

    assert hits[("scan_config", "Nightly checkout scan")]["route_path"].startswith(
        "/o/default/p/recall/scans/"
    )
    assert (
        hits[("alert_rule", "Checkout collapse watch")]["route_path"]
        == "/o/default/p/recall/alerting"
    )

    # The rule's template is human-written text, so it is searchable on its own.
    by_template = await client.get("/api/v1/projects/recall/search?q=funnel&limit=50")
    assert by_template.status_code == 200
    assert any(
        item["entity_type"] == "alert_rule" and item["title"] == "Checkout collapse watch"
        for item in by_template.json()["items"]
    )


async def _search_titles(client: AsyncClient, slug: str, query: str, entity_type: str) -> list[str]:
    resp = await client.get(f"/api/v1/projects/{slug}/search?q={query}&limit=50")
    assert resp.status_code == 200, resp.text
    return [item["title"] for item in resp.json()["items"] if item["entity_type"] == entity_type]


async def _search_hits(
    client: AsyncClient, slug: str, query: str, entity_type: str
) -> list[dict[str, Any]]:
    """Like :func:`_search_titles`, but keeps the whole hit.

    Some assertions are about a field other than the title — an alert rule's
    subtitle is the name of the scan it is narrowed to, and that is the field
    that goes stale when the scan disappears (tripl-9jvz).
    """
    resp = await client.get(f"/api/v1/projects/{slug}/search?q={query}&limit=50")
    assert resp.status_code == 200, resp.text
    items: list[dict[str, Any]] = resp.json()["items"]
    return [item for item in items if item["entity_type"] == entity_type]


@pytest.mark.asyncio
async def test_a_scan_config_is_searchable_the_moment_its_service_saves_it(
    client: AsyncClient,
) -> None:
    """No hand reindex anywhere below — the mutation is the trigger (tripl-ugrm).

    The recall test above has to call ``reindex_branch`` itself, and that hand
    reindex was the only thing making it pass: ``scan_config`` was one of two
    document kinds whose service never refreshed the index, so a scan the user
    had just saved stayed unfindable in the palette until an unrelated trigger
    fired. Everything here goes through the HTTP surface a user goes through.
    """
    await client.post("/api/v1/projects", json={"name": "Fresh", "slug": "fresh-scan"})
    source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Fresh warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "default",
        },
    )
    assert source.status_code == 201, source.text

    created = await client.post(
        "/api/v1/projects/fresh-scan/scans",
        json={
            "data_source_id": source.json()["id"],
            "name": "Zebrascan nightly",
            "base_query": "SELECT * FROM warehouse.checkout_events",
        },
    )
    assert created.status_code == 201, created.text
    scan_id = created.json()["id"]

    assert await _search_titles(client, "fresh-scan", "zebrascan", "scan_config") == [
        "Zebrascan nightly"
    ]

    renamed = await client.patch(
        f"/api/v1/projects/fresh-scan/scans/{scan_id}",
        json={"name": "Quaggascan nightly"},
    )
    assert renamed.status_code == 200, renamed.text
    assert await _search_titles(client, "fresh-scan", "quaggascan", "scan_config") == [
        "Quaggascan nightly"
    ]
    assert await _search_titles(client, "fresh-scan", "zebrascan", "scan_config") == []

    deleted = await client.delete(f"/api/v1/projects/fresh-scan/scans/{scan_id}")
    assert deleted.status_code == 204, deleted.text
    assert await _search_titles(client, "fresh-scan", "quaggascan", "scan_config") == []


@pytest.mark.asyncio
async def test_an_alert_rule_is_searchable_the_moment_its_service_saves_it(
    client: AsyncClient,
) -> None:
    """The other half of tripl-ugrm: alert rules had the same missing trigger.

    A rule is reached through its destination rather than a branch, so the
    refresh has to happen in the destination/rule CRUD module — the branch-scoped
    services never touch it.
    """
    await client.post("/api/v1/projects", json={"name": "Fresh", "slug": "fresh-rule"})
    destination = await client.post(
        "/api/v1/projects/fresh-rule/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination.status_code == 201, destination.text
    destination_id = destination.json()["id"]

    created = await client.post(
        f"/api/v1/projects/fresh-rule/alert-destinations/{destination_id}/rules",
        json={"name": "Zebrarule collapse watch"},
    )
    assert created.status_code == 201, created.text
    rule_id = created.json()["id"]

    assert await _search_titles(client, "fresh-rule", "zebrarule", "alert_rule") == [
        "Zebrarule collapse watch"
    ]

    renamed = await client.patch(
        f"/api/v1/projects/fresh-rule/alert-destinations/{destination_id}/rules/{rule_id}",
        json={"name": "Quaggarule collapse watch"},
    )
    assert renamed.status_code == 200, renamed.text
    assert await _search_titles(client, "fresh-rule", "quaggarule", "alert_rule") == [
        "Quaggarule collapse watch"
    ]

    deleted = await client.delete(
        f"/api/v1/projects/fresh-rule/alert-destinations/{destination_id}/rules/{rule_id}"
    )
    assert deleted.status_code == 204, deleted.text
    assert await _search_titles(client, "fresh-rule", "quaggarule", "alert_rule") == []


@pytest.mark.asyncio
async def test_deleting_a_destination_takes_its_rules_out_of_the_index(
    client: AsyncClient,
) -> None:
    """``AlertDestination.rules`` cascades, so the delete removes documents too.

    The rule CRUD paths are the obvious half of tripl-ugrm; this is the half that
    deletes rules without ever calling ``delete_rule``.
    """
    await client.post("/api/v1/projects", json={"name": "Fresh", "slug": "fresh-dest"})
    destination = await client.post(
        "/api/v1/projects/fresh-dest/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination.status_code == 201, destination.text
    destination_id = destination.json()["id"]

    created = await client.post(
        f"/api/v1/projects/fresh-dest/alert-destinations/{destination_id}/rules",
        json={"name": "Okapirule collapse watch"},
    )
    assert created.status_code == 201, created.text
    assert await _search_titles(client, "fresh-dest", "okapirule", "alert_rule") == [
        "Okapirule collapse watch"
    ]

    deleted = await client.delete(
        f"/api/v1/projects/fresh-dest/alert-destinations/{destination_id}"
    )
    assert deleted.status_code == 204, deleted.text
    assert await _search_titles(client, "fresh-dest", "okapirule", "alert_rule") == []


@pytest.mark.asyncio
async def test_deleting_a_data_source_takes_its_scans_out_of_the_index(
    client: AsyncClient,
) -> None:
    """``DataSource.scan_configs`` cascades, so the delete removes documents too.

    Exactly the shape of the destination cascade above, one module over: the scan
    CRUD paths refresh the index themselves, but a source delete removes scan
    configs without ever calling ``delete_scan_config``, so their documents
    outlived them and a deleted scan stayed findable in the command palette
    (tripl-9jvz). No hand reindex anywhere below — the delete is the trigger.
    """
    await client.post("/api/v1/projects", json={"name": "Fresh", "slug": "fresh-source"})
    source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Doomed warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "default",
        },
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]

    created = await client.post(
        "/api/v1/projects/fresh-source/scans",
        json={
            "data_source_id": source_id,
            "name": "Narwhalscan nightly",
            "base_query": "SELECT * FROM warehouse.checkout_events",
        },
    )
    assert created.status_code == 201, created.text
    assert await _search_titles(client, "fresh-source", "narwhalscan", "scan_config") == [
        "Narwhalscan nightly"
    ]

    deleted = await client.delete(f"/api/v1/data-sources/{source_id}")
    assert deleted.status_code == 204, deleted.text
    assert await _search_titles(client, "fresh-source", "narwhalscan", "scan_config") == []


@pytest.mark.asyncio
async def test_deleting_a_source_clears_the_scan_name_from_its_rules_subtitle(
    client: AsyncClient,
) -> None:
    """The OTHER document kind the same cascade moves, covered by the SAME call.

    ``_alert_rule_document`` builds its subtitle from the name of the scan the
    rule is narrowed to, and the cascade unbinds the rule
    (``disable_rules_bound_to_scan``) without rebuilding its document — so the
    rule went on advertising a scan that no longer exists. This is why the fix
    needs no second mechanism: one whole-branch reindex regenerates every kind at
    once, and this test is what proves the free coverage is real (tripl-9jvz).
    """
    await client.post("/api/v1/projects", json={"name": "Fresh", "slug": "fresh-subtitle"})
    source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Subtitled warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "default",
        },
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]

    scan = await client.post(
        "/api/v1/projects/fresh-subtitle/scans",
        json={
            "data_source_id": source_id,
            "name": "Manateescan nightly",
            "base_query": "SELECT * FROM warehouse.checkout_events",
        },
    )
    assert scan.status_code == 201, scan.text

    destination = await client.post(
        "/api/v1/projects/fresh-subtitle/alert-destinations",
        json={
            "type": "slack",
            "name": "Main Slack",
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T000/B000/XXX",
        },
    )
    assert destination.status_code == 201, destination.text
    rule = await client.post(
        f"/api/v1/projects/fresh-subtitle/alert-destinations/{destination.json()['id']}/rules",
        json={"name": "Dugong collapse watch", "scan_config_id": scan.json()["id"]},
    )
    assert rule.status_code == 201, rule.text

    before = await _search_hits(client, "fresh-subtitle", "dugong", "alert_rule")
    assert [hit["subtitle"] for hit in before] == ["Manateescan nightly"]

    deleted = await client.delete(f"/api/v1/data-sources/{source_id}")
    assert deleted.status_code == 204, deleted.text

    # The rule itself SURVIVES the cascade — unbound and disabled, but still
    # indexed, because "why am I not getting alerts about X" is exactly when
    # someone searches for it. Only the scan name it used to carry is gone.
    after = await _search_hits(client, "fresh-subtitle", "dugong", "alert_rule")
    assert [hit["title"] for hit in after] == ["Dugong collapse watch"]
    assert [hit["subtitle"] for hit in after] == [""]


@pytest.mark.asyncio
async def test_the_read_path_enqueues_the_task_name_the_worker_registers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``send_task`` routes by NAME, so a typo there is a silently dead feature.

    Publishing an unregistered name raises nothing: the message is accepted, no
    worker ever claims it, and the only symptom is a branch that stays unindexed
    while every log line looks healthy. Asserting against the task's own ``name``
    ties the two ends of the wire together (tripl-zbv0).
    """
    from tripl.worker import celery_app as celery_module
    from tripl.worker.tasks import search as search_tasks

    sent: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        celery_module.celery_app,
        "send_task",
        lambda name, args: sent.append((name, args)),
    )
    project_id, branch_id = uuid.uuid4(), uuid.uuid4()

    assert await search_service._queue_branch_reindex(project_id, branch_id) is True
    assert sent == [(search_tasks.reindex_search_branch.name, [str(project_id), str(branch_id)])]


@pytest.mark.asyncio
async def test_a_broker_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search GET must answer even when the broker is gone."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("celery broker gone")

    from tripl.worker import celery_app as celery_module

    monkeypatch.setattr(celery_module.celery_app, "send_task", boom)

    assert await search_service._queue_branch_reindex(uuid.uuid4(), uuid.uuid4()) is False


@pytest.mark.asyncio
async def test_semantic_false_never_reaches_the_embedding_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``semantic=False`` is lexical-only: no provider call, no demo vector.

    The palette asks for this answer first and upgrades to the full one when it
    lands; on production the embedding round trip was the whole difference
    between results at ~150 ms and at ~1.5 s (tripl-kjhi.15). The flag has to
    skip the embed leg outright — a call that is made and thrown away would
    still be paid for.
    """

    async def fake_lexical(_session: object, **_kwargs: object) -> list[SearchResult]:
        return []

    def fake_embed(_query: str, *, config: AiConfig) -> list[float]:
        pytest.fail("semantic=False must not call the embedding provider")

    async def fake_semantic(_session: object, **_kwargs: object) -> list[SearchResult]:
        pytest.fail("semantic=False must not run the vector leg")

    enabled = replace(env_ai_config(), search_embeddings_enabled=True)

    async def fake_ai_config(_session: object) -> AiConfig:
        return enabled

    monkeypatch.setattr(_search_query, "postgres_lexical_search", fake_lexical)
    monkeypatch.setattr(_search_query, "postgres_semantic_search", fake_semantic)
    monkeypatch.setattr(_search_query, "embed_query", fake_embed)
    monkeypatch.setattr(app_settings_service, "get_ai_config", fake_ai_config)

    async with TestSessionLocal() as session:
        _, semantic_used = await _search_query.postgres_search(
            session,
            project_id=uuid.uuid4(),
            branch_id=uuid.uuid4(),
            query="session",
            entity_types=None,
            include_archived=False,
            limit=10,
            project_is_demo=True,
            semantic=False,
        )

    assert semantic_used is False


@pytest.mark.asyncio
async def test_search_endpoint_accepts_the_semantic_flag(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "Lex", "slug": "search-lexical"})
    event_type = await client.post(
        "/api/v1/projects/search-lexical/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    created = await client.post(
        "/api/v1/projects/search-lexical/events",
        json={"event_type_id": event_type.json()["id"], "name": "checkout_started"},
    )
    assert created.status_code == 201, created.text

    lexical_only = await client.get(
        "/api/v1/projects/search-lexical/search", params={"q": "checkout", "semantic": "false"}
    )
    assert lexical_only.status_code == 200, lexical_only.text
    body = lexical_only.json()
    assert body["semantic_used"] is False
    assert [item["name"] for item in body["items"]] == ["checkout_started"]
