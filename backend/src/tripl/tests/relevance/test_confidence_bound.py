"""Only a document that IS what was typed may be painted as certain.

WHY THIS NEEDS A REAL POSTGRESQL, AND WHY THE BACKEND SUITE CANNOT HOST IT
--------------------------------------------------------------------------
The defect lived entirely in the Postgres score. ``_FULL_CONFIDENCE_SCORE`` was
derived as ``5.0`` (exact title) ``+ 2.0`` (a perfect trigram on that same
title), and confidence was that fraction of the TOTAL — but the total also
carries ``lexical_score * 4.0`` and, since the coverage fix, ``COVERAGE_BONUS``.
Neither was in the derivation, so documents that are not the thing named crossed
the line anyway.

On SQLite that could never happen: ``fallback_score`` returns ONE tier value per
document and never sums evidence, and only its two equality tiers sit at or above
the line. So the guarantee the whole backend suite was verifying was one the
dialect users actually search on did not provide. That is why this file is here
and not in ``tests/test_search.py``.

THREE ASSERTIONS, AND THE FIRST IS WHAT MAKES THE OTHERS MEAN ANYTHING
-----------------------------------------------------------------------
"No result reports 1.0" is satisfied by a great many broken states — a
confidence stuck at zero, a cap applied to everything, a query that returned
nothing. So this file also asserts that an exact title DOES still reach 1.0.
Without that control, deleting the confidence calculation entirely would leave
this file green.

The same shape as :mod:`tripl.tests.relevance.test_coverage_invariants`, and for
the same reason.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.schemas.search import SearchResult
from tripl.services import search_service
from tripl.services._search_query import _PARTIAL_CONFIDENCE_CEILING
from tripl.tests.relevance.cases import (
    CASES,
    EVENT_PURCHASE,
    EVENT_SCREEN_SPOT,
    VAR_CARD_TARGET,
    RelevanceCase,
)
from tripl.tests.relevance.corpus import Corpus

pytestmark = pytest.mark.relevance

#: Matches ``test_search_relevance.SEARCH_LIMIT`` so the candidate window — and
#: therefore every score in it — is the one the case table was measured at.
SEARCH_LIMIT = 50


async def _search(session: AsyncSession, corpus: Corpus, query: str) -> list[SearchResult]:
    response = await search_service.search_project(session, corpus.slug, query, limit=SEARCH_LIMIT)
    return list(response.items)


def _find(items: list[SearchResult], title: str) -> SearchResult | None:
    return next((item for item in items if item.title == title), None)


async def test_an_exact_title_is_still_served_as_a_certain_answer(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """The control. Without it every other assertion here is vacuous."""
    items = await _search(relevance_session, seeded_corpus, EVENT_PURCHASE)

    assert items, "an exact event name returned nothing"
    top = items[0]
    assert top.title == EVENT_PURCHASE, f"expected the named event first, got {top.title!r}"
    assert top.identity_match is True, "an exact title did not register as an identity match"
    assert top.confidence == 1.0, (
        f"an exact-title match is served at {top.confidence}, not 1.0 — the cap is "
        "swallowing the case it is supposed to leave alone"
    )


@pytest.mark.parametrize(
    ("query", "title"),
    [
        # The two crossings recorded in the original report. Neither document is the thing
        # named: `screen_spot` merely CONTAINS `spot`, and `${property.card_target}`
        # holds `screen_settings` in harvested text.
        pytest.param("spot", EVENT_SCREEN_SPOT, id="spot-does-not-certify-screen_spot"),
        pytest.param(
            "screen_settings", VAR_CARD_TARGET, id="screen_settings-does-not-certify-card_target"
        ),
    ],
)
async def test_a_measured_crossing_no_longer_reports_a_certain_answer(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
    query: str,
    title: str,
) -> None:
    items = await _search(relevance_session, seeded_corpus, query)

    hit = _find(items, title)
    assert hit is not None, (
        f"{title!r} was not returned for q={query!r}; the case cannot assert anything "
        "about a document that is absent"
    )
    assert hit.identity_match is False, (
        f"{title!r} registered as an identity match for q={query!r}, which would make "
        "this case vacuous — check the boost ladder, not the cap"
    )
    assert hit.confidence <= _PARTIAL_CONFIDENCE_CEILING, (
        f"{title!r} is served at {hit.confidence} for q={query!r}, above the partial "
        f"ceiling {_PARTIAL_CONFIDENCE_CEILING}"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
async def test_no_partial_match_anywhere_in_the_table_claims_certainty(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
    case: RelevanceCase,
) -> None:
    """The general invariant, over every query the harness already measures.

    Two hand-picked crossings are what was originally recorded, but the bound is not
    about those two documents — it is about the arithmetic. Sweeping the whole
    case table means the next term added to the score sum cannot quietly push
    some OTHER partial match over the line without a case going red, which is
    exactly how ``COVERAGE_BONUS`` widened the defect the first time.
    """
    items = await _search(relevance_session, seeded_corpus, case.query)

    certain = [item for item in items if item.confidence >= 1.0 and not item.identity_match]
    assert not certain, (
        f"q={case.query!r} served {[(item.title, item.confidence) for item in certain]} at full "
        "confidence without an identity match"
    )
