"""The two keyword tiers, read on their own instead of through a total score.

WHY THE CASE TABLE COULD NOT SEE THIS
-------------------------------------
``test_search_relevance`` asserts ORDER, and an order is produced by three legs
added together — the boost ladder, ``ts_rank_cd * 4.0`` and the trigram
similarity ``* 2.0``. So a tier can be paying the WRONG document and the case can
still be green, as long as another leg covers the difference. That is not
hypothetical: until tripl-0qld, ``q='purchases'`` paid ``app_open``,
``screen_home`` and ``screen_settings`` the 3.5 literal keyword-token tier —
purely because ``${property.screen_name}`` had harvested the string ``purchases``
onto their ``view_id`` field and ``_event_document`` joined a variable's observed
values into an EVENT's ``keywords`` — while ``purchase_completed``, the entity the
query is about, reached only the 3.25 stemmed-identity tier. The ladder ordering
tripl-nh5s built ("a stemmed match on the entity's own name is stronger than the
literal token appearing somewhere in its body") was inverted for three unrelated
events, and ``cases.purchase-plural`` stayed green because the trigram leg
(``similarity('purchase_completed', 'purchases') ~ 0.38 -> +0.76``) more than
covered the 0.25 the ladder had given away.

This module therefore reads the ``boost`` column and nothing else. It imports
:data:`~tripl.services._search_query.BOOST_LADDER_EXPRESSION` and
:data:`~tripl.services._search_query.TEXT_QUERY_EXPRESSION` rather than copying
the SQL, for the same reason ``test_stemming_invariants`` imports its two
expressions: a copied literal keeps passing after the shipped one is edited,
which is precisely how this harness has certified a wrong repair before.

WHAT IT CANNOT DO, SAID HERE RATHER THAN IMPLIED
------------------------------------------------
It reproduces the ``ranked`` CTE's projection, not the whole query: no WHERE
retrieval clause, no ``ORDER BY``, no ``merge_results``. It is evidence about
which tier a document lands on and about nothing else. The claim "and therefore
the right document ranks first" is still ``test_search_relevance``'s to make.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.search_document import SearchDocument
from tripl.services._search_query import (
    BOOST_LADDER_EXPRESSION,
    TEXT_QUERY_EXPRESSION,
    token_boundary_regex,
)
from tripl.tests.relevance.corpus import Corpus

#: The ``ranked`` CTE's boost projection, over one branch's EVENT documents.
#:
#: Every bind parameter the ladder can reference is supplied by
#: :func:`_boosts` below — ``:query``, ``:has_token_regex``, ``:token_regex``,
#: ``:prefix`` and ``:contains`` — because a CASE arm that is never reached is
#: still parsed and still needs its parameter. Missing one raises at execute
#: time rather than producing a wrong number, which is the failure mode worth
#: naming: it would look like a broken test rather than a broken tier.
_EVENT_BOOSTS = text(
    f"""
    WITH q AS (
        SELECT ({TEXT_QUERY_EXPRESSION}) AS tsq
    )
    SELECT d.title AS title, ({BOOST_LADDER_EXPRESSION}) AS boost
    FROM search_documents d
    CROSS JOIN q
    WHERE d.project_id = :project_id
      AND d.branch_id = :branch_id
      AND d.entity_type = 'event'
    """  # noqa: S608 - both operands are module constants, not user input
)

#: The three pageview events ``${property.screen_name}`` is bound to. They have a
#: ``view_id`` value, so ``_event_document`` picks up that variable's contexts —
#: which is how its harvested plurals reached their documents at all. See
#: ``corpus._SCREEN_NAMES``.
_HARVEST_CARRIERS = ("app_open", "screen_home", "screen_settings")

#: The literal plurals that variable harvested. Each is the wrong answer to one
#: of the plural cases in ``cases.py``.
_HARVESTED_PLURALS = ("purchases", "spots", "уловы")


async def _boosts(session: AsyncSession, corpus: Corpus, query: str) -> dict[str, float]:
    token_regex = token_boundary_regex(query)
    assert token_regex is not None, (
        f"q={query!r} is not identifier-shaped, so the 3.5/3.0 word-boundary "
        f"tiers are switched off and this query cannot measure them"
    )
    rows = (
        await session.execute(
            _EVENT_BOOSTS,
            {
                "project_id": corpus.project_id,
                "branch_id": corpus.branch_id,
                "query": query,
                "token_regex": token_regex,
                "has_token_regex": True,
                "prefix": f"{query}%",
                "contains": f"%{query}%",
            },
        )
    ).mappings()
    return {row["title"]: float(row["boost"]) for row in rows}


async def test_a_harvested_value_does_not_buy_an_event_the_keyword_tier(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """q='purchases': the entity takes 3.25, the documents that merely quote it take 3.0.

    THE MUTATION THIS EXISTS TO FAIL, NAMED EXACTLY
    Restoring ``" ".join(variable_context_text)`` (values included) to
    ``_search_documents._event_document``'s ``keywords`` join makes
    ``d.keywords ~* '\\mpurchases\\M'`` fire for all three carriers, their boost
    becomes 3.5, and every equality below breaks at once. Restoring
    ``" ".join(safe_values)`` in place of ``authored_values`` does not move THIS
    query — the corpus seeds every ``EventFieldValue`` unauthored and none of
    them spells a plural — which is why that half is pinned on the document text
    instead, by ``tests/test_search.py::test_event_keywords_carry_only_curated_
    text_while_body_keeps_the_harvest``.
    """
    boosts = await _boosts(relevance_session, seeded_corpus, "purchases")

    assert boosts.get("purchase_completed") == pytest.approx(3.25), (
        "the entity whose name stems to the query must take the stemmed-identity "
        f"tier. Got: {boosts}"
    )
    for carrier in _HARVEST_CARRIERS:
        assert boosts.get(carrier) == pytest.approx(3.0), (
            f"{carrier!r} spells 'purchases' only in its BODY, via a value "
            f"${{property.screen_name}} harvested from traffic, so the 3.0 "
            f"body-token tier is the whole of what it may earn. Got: {boosts}"
        )
        assert boosts["purchase_completed"] > boosts[carrier], (
            "the ladder itself must order these, not the trigram leg papering "
            f"over an inversion. Got: {boosts}"
        )


async def test_the_keyword_token_tier_still_fires_on_an_event_s_own_identity(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """The 3.5 tier is narrowed, not deleted — otherwise the test above is vacuous.

    ``q='pageviews'`` is the event TYPE's name, which ``_event_document`` joins
    into every member event's keywords as identity text and always did. If a
    future change empties an event's keywords wholesale, the assertions above go
    on passing (3.0 beats a boost of 0 just as well) while search quietly loses a
    tier. This is the assertion that notices.
    """
    boosts = await _boosts(relevance_session, seeded_corpus, "pageviews")

    for carrier in _HARVEST_CARRIERS:
        assert boosts.get(carrier) == pytest.approx(3.5), (
            f"{carrier!r} carries its event type's name in keywords as curated "
            f"identity text, so the literal keyword-token tier must pay it. "
            f"Got: {boosts}"
        )


async def test_event_keywords_hold_the_binding_and_the_body_holds_the_harvest(
    relevance_session: AsyncSession,
    seeded_corpus: Corpus,
) -> None:
    """The stored columns behind the tiers above, asserted on the real index.

    ``test_search.py``'s unit test proves ``_event_document`` builds the split;
    this proves the split survives ``reindex_project_branch`` and is what the
    ranking SQL actually reads. It also pins the other direction: the BINDING
    (``properties.screen_name``) stays in keywords, so a "fix" that deleted the
    variable context text outright would fail here rather than look correct.
    """
    rows = (
        await relevance_session.execute(
            select(SearchDocument.title, SearchDocument.keywords, SearchDocument.body).where(
                SearchDocument.project_id == seeded_corpus.project_id,
                SearchDocument.branch_id == seeded_corpus.branch_id,
                SearchDocument.entity_type == "event",
                SearchDocument.title.in_(_HARVEST_CARRIERS),
            )
        )
    ).all()
    assert len(rows) == len(_HARVEST_CARRIERS), (
        f"expected one document per carrier, got {[row.title for row in rows]}"
    )

    for row in rows:
        for plural in _HARVESTED_PLURALS:
            assert plural not in row.keywords, (
                f"{row.title!r}: {plural!r} is a value ${{property.screen_name}} "
                f"harvested from traffic; keywords is what the 3.5 and 3.25 tiers "
                f"read. keywords={row.keywords!r}"
            )
            assert plural in row.body, (
                f"{row.title!r}: {plural!r} must stay searchable as body text — "
                f"removing it from the index is a different change from removing "
                f"it from keywords. body={row.body!r}"
            )
        assert "properties.screen_name" in row.keywords, (
            f"{row.title!r}: the binding a variable was detected on is identity "
            f"text and stays; only the observed values left. "
            f"keywords={row.keywords!r}"
        )
