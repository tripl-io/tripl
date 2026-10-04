"""The coverage invariant, asserted on the mechanism itself.

WHY THIS FILE EXISTS, AND WHY IT IS THE PRIMARY GUARD FOR THE COVERAGE TERM
---------------------------------------------------------------------------
``COVERAGE_BONUS`` pays a document for satisfying ``d.text_vector @@ q.tsq``, and
that is only a COVERAGE signal because of a fact about the shipped tsquery:
``websearch_to_tsquery`` ANDs the terms WITHIN each leg, so
``_search_query.TEXT_QUERY_EXPRESSION`` is ``(a1 & b1) | (a2 & b2)`` and ``@@``
means "this document answered EVERY term", not "this document answered one of
them". If that ever became a disjunction, the bonus would silently turn into a
flat payment to every document that matched anything — the ranking would still
be green on the corpus for a while, and the term would have stopped measuring
what its name says.

So this file asks Postgres that one question directly, the way
:mod:`tripl.tests.relevance.test_stemming_invariants` does: NO corpus, nothing
seeded, no ranking SQL, no score. A document consisting of exactly one of the
query's two words either satisfies the shipped tsquery or it does not, and there
is no second path to a yes — no title to be trigram-similar to, no body for a
``LIKE`` to find a substring in, no boost tier to fire.

TWO ASSERTIONS, AND THE SECOND IS WHAT MAKES THE FIRST MEAN ANYTHING
---------------------------------------------------------------------
"``экране`` is not matched by ``q='экран спота'``" is satisfied by a great many
broken states: a stemmer that does nothing, a query expression that parses to
nothing, a configuration that vanished. So the same one-word document is also
asserted to BE matched by the part of the query it does hold (``q='экран'``), and
the two-word document to be matched by the whole query. The miss above is then
attributable to the CONJUNCTION and to nothing else.

WHY THESE WORD FORMS
--------------------
The Cyrillic pair is the measured fault: ``q='экран спота'`` is the harness case,
``Экран`` is the FIELD document whose entire title is one of the two words, and
``экране`` is the form ``test_stemming_invariants`` measured into the OTHER
lexical class from ``экран`` (``экран``/``экрана``/``экраны`` -> ``экра``,
``экране`` -> ``экран``). Using ``экране`` for the document therefore exercises
the stem-meets-surface identity at the same time, so a run where this file is
green only because the surface leg quietly matched everything would look
different from this one.

The English pair is a CONTROL, and it is not decoration: the two legs of the
tsquery are the Russian and the English Snowball dictionary routed by ASCII-ness
(migration ``a7c3e1b9d5f2``), so an assertion made only in Cyrillic says nothing
about the half of the corpus that is snake_case identifiers. ``screen`` and
``screen spot`` are the ASCII shape of exactly the same question.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.services._search_query import TEXT_QUERY_EXPRESSION
from tripl.services.search_service import TEXT_VECTOR_EXPRESSION

#: One document holding exactly the given text, matched against the given query,
#: using the SHIPPED expressions on both sides.
#:
#: This is deliberately the same shape as ``test_stemming_invariants._REACHES``,
#: including the four-column CAST-ed subselect: the document text is fed in as
#: ``title`` with the other three columns empty, which column carries it is
#: irrelevant because ``TEXT_VECTOR_EXPRESSION`` concatenates all four before
#: tokenizing, and the casts are there because ``concat_ws`` is variadic-``any``
#: and the driver would otherwise send a parameter of undetermined type.
#:
#: Both constants are IMPORTED and interpolated, never copied. A copied literal
#: would go on passing after either side was edited, which is the arrangement
#: that let the index and the query drift apart in the first place.
_MATCHES = text(
    f"""
    SELECT (
        SELECT {TEXT_VECTOR_EXPRESSION}
        FROM (
            SELECT
                CAST(:document AS text) AS title,
                CAST('' AS text) AS subtitle,
                CAST('' AS text) AS body,
                CAST('' AS text) AS keywords
        ) AS d
    ) @@ ({TEXT_QUERY_EXPRESSION}) AS matched
    """  # noqa: S608 - both operands are module constants, not user input
)

#: What each leg makes of one text. Used only to build failure messages, so a red
#: assertion says WHICH lexemes did or did not intersect without a psql session.
_LEXEMES = text(
    """
    SELECT
        CAST(to_tsvector('tripl_search', CAST(:document AS text)) AS text) AS stem,
        CAST(to_tsvector('tripl_search_surface', CAST(:document AS text)) AS text) AS surface
    """
)

#: (document text, two-word query). The document holds ONE of the query's two
#: words and must NOT satisfy the tsquery.
PARTIAL_DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("экране", "экран спота"),
    ("screen", "screen spot"),
)

#: (document text, query, why). The same documents, asked for what they DO hold,
#: plus the complete document asked for the whole query. Every one must match, or
#: the misses above are caused by something other than the conjunction.
COMPLETE_PAIRS: tuple[tuple[str, str, str], ...] = (
    (
        "экране",
        "экран",
        "the one-word document is reachable by the one word it holds — so its "
        "miss on the two-word query is the conjunction and not a stemming failure "
        "(экране indexes {экран, экране}, q='экран' asks {экра, экран})",
    ),
    (
        "экране спота",
        "экран спота",
        "the document holding BOTH words satisfies the whole query — this is the "
        "screen_spot event's side of the harness case",
    ),
    (
        "screen",
        "screen",
        "the English control's one-word document is reachable by its one word",
    ),
    (
        "screen spot",
        "screen spot",
        "the English control's complete document satisfies the whole query",
    ),
)


async def _matches(session: AsyncSession, *, document: str, query: str) -> bool:
    result = await session.scalar(_MATCHES, {"document": document, "query": query})
    return bool(result)


async def _explain(session: AsyncSession, *texts: str) -> str:
    parts: list[str] = []
    for value in texts:
        row = (await session.execute(_LEXEMES, {"document": value})).mappings().one()
        parts.append(f"{value!r}: stem={row['stem']} surface={row['surface']}")
    return "; ".join(parts)


@pytest.mark.parametrize(
    ("document", "query"),
    PARTIAL_DOCUMENTS,
    ids=[f"{document}-vs-{query}" for document, query in PARTIAL_DOCUMENTS],
)
async def test_a_partial_match_does_not_satisfy_the_shipped_tsquery(
    document: str,
    query: str,
    unseeded_session: AsyncSession,
) -> None:
    """One of two words is not a match. This is what makes ``@@`` a COVERAGE test.

    ``COVERAGE_BONUS`` is gated on ``d.text_vector @@ q.tsq`` and on nothing else,
    so the bonus is a payment for ANSWERING THE WHOLE QUERY only for as long as
    this holds.

    THE MUTATION THIS EXISTS TO FAIL
    --------------------------------
    Rewrite ``TEXT_QUERY_EXPRESSION`` to OR the terms — any per-term disjunctive
    construction, or an OR-ed recall fallback bolted on to widen the result set.
    The one-word document starts matching, this goes red, and without it that
    change would have quietly turned the coverage term into a flat bonus paid to
    every document that matched anything at all, with the corpus cases staying
    green because they are ranked against each other.
    """
    matched = await _matches(unseeded_session, document=document, query=query)
    assert not matched, (
        f"a document containing only {document!r} SATISFIES the tsquery for "
        f"{query!r}, which holds two words. The shipped query is no longer "
        f"conjunctive, so `text_vector @@ tsq` has stopped meaning 'answered every "
        f"term' and COVERAGE_BONUS is now paid for partial matches "
        f". {await _explain(unseeded_session, document, query)}."
    )


@pytest.mark.parametrize(
    ("document", "query", "why"),
    COMPLETE_PAIRS,
    ids=[f"{document}-from-{query}" for document, query, _ in COMPLETE_PAIRS],
)
async def test_the_same_document_is_matched_by_the_part_it_does_hold(
    document: str,
    query: str,
    why: str,
    unseeded_session: AsyncSession,
) -> None:
    """Prove the misses above are the CONJUNCTION and not a broken expression.

    Without this, every assertion in the file above would be satisfied by a
    stemmer that stopped working, a text search configuration that was never
    created, or a ``TEXT_QUERY_EXPRESSION`` that parses to an empty tsquery — all
    of which make everything miss everything, and all of which would be reported
    as a clean run.

    THE MUTATION THIS EXISTS TO FAIL
    --------------------------------
    Delete the surface leg from ``TEXT_QUERY_EXPRESSION``. ``экране`` indexes
    ``{экран, экране}`` while ``q='экран'`` would then ask only for the over-stem
    ``экра``; the pair stops meeting and this goes red while the partial-match
    test above stays green — which is exactly the asymmetry that tells "the
    conjunction is intact" apart from "nothing matches anything any more".
    """
    matched = await _matches(unseeded_session, document=document, query=query)
    assert matched, (
        f"a document containing {document!r} is NOT matched by a query for "
        f"{query!r}, so the partial-match assertions in this module prove nothing "
        f"about the conjunction — they would be green under a broken stemmer too. "
        f"Expected: {why}. {await _explain(unseeded_session, document, query)}."
    )
