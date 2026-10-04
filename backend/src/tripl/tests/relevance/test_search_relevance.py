"""The search-relevance harness: three tests per measured ranking case.

ONE CASE, THREE CLAIMS, THREE TEST FUNCTIONS
---------------------------------------------------------
A case asserts up to three different things and they do not deserve the same
treatment, so they are asserted separately:

* :func:`test_relevance_case_retrieves_what_it_names` — the document comes back
  at all. Never xfailable.
* :func:`test_relevance_case_ranks_the_expected_document_first` — it comes back
  first, ahead of the competitors that beat it on production. This is the ONLY
  one an ``xfail_ordering`` marker can excuse.
* :func:`test_relevance_case_does_not_overstate_confidence` — the top hit is not
  served as certain. Never xfailable.

They were one function, and ``pytest.mark.xfail`` marks a function, so a marker
filed against a ranking nuance also excused the document vanishing from the
results entirely. That was not hypothetical: ``russian-phrase-finds-the-event-it-
describes`` was xfailed for a scoring gap while the fault four
earlier fixes were aimed at is precisely ``screen_spot`` not being RETRIEVED —
so the harness was carrying a ranking marker that would have hidden the
regression it exists to catch. That marker is gone: the coverage-term change shipped
``_search_query.COVERAGE_BONUS`` and the case passes on its own, which is what
:func:`test_the_coverage_term_is_what_wins_the_russian_phrase_case` below exists
to keep honest. The split outlives it — no case carries ``xfail_ordering``
today, and the next one that does will be excused for its ORDER only.

This is the first test in the repository that executes the production ranking
SQL. Everything else about search is asserted either against generated SQL
strings or against ``sqlite_search``, the Python fallback that exists only
because the suite runs on SQLite — so ``postgres_lexical_search``'s
``ts_rank_cd``/trigram/boost arithmetic and ``merge_results`` had no coverage at
all, and any weight in them could be changed without a single test noticing.

Read :mod:`tripl.tests.relevance.cases` for the case table and what each case was
measured to do on production, and :mod:`tripl.tests.relevance.corpus` for the
fixed corpus every case is ranked against.

WHAT THIS FILE IS NOT THE RIGHT TOOL FOR
----------------------------------------
Everything here is ranking over a corpus, and a corpus supplies many paths to
the right answer — trigram similarity, the ``LIKE`` tiers, term frequency. That
makes it strong evidence about ORDER and weak evidence about whether a
RETRIEVAL mechanism works, because the mechanism under test is rarely the only
thing that could have produced the result. Claims about the text-search
mechanism itself belong in
:mod:`tripl.tests.relevance.test_stemming_invariants`, which seeds nothing and
asks Postgres a question with exactly one possible cause.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.search_document import SearchDocument
from tripl.schemas.search import SearchResult
from tripl.services import search_service
from tripl.services._search_query import COVERAGE_BONUS, _is_postgres
from tripl.tests.relevance.cases import (
    CASES,
    EVENT_SCREEN_SPOT,
    FIELD_SCREEN,
    RelevanceCase,
)
from tripl.tests.relevance.corpus import Corpus

#: Every case queries with the SAME limit, and that limit is >= 50 on purpose.
#: ``search_service.search_project`` widens the candidate window by 24 only when
#: the requested limit is under 50, which makes the top result of a query depend
#: on how many results were asked for (measured: ``q='экран спота'`` returns a
#: different top-1 at limit=5 than at limit=50). That instability is filed
#: separately and is NOT this harness's subject; pinning the limit above the
#: threshold keeps every assertion here independent of it. The corpus is also
#: smaller than this limit, so nothing is ever truncated and the ranking under
#: test is total.
SEARCH_LIMIT = 50


def _rank_of(items: list[SearchResult], title: str) -> int | None:
    for index, item in enumerate(items):
        if item.title == title:
            return index
    return None


def _describe(items: list[SearchResult]) -> str:
    if not items:
        return "<no results>"
    return ", ".join(f"{item.title}={item.score:.3f}" for item in items[:8])


async def _search(session: AsyncSession, corpus: Corpus, query: str) -> list[SearchResult]:
    response = await search_service.search_project(
        session,
        corpus.slug,
        query,
        limit=SEARCH_LIMIT,
    )
    return list(response.items)


async def test_the_harness_executes_the_postgres_ranking_path(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """Prove the harness is testing what it claims to test.

    Without this, a misconfigured fixture could silently fall back to the SQLite
    scorer and the whole case table would be asserting the wrong implementation —
    the exact failure mode that let the ranking SQL go untested for its entire
    life. Three things are checked: the session really speaks PostgreSQL, the
    corpus really got indexed, and the semantic leg really is off (so the case
    table measures lexical ranking and nothing else).
    """
    assert _is_postgres(relevance_session), "the harness is not connected to PostgreSQL"

    indexed = await relevance_session.scalar(
        select(func.count())
        .select_from(SearchDocument)
        .where(
            SearchDocument.project_id == seeded_corpus.project_id,
            SearchDocument.branch_id == seeded_corpus.branch_id,
        )
    )
    assert indexed is not None and indexed >= 20, f"corpus indexed only {indexed} documents"

    vectorized = await relevance_session.scalar(
        select(func.count())
        .select_from(SearchDocument)
        .where(
            SearchDocument.project_id == seeded_corpus.project_id,
            SearchDocument.branch_id == seeded_corpus.branch_id,
            SearchDocument.text_vector.is_(None),
        )
    )
    assert vectorized == 0, f"{vectorized} documents have no text_vector; ts_rank_cd sees nothing"

    response = await search_service.search_project(
        relevance_session,
        seeded_corpus.slug,
        "purchase_completed",
        limit=SEARCH_LIMIT,
    )
    assert response.items, "an exact event name returned nothing; the index is not queryable"
    assert response.semantic_used is False, (
        "the semantic leg ran; rankings in this harness would not be reproducible"
    )


async def test_the_coverage_term_is_what_wins_the_russian_phrase_case(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """The whole winning margin is bought by ``COVERAGE_BONUS``, and no more.

    ``russian-phrase-finds-the-event-it-describes`` is green now, and a green
    ordering case is the weakest evidence this harness produces — it says the
    right document is on top, not WHY. This bounds the margin on both sides so
    that "green" can only mean "green for the reason the coverage term names":

    * ``> 0`` — the event beats the field document at all;
    * ``< COVERAGE_BONUS`` — and it does NOT beat it without the bonus. Measured:
      ``screen_spot`` 1.8294 against ``Экран`` 1.0000, a margin of 0.8294 against
      a bonus of 1.0. Both documents' pre-bonus scores are what they were before
      the fix (0.8294 and 1.0000), and only one of them is paid, so the margin is
      exactly ``COVERAGE_BONUS`` minus the 0.1706 the complete match was losing
      by.

    THE MUTATION THAT MUST KILL THIS, WHICH IS NOT THE ONE THAT KILLS THE CASE
    -------------------------------------------------------------------------
    Join ``event.description`` into the keywords in
    ``_search_documents._event_document`` and ``screen_spot`` earns the 3.25
    stemmed tier on its own. The CASE would then be green with the coverage term
    deleted — the base ranking would win it — and this assertion is what notices:
    the margin blows past ``COVERAGE_BONUS``. Deleting ``+ coverage_score``
    instead takes the margin negative and kills both.
    """
    items = await _search(relevance_session, seeded_corpus, "экран спота")
    ranking = _describe(items)

    event = next((item for item in items if item.title == EVENT_SCREEN_SPOT), None)
    field = next((item for item in items if item.title == FIELD_SCREEN), None)
    assert event is not None and field is not None, (
        f"q='экран спота' must return both {EVENT_SCREEN_SPOT!r} and {FIELD_SCREEN!r} "
        f"for this comparison to mean anything. Got: {ranking}."
    )

    margin = event.score - field.score
    assert margin > 0, (
        f"{EVENT_SCREEN_SPOT!r} ({event.score:.4f}) does not beat {FIELD_SCREEN!r} "
        f"({field.score:.4f}); the coverage term is not doing its job. Got: {ranking}."
    )
    assert margin < COVERAGE_BONUS, (
        f"{EVENT_SCREEN_SPOT!r} beats {FIELD_SCREEN!r} by {margin:.4f}, which is more "
        f"than the whole COVERAGE_BONUS of {COVERAGE_BONUS}. The case is green for "
        f"some OTHER reason, so deleting the coverage term would no longer fail it "
        f"and the coverage term has lost its guard. Got: {ranking}."
    )


#: Each claim gets its own parametrization, and a case only appears where it
#: actually asserts something. Filtering here rather than returning early inside
#: the test bodies matters: a case that silently checked nothing would otherwise
#: be REPORTED as a passing check of something it never looked at — the same
#: self-deception as a case that is green for the wrong reason, one level down.
RETRIEVAL_CASES: tuple[RelevanceCase, ...] = tuple(
    case for case in CASES if case.expect_top or case.must_retrieve
)
ORDERING_CASES: tuple[RelevanceCase, ...] = tuple(case for case in CASES if case.expect_top)
CONFIDENCE_CASES: tuple[RelevanceCase, ...] = tuple(
    case for case in CASES if case.max_top_confidence is not None
)

#: Which corpus fixture is supposed to supply the match each confidence case is
#: bounding, so an EMPTY result set points at the right place instead of at
#: whichever fixture happened to be the only one when the message was written.
_CONFIDENCE_CASE_FIXTURES: dict[str, str] = {
    "garbage-query-is-not-a-confident-answer": "see corpus._SESSION_KEYS",
    "russian-phrase-finds-the-event-it-describes": (
        "see corpus for the screen_spot event, whose description is 'Показ экрана спота'"
    ),
}


def test_every_case_makes_at_least_one_assertion() -> None:
    """No case may be collected three times and checked zero times.

    ``RelevanceCase.__post_init__`` refuses such a case at import time, so this
    is a second lock on the same door — but it is the lock that is readable from
    the test side, where someone debugging a suspiciously green run will look. It
    also fails loudly if the dataclass validation is ever softened.
    """
    for case in CASES:
        asserted = (
            bool(case.expect_top) or bool(case.must_retrieve) or case.max_top_confidence is not None
        )
        assert asserted, f"case {case.id!r} is parametrized into every test and checked by none"


@pytest.mark.parametrize(
    "case",
    RETRIEVAL_CASES,
    ids=[case.id for case in RETRIEVAL_CASES],
)
async def test_relevance_case_retrieves_what_it_names(
    case: RelevanceCase,
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """The documents a case names must come back. NEVER xfailable.

    THIS FUNCTION READS NO ``xfail_ordering`` FIELD, AND THAT IS THE FEATURE
    ------------------------------------------------------------------------
    Retrieval and ordering used to be asserted by one function, so a marker filed
    against a RANKING nuance also excused the document disappearing from the
    result set entirely. ``russian-phrase-finds-the-event-it-describes`` was the
    worked instance: it was xfailed because a short almost-exact title outranked
    a complete match, and under the old arrangement the ``screen_spot`` event
    could have gone back to being ABSENT — the production fault four fixes were
    aimed at — with the case still reporting a tidy expected failure. A ranking
    regression and a retrieval regression are different failures with different
    causes, and the harness now has to say which one it is looking at. The marker
    was deleted when the coverage term landed; the arrangement is what it left behind.

    ``must_retrieve`` is asserted here too, with no position claim: it carries the
    cases about the retrieval MECHANISM that have no measured ranking (the
    over-stem case), and the competitors whose presence the ordering assertions
    silently depend on.
    """
    items = await _search(relevance_session, seeded_corpus, case.query)
    ranking = _describe(items)

    expected = tuple(title for title in (case.expect_top,) if title) + case.must_retrieve
    assert items, (
        f"q={case.query!r} returned nothing at all, so none of {expected} can be "
        f"found. Measured {case.measured}."
    )
    for title in expected:
        assert _rank_of(items, title) is not None, (
            f"q={case.query!r} did not return {title!r} anywhere in {len(items)} "
            f"results. Got: {ranking}. Measured {case.measured}."
        )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            id=case.id,
            marks=(
                pytest.mark.xfail(strict=True, reason=case.xfail_ordering)
                if case.xfail_ordering
                else ()
            ),
        )
        for case in ORDERING_CASES
    ],
)
async def test_relevance_case_ranks_the_expected_document_first(
    case: RelevanceCase,
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """The ORDER claim, and the only claim an ``xfail_ordering`` may excuse.

    Retrieval is re-derived here rather than assumed, because a strict xfail on
    this function must fail for the reason the marker names. If the document were
    missing the ``_rank_of`` below would return ``None`` and this would raise a
    confusing ``TypeError`` instead — so it is asserted first, with a message
    that points at the retrieval test, which is where that failure belongs.
    """
    items = await _search(relevance_session, seeded_corpus, case.query)
    ranking = _describe(items)
    assert case.expect_top is not None  # guaranteed by the parametrization filter

    rank = _rank_of(items, case.expect_top)
    assert rank is not None, (
        f"q={case.query!r} did not return {case.expect_top!r} at all, so there is "
        f"no order to check — this is a RETRIEVAL failure and "
        f"test_relevance_case_retrieves_what_it_names is reporting it too. "
        f"Got: {ranking}. Measured {case.measured}."
    )
    for competitor in case.must_not_outrank:
        competitor_rank = _rank_of(items, competitor)
        assert competitor_rank is None or competitor_rank > rank, (
            f"q={case.query!r}: {competitor!r} outranks {case.expect_top!r} "
            f"(#{competitor_rank} vs #{rank}). Got: {ranking}. "
            f"Measured {case.measured}."
        )
    assert rank == 0, (
        f"q={case.query!r}: {case.expect_top!r} ranked #{rank}, not first. "
        f"Got: {ranking}. Measured {case.measured}."
    )


@pytest.mark.parametrize(
    "case",
    CONFIDENCE_CASES,
    ids=[case.id for case in CONFIDENCE_CASES],
)
async def test_relevance_case_does_not_overstate_confidence(
    case: RelevanceCase,
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """The score claim. Also never xfailable.

    A confidence bound is not an ordering nuance — "we told the user this was a
    certain answer when it was not" is a user-visible defect on its own — so it
    does not live in the function a marker can excuse.
    """
    items = await _search(relevance_session, seeded_corpus, case.query)
    ranking = _describe(items)
    assert case.max_top_confidence is not None  # guaranteed by the parametrization filter

    # The empty-result guard names the fixture that is supposed to supply the
    # match, and that name is PER CASE. It used to be hard-coded to
    # `corpus._SESSION_KEYS`, which is the garbage-query case's fixture and was
    # the only case here — a second case would have been told to go look at a
    # corpus entry that has nothing to do with it.
    assert items, (
        f"q={case.query!r} returned nothing, so the confidence claim cannot be "
        f"observed. The corpus is supposed to contain a match for it "
        f"({_CONFIDENCE_CASE_FIXTURES.get(case.id, 'see corpus.py')}). "
        f"Measured {case.measured}."
    )
    assert items[0].confidence <= case.max_top_confidence, (
        f"q={case.query!r} is served at confidence {items[0].confidence} on an "
        f"absolute score of {items[0].score:.3f}; nothing this weak may be "
        f"presented as a certain answer. Got: {ranking}. Measured {case.measured}."
    )
