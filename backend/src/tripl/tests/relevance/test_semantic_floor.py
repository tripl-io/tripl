"""Executable coverage for the semantic leg's cosine floor.

WHY THIS FILE EXISTS SEPARATELY FROM THE CASE TABLE
---------------------------------------------------
``_SEMANTIC_MIN_COSINE`` and its ``>= :min_cosine`` predicate are reachable only
through :func:`postgres_semantic_search`, and the case table
(:mod:`tripl.tests.relevance.cases`) deliberately runs with the semantic leg
OFF — ``seeded_corpus`` hard-fails if embeddings are enabled, because a live
provider would make the lexical rankings irreproducible. The headline fix of
the confidence change was therefore asserted by nothing at all: the floor could be deleted,
or set to 0.0, and every test in the repository would still pass.

The way out is that the floor does not need a provider — it needs VECTORS. This
module stamps three hand-built unit vectors onto three corpus documents and
queries the real SQL with a fourth, so the predicate under test executes against
a real PostgreSQL with pgvector and arithmetic that is exact rather than
model-dependent. No embedding call, no API key, and the case table's "the
semantic leg does not run here" invariant is untouched: these rows are written
inside the test's own transaction and are never committed, so nothing else in
the harness ever sees an embedded document.
"""

from __future__ import annotations

import math

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.services import _search_query
from tripl.services._search_query import (
    _SEMANTIC_MIN_COSINE,
    finalize_results,
    merge_results,
    postgres_semantic_search,
)
from tripl.tests.relevance.corpus import Corpus

#: The width of ``search_documents.embedding`` in the baseline schema.
#: A mismatch is a hard SQL error,
#: not a silent miss, so this constant cannot drift quietly.
_DIMENSIONS = 1536

#: Three events, picked because their titles are unique in the corpus and none of
#: them is archived. Which events they are does not matter — the vectors decide
#: everything here, and the lexical text is never consulted.
_STRONG_MATCH = "payment_failed"
_WEAK_MATCH = "app_open"
_NON_MATCH = "screen_settings"

#: Cosines chosen to straddle ``_SEMANTIC_MIN_COSINE`` (0.35) with room on both
#: sides: 0.92 is what a real rescue looks like, 0.45 is the low end of the band
#: the floor was calibrated to keep, and 0.15 is the flat tail it exists to cut.
_COSINES: dict[str, float] = {
    _STRONG_MATCH: 0.92,
    _WEAK_MATCH: 0.45,
    _NON_MATCH: 0.15,
}


def _unit_vector(cosine: float) -> list[float]:
    """A unit vector whose cosine similarity with :data:`_QUERY_VECTOR` is ``cosine``.

    Both vectors are unit length, so ``pgvector``'s ``<=>`` (cosine distance)
    returns exactly ``1 - cosine`` and the assertions below can be about precise
    numbers instead of tolerances wide enough to hide a broken predicate.
    """
    orthogonal = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    vector = [0.0] * _DIMENSIONS
    vector[0] = cosine
    vector[1] = orthogonal
    return vector


#: The "query embedding". e0, so a document vector's first component IS its
#: cosine with the query.
_QUERY_VECTOR = _unit_vector(1.0)


def _as_pgvector(embedding: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in embedding) + "]"


async def _stamp_embeddings(session: AsyncSession, corpus: Corpus) -> None:
    """Give three event documents an embedding, WITHOUT committing.

    Every other document in the corpus was indexed with embeddings disabled, so
    it is ``embedding_status='disabled'`` with a NULL embedding and cannot pass
    the semantic leg's WHERE. That makes the candidate set exactly these three
    rows and the expected result set knowable in full.
    """
    for title, cosine in _COSINES.items():
        result = await session.execute(
            text(
                """
                UPDATE search_documents
                SET embedding = CAST(:embedding AS vector),
                    embedding_status = 'ready',
                    embedding_model = 'relevance-harness'
                WHERE project_id = :project_id
                  AND branch_id = :branch_id
                  AND entity_type = 'event'
                  AND title = :title
                """
            ),
            {
                "embedding": _as_pgvector(_unit_vector(cosine)),
                "project_id": corpus.project_id,
                "branch_id": corpus.branch_id,
                "title": title,
            },
        )
        assert result.rowcount == 1, (
            f"expected exactly one event document titled {title!r} in the corpus, "
            f"got {result.rowcount}; the fixture drifted and this test is measuring nothing"
        )


async def _semantic_hits(session: AsyncSession, corpus: Corpus) -> dict[str, float]:
    results = await postgres_semantic_search(
        session,
        project_id=corpus.project_id,
        branch_id=corpus.branch_id,
        embedding=_QUERY_VECTOR,
        embedding_model="relevance-harness",
        entity_types=None,
        include_archived=False,
        limit=50,
    )
    return {item.title: item.score for item in results}


async def test_the_cosine_floor_drops_neighbours_that_are_not_matches(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """The floor keeps the rescues and cuts the tail — and it is the floor doing it.

    Without ``>= :min_cosine`` this leg is a plain nearest-neighbour scan: it has
    no notion of "no good answer", so it returns ``limit`` rows for ANY query and
    ``merge_results`` pays each of them ``cosine * 2.5``, which is enough for the
    nearest junk row to be served as a result.

    On its own this would only be an observation — "the junk row is absent" is
    also true of a corpus that never had one. The test below is its control: it
    lowers the floor to 0.0 and the same query returns the junk row, which is
    what proves the predicate (not the corpus, not the LIMIT) excluded it.
    """
    await _stamp_embeddings(relevance_session, seeded_corpus)

    hits = await _semantic_hits(relevance_session, seeded_corpus)

    assert set(hits) == {_STRONG_MATCH, _WEAK_MATCH}, (
        f"semantic leg returned {sorted(hits)}; expected the two documents above "
        f"the {_SEMANTIC_MIN_COSINE} floor and nothing else"
    )
    assert hits[_STRONG_MATCH] == pytest.approx(_COSINES[_STRONG_MATCH], abs=1e-4)
    assert hits[_WEAK_MATCH] == pytest.approx(_COSINES[_WEAK_MATCH], abs=1e-4)

    await relevance_session.rollback()


async def test_removing_the_floor_brings_the_non_match_back(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the test above: prove the predicate is load-bearing.

    A test that only asserts "the junk row is absent" passes just as happily on a
    corpus that never had a junk row. Lowering the floor has to bring it back,
    or the previous test is asserting the fixture rather than the fix.
    """
    await _stamp_embeddings(relevance_session, seeded_corpus)

    monkeypatch.setattr(_search_query, "_SEMANTIC_MIN_COSINE", 0.0)
    hits = await _semantic_hits(relevance_session, seeded_corpus)

    assert set(hits) == {_STRONG_MATCH, _WEAK_MATCH, _NON_MATCH}, (
        "with the floor at 0.0 the nearest-neighbour scan must return every "
        f"embedded document; got {sorted(hits)}"
    )
    assert hits[_NON_MATCH] == pytest.approx(_COSINES[_NON_MATCH], abs=1e-4)

    await relevance_session.rollback()


async def test_a_semantic_only_hit_is_not_reported_as_a_weak_answer(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """The confidence half of that change, executed end to end on real SQL.

    ``merge_results`` scores a vector-only hit ``cosine * 2.5``, so its score
    cannot exceed 2.5. Confidence uses the cosine, then caps non-identity hits
    at 0.8. This pins the rule on rows read from Postgres.
    """
    await _stamp_embeddings(relevance_session, seeded_corpus)

    results = await postgres_semantic_search(
        relevance_session,
        project_id=seeded_corpus.project_id,
        branch_id=seeded_corpus.branch_id,
        embedding=_QUERY_VECTOR,
        embedding_model="relevance-harness",
        entity_types=None,
        include_archived=False,
        limit=50,
    )
    finalized = finalize_results(merge_results([], results, 50), 50)
    confidence = {item.title: item.confidence for item in finalized}

    assert confidence[_STRONG_MATCH] == pytest.approx(0.8, abs=1e-3)
    assert confidence[_WEAK_MATCH] == pytest.approx(_COSINES[_WEAK_MATCH], abs=1e-3)
    # The number the old rule would have produced, named so the regression is
    # unmistakable rather than a mystery float.
    assert confidence[_STRONG_MATCH] > (_COSINES[_STRONG_MATCH] * 2.5) / 7.0

    await relevance_session.rollback()


def test_the_harness_query_vector_is_the_width_the_column_expects() -> None:
    """A vector of the wrong width is a SQL error, but a silent one to read.

    Asserted here so a dimension change shows up as this one-line failure instead
    of three confusing ``expected 1536 dimensions`` errors from the database.
    """
    assert len(_QUERY_VECTOR) == _DIMENSIONS
    assert all(len(_unit_vector(cosine)) == _DIMENSIONS for cosine in _COSINES.values())
    # Unit length is what makes `1 - (a <=> b)` exactly the cosine above.
    assert math.isclose(sum(value * value for value in _QUERY_VECTOR), 1.0, abs_tol=1e-9)
