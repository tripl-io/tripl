"""Search querying, ranking, and result shaping.

Contains the Postgres (lexical + semantic) and SQLite search paths, result
merging/boosting, event-hit enrichment, and response-shaping helpers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import Mapping
from difflib import SequenceMatcher
from typing import cast

from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event import Event
from tripl.models.field_definition import FieldDefinition
from tripl.models.search_document import SearchDocument
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue
from tripl.schemas.search import (
    SearchEntityType,
    SearchEventVariableValue,
    SearchResult,
)
from tripl.schemas.text_filters import strip_nul_bytes
from tripl.services import app_settings_service
from tripl.services._search_documents import _clean
from tripl.services.demo.search_embeddings import demo_query_embedding, load_demo_embedding_fixture
from tripl.services.embedding_service import embed_query, embedding_provenance, sanitize_embedding

logger = logging.getLogger(__name__)

# Cosine similarity a semantic hit must clear to be merged into the result set
# (tripl-txcz). Below this the vector leg is not "a weaker answer", it is noise:
# it is a plain nearest-neighbour scan, so it returns `limit` rows for ANY query
# a branch's embedded documents can be ranked against — there is no such thing
# as "no match" in it — and `merge_results` then pays each of those rows
# `cosine * 2.5`, which is enough to displace a real lexical hit.
#
# 0.35 is deliberately at the LOW end of the useful band. The semantic leg is
# what rescues the measured misspellings ('пейволл', 'forcast', 'screan_spot',
# 'onboarding quiz'), which land around 0.45-0.65 with the shipped embedding
# model, while unrelated text sits under 0.25. A higher floor would start
# cutting into the rescues this leg exists for, so the floor removes the flat
# tail and nothing else.
_SEMANTIC_MIN_COSINE = 0.35

# The score at which a result is presented as a certain answer (tripl-txcz).
# 5.0 (the boost for "the title IS what you typed") + 2.0 (a perfect trigram
# similarity on that same title) = the score of a document that is exactly the
# thing the user asked for. Confidence is that fraction, capped at 1.0 — an
# ABSOLUTE scale, so a query that matched nothing well cannot be dressed up as
# a perfect match just by being the best of a bad set.
#
# ONLY IDENTITY TIERS MAY REACH THIS LINE, ON EITHER DIALECT
# ----------------------------------------------------------
# An earlier version of this comment claimed the SQLite fallback's top three
# tiers "sit at or above the same line, so the two dialects agree on what
# 'certain' means". They did not: ``fallback_score`` paid a BARE
# ``title.startswith(query)`` exactly 7.0, so a prefix match — the weakest kind
# of evidence there is, ``s`` matching ``screen_spot`` — was served at
# confidence 1.0. That is the "asdkjhasd at 100%" defect this issue exists to
# remove, reintroduced on the dialect the entire backend suite runs on.
#
# The rule the two dialects share, and the one Postgres no longer honours: a
# result should reach 1.0 only when the document IS what was typed. On SQLite
# that still holds exactly — the only tiers at or above this constant are
# ``title == query`` and ``keywords == query``, and every partial-evidence tier
# in ``fallback_score`` sits strictly below it.
#
# ON POSTGRES IT IS NOW A STATEMENT ABOUT THE LADDER, NOT ABOUT THE SCORE
# ------------------------------------------------------------------------
# The intended reading was 5.0 exact-title (or 4.0 exact-keywords) plus a
# near-perfect trigram on the same short field. The SCORE has never been only
# those two terms: ``lexical_score * 4.0`` is in the same sum, so a document with
# a strong lexical leg and a mid ladder tier could already cross 7.0 without
# being what was typed (measured on the relevance corpus, BEFORE tripl-9t2s:
# ``screen_spot`` served at 1.0 for ``q='spot'`` on 7.64, and
# ``${property.spot_id}`` at 7.61).
#
# tripl-9t2s widens that: COVERAGE_BONUS adds up to 1.0 to every hit that
# answered the whole query, so ``${property.card_target}`` for
# ``q='screen_settings'`` moves 6.2985 -> 7.2985 and crosses the line too. The
# gap is real and it is tripl-txcz's bound eroding — but it is a CONFIDENCE
# defect with a confidence-shaped fix (raise this constant, or make confidence
# read the ladder tier rather than the total), not a reason to underpay coverage
# in the RANKING. TRACKED AS tripl-d5u8; it lived here as prose for four PRs
# without being filed, which is why nobody could schedule it.
_FULL_CONFIDENCE_SCORE = 7.0

# How much a semantic hit's cosine similarity is worth to the RANKING, i.e. how
# a vector-only hit is placed among lexical ones (tripl-txcz). It is not, and
# cannot be, the scale confidence is read on: it caps a perfect cosine at 2.5,
# which is a deliberate ranking statement ("a pure vector match ranks around a
# literal body-token match") and a nonsense certainty statement ("a perfect
# semantic match is 36% sure"). :func:`finalize_results` therefore scores the
# two legs on one scale and reports certainty on another; see both docstrings.
_SEMANTIC_SCORE_WEIGHT = 2.5

# The lowest boost-ladder tier that counts as "this document IS what was typed"
# (tripl-d5u8). The ladder's top two rungs are equality tests — 5.0 for
# ``lower(title) = lower(query)`` and 4.0 for ``lower(keywords) = lower(query)``
# — and every rung below them (3.5 word-boundary, 3.25 stemmed, 3.0 body token
# or title prefix, 2.25/1.5 substring) says only that the query APPEARS
# somewhere, which is partial evidence.
_IDENTITY_BOOST_MIN = 4.0

# What a result that did NOT match by identity may be reported at, at most
# (tripl-d5u8), regardless of whether lexical score or semantic cosine wins.
#
# WHY CONFIDENCE COULD NOT BE READ OFF THE SCORE ALONE
# ----------------------------------------------------
# ``_FULL_CONFIDENCE_SCORE`` was derived as 5.0 exact-title + 2.0 perfect
# trigram, but it is divided into a sum that also carries ``lexical_score * 4.0``
# and, since tripl-9t2s, ``COVERAGE_BONUS``. Neither was in the derivation, so
# documents that are not the thing named reached 1.0 anyway — measured on the
# relevance corpus: ``screen_spot`` at 7.64 for ``q='spot'``,
# ``${property.spot_id}`` at 7.61, and ``${property.card_target}`` crossing at
# 7.2985 for ``q='screen_settings'``. Raising the constant would have been a
# guess that the next term added to the sum invalidates again; asking the LADDER
# instead is stable, because the ladder is the only term that answers the actual
# question.
#
# WHY 0.80, AND WHY IT IS NOT A NEW NUMBER
# -----------------------------------------
# It is what the SQLite fallback has always produced for its strongest partial
# tier: ``_SQLITE_TITLE_PREFIX / _FULL_CONFIDENCE_SCORE`` is ``5.6 / 7.0``,
# exactly 0.80. That dialect already enforces this rule by construction — only
# ``title == query`` (8.0) and ``keywords == query`` (7.2) sit at or above the
# certainty line — so this ceiling does not change SQLite at all. It ports the
# guarantee to Postgres, which is the "make the two dialects agree" half of
# tripl-txcz that was never actually true on the dialect users search on.
#
# RANKING IS UNTOUCHED. This is applied in :func:`finalize_results` AFTER the
# sort, to the number painted on a result and never to its position.
_PARTIAL_CONFIDENCE_CEILING = 0.80


def sanitize_query(query: str) -> str:
    """Trim the incoming query and drop codepoints no backend can carry.

    Postgres ``text`` cannot represent U+0000, so a query containing one aborts
    inside the driver (``asyncpg.exceptions.CharacterNotInRepertoireError``)
    before any SQL runs — ``?q=%00``, the first thing a routine security scan
    sends, used to surface as a 500 for any authenticated caller (tripl-q4q7).

    We strip rather than reject with 422 because stripping is *lossless here*:
    an indexed document cannot contain a NUL either (same column type, same
    restriction), so removing it cannot change which rows match or how they
    rank. The user who pasted text with a stray NUL gets the results they meant
    and no API client loses behaviour it could have relied on — the surprise
    argument against silently rewriting a query has no bite when the rewrite is
    provably a no-op on the result set. That reasoning does NOT generalise:
    anything with search meaning must survive, which is why this removes
    exactly U+0000 and leaves every other character alone.

    A query made only of NULs collapses to ``""`` and then takes the same
    empty-result path a whitespace-only query already took.

    The NUL removal itself now lives in ``schemas.text_filters``, where a route
    parameter type applies the identical rule to every other free-text filter
    (tripl-8wez). One rule, one implementation; this function adds only the
    whitespace trim, which is a search concern rather than a driver one.
    """
    cleaned: str = strip_nul_bytes(query)
    return cleaned.strip()


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _normalized_spans(value: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize text while retaining each character's raw source span."""
    normalized: list[str] = []
    spans: list[tuple[int, int]] = []
    whitespace_start: int | None = None
    for index, char in enumerate(value):
        if char.isspace():
            if normalized and whitespace_start is None:
                whitespace_start = index
            continue
        if whitespace_start is not None:
            normalized.append(" ")
            spans.append((whitespace_start, index))
            whitespace_start = None
        for folded in char.casefold():
            normalized.append(folded)
            spans.append((index, index + 1))
    return "".join(normalized), spans


def _tokens(query: str) -> list[str]:
    return [token for token in re.split(r"\s+", _normalize(query)) if token]


def _safe_limit(limit: int) -> int:
    return max(1, min(limit, 10000))


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind.dialect.name == "postgresql"


async def postgres_search(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    query: str,
    entity_types: list[SearchEntityType] | None,
    include_archived: bool,
    limit: int,
    project_is_demo: bool = False,
    semantic: bool = True,
) -> tuple[list[SearchResult], bool]:
    semantic_used = False
    semantic_results: list[SearchResult] = []
    # The request organization's vector space (F20 PR10): its provider for the
    # query vector, its provenance for the stored rows it may be compared with.
    ai_config = await app_settings_service.get_search_embedding_config(
        session, project_id=project_id
    )

    # Resolve the query vector, then run the leg once. Two sources, in priority
    # order: the live embedding provider, and — only when that is unavailable
    # (embeddings disabled, or the embed call failed / returned empty) — a canned
    # vector from the demo project's precomputed fixture. Non-demo projects never
    # reach the second source.
    # ``semantic=False`` is the caller declining the vector leg outright: no
    # provider round trip, no demo fixture, lexical rows only. The palette asks
    # for this first and upgrades to the full answer when it lands, because on
    # production the embedding call was the whole difference between a result
    # list at ~150 ms and one at ~1.5 s (tripl-kjhi.15).
    embedding: list[float] | None = None
    is_semantic_eligible = semantic and len(query) >= 3
    lexical = postgres_lexical_search(
        session,
        project_id=project_id,
        branch_id=branch_id,
        query=query,
        entity_types=entity_types,
        include_archived=include_archived,
        limit=limit,
    )
    # The provider round trip overlaps the lexical query rather than queueing
    # behind it (tripl-2x5d). ``embed_query`` is a blocking HTTP POST handed to a
    # thread and the lexical leg is SQL on this session; the thread touches
    # neither the session nor the lexical rows, so awaiting them in sequence made
    # every semantic search cost the SUM of two round trips on a read path
    # measured at over 2.2s. Both legs still receive exactly the inputs they
    # received before and ``merge_results`` is handed the same two lists, so this
    # moves WHEN the vector is fetched and nothing about the ranking.
    #
    # The two legs fail independently (tripl-l33u), and awaiting the lexical one
    # DIRECTLY is what buys that without a gather. An embed failure is simply "no
    # embedding" — the same state an empty provider response produces — so it is
    # logged and the fallback below still runs. A lexical failure is the request
    # failing, and it must surface at the speed it happened: the embed leg is a
    # blocking HTTP POST with its own short query timeout, so waiting for it to
    # settle before re-raising (what ``gather(..., return_exceptions=True)`` does)
    # would still hold an immediate SQL error hostage to a stalled provider.
    # Cancelling the embed task cannot strand SQL on this request's AsyncSession,
    # because the thread it wraps touches neither the session nor the lexical
    # rows; the await after the cancel exists to retrieve an exception from a
    # task that had already finished, which asyncio would otherwise log at GC.
    if is_semantic_eligible and ai_config.search_embeddings_enabled:
        embed_leg = asyncio.ensure_future(
            asyncio.to_thread(embed_query, query, config=ai_config, timeout=3)
        )
        try:
            lexical_results = await lexical
        except BaseException:
            embed_leg.cancel()
            with contextlib.suppress(BaseException):
                await embed_leg
            raise
        try:
            embedding = await embed_leg
        except Exception:
            logger.warning("Search embedding leg failed; continuing without it", exc_info=True)
    else:
        lexical_results = await lexical
    fixture_vector = False
    if is_semantic_eligible and not embedding and project_is_demo:
        embedding = demo_query_embedding(query)
        fixture_vector = embedding is not None

    if embedding:
        embedding = sanitize_embedding(embedding)

    if embedding:
        # Canned demo queries must only compare with the fixture's own vectors;
        # live queries must only compare with rows from the current provider.
        fixture = load_demo_embedding_fixture() if fixture_vector else None
        model = fixture.model if fixture is not None else embedding_provenance(ai_config)
        semantic_used = True
        semantic_results = await postgres_semantic_search(
            session,
            project_id=project_id,
            branch_id=branch_id,
            embedding=embedding,
            embedding_model=model,
            entity_types=entity_types,
            include_archived=include_archived,
            limit=limit,
        )

    merged = merge_results(lexical_results, semantic_results, limit)
    return merged, semantic_used


#: The tsquery every lexical search runs with: the STEMMED reading of what was
#: typed, OR-ed with its SURFACE reading (tripl-uojz). ``||`` on ``tsquery`` is
#: OR, not concatenation.
#:
#: WHY THIS IS A MODULE CONSTANT AND NOT JUST A LINE OF SQL
#: --------------------------------------------------------
#: It is the QUERY half of an identity the whole fix rests on: a document
#: indexes ``{stem(w), surface(w)}`` (``search_service.TEXT_VECTOR_EXPRESSION``)
#: and a query asks for ``{stem(q), surface(q)}``, so two forms of one word meet
#: iff those sets intersect. Half of that identity being right is worth nothing —
#: a surface-indexed document under a stem-only query is unreachable in exactly
#: the way the bug was.
#:
#: ``tests/relevance/test_stemming_invariants.py`` asserts the identity directly,
#: over production-harvested word forms, with no corpus and no ranking involved.
#: It IMPORTS this constant and ``TEXT_VECTOR_EXPRESSION`` rather than copying
#: the SQL, so the thing it proves is the thing that ships. A copied literal
#: would have gone on passing after either side was edited — which is precisely
#: how the previous harness certified a repair that was wrong on production.
TEXT_QUERY_EXPRESSION = """
    websearch_to_tsquery('tripl_search', :query)
    || websearch_to_tsquery('tripl_search_surface', :query)
"""


#: What a document is paid for having answered the WHOLE query (tripl-9t2s).
#:
#: Public, and a module constant, for the same reason :data:`TEXT_QUERY_EXPRESSION`
#: is: it is interpolated into shipped SQL and imported by the tests that bound it
#: from above and below, so those tests assert the value that ships rather than a
#: copy of it.
#:
#: WHY A COVERAGE TERM EXISTS AT ALL
#: ---------------------------------
#: ``ts_rank_cd`` pays for cover DENSITY, the ladder below pays for match SHAPE
#: (where in the document the query appears), and the trigram leg pays for string
#: OVERLAP. Measured, none of them pays for having answered the whole query, and
#: the gap is user-visible: for ``q='экран спота'`` the ``Экран`` FIELD document —
#: whose entire title is ONE of the two words, which matches no tsquery at all and
#: earns no ladder tier — scored 1.0000 on ``2.0 x similarity 0.5`` alone, while
#: the ``screen_spot`` event, whose description is literally ``Показ экрана
#: спота`` and which therefore matched BOTH words, scored 0.8294.
#:
#: WHY A BOOLEAN OVER ``@@`` IS THE WHOLE COVERAGE SIGNAL, AND COSTS NOTHING
#: ------------------------------------------------------------------------
#: ``websearch_to_tsquery`` ANDs the terms WITHIN each leg, so
#: :data:`TEXT_QUERY_EXPRESSION` is ``(a1 & b1) | (a2 & b2)`` and
#: ``d.text_vector @@ q.tsq`` is ALREADY the "matched every term" predicate — it
#: does not need the query's term count, and it is a per-DOCUMENT test that adds
#: no per-row work: the identical expression is evaluated in the WHERE below, so
#: the projection re-reads a predicate this row has already been through.
#: A genuine matched/total ratio would need ``numnode`` plus one ``@@`` probe per
#: term against every candidate tsvector, i.e. O(terms) extra probes per row on a
#: path the command palette fires on every debounced keystroke. The boolean buys
#: the measured fault for free; the ratio would buy a distinction no measured
#: query makes at a cost every query pays.
#:
#: WHY 1.0, HONESTLY — A BOUND ON THE MEASURED SAMPLE AND NOT A GENERAL ONE
#: -----------------------------------------------------------------------
#: * FROM BELOW: it clears the whole score a bare one-word title can earn on the
#:   MEASURED shape. ``similarity('Экран', 'экран спота')`` is exactly 0.5 (6
#:   trigrams of 12, all shared), so that document's entire score is
#:   ``2.0 x 0.5 = 1.0`` with a lexical leg and a boost of 0. Paying a complete
#:   match 1.0 therefore brings it LEVEL with that document — level, not past it.
#:   A complete match whose own lexical leg really is ~0 lands on the same
#:   1.0000 and the outcome falls to ``ORDER BY score DESC, title``, which is a
#:   tiebreak rather than a guarantee. What wins the MEASURED case is that the
#:   complete match is not weak: screen_spot carries 0.8294 of its own and
#:   finishes at 1.8294. So do not read this bullet as "coverage outranks a
#:   partial title" — read it as "coverage stops a partial title winning for
#:   free". That is a bound on THIS sample and this constant does not pretend
#:   otherwise: stopwords are not filtered, so ``q='the spot'`` gives a one-word
#:   title 5 of 9 trigrams — ``2.0 x 0.556 = 1.11`` — and it would still beat a
#:   complete match with a weak lexical leg. The term narrows the fault; it does
#:   not close the class.
#: * FROM ABOVE: a complete-but-weak match must not be served as a CERTAIN answer.
#:   Confidence is ``score / _FULL_CONFIDENCE_SCORE`` (7.0), and the harness pins
#:   the russian-phrase case at ``max_top_confidence=0.5``, which starts biting
#:   near 2.7. 1.0 leaves that bound with room (1.8294 / 7.0 = 0.261).
#: * IT IS A CONSTANT, and must stay one. Anything that grew with term frequency,
#:   cover count or document length would re-open tripl-gbxj through a second
#:   door — the whole point of normalization flag 32 is that no text-search leg
#:   scales with how often a document repeats itself.
#: * IT IS NOT A LADDER TIER. ``CASE`` is first-match-wins and encodes WHERE the
#:   query appears; coverage encodes HOW MUCH of it was answered. As a tier it
#:   would REPLACE a document's shape score instead of adding to it, and an
#:   exact-title match (5.0) would be paid nothing for also being complete.
#: * The ``ELSE`` branch also absorbs a NULL ``text_vector`` (a document indexed
#:   before its vector was built): ``NULL @@ tsq`` is NULL, so the CASE falls
#:   through to 0.0 rather than making the whole score NULL.
#:
#: WHAT IT DOES TO THE LEG BOUNDS, WITHOUT THE FALSE INEQUALITY
#: -----------------------------------------------------------
#: Flag 32 (``rank/(rank+1)``) bounds the lexical leg at 4.0 and that is unchanged
#: — the coverage term is not scaled by the rank, so it cannot be inflated by
#: repetition. What DOES change is the total a text-search match can pay: 4.0
#: becomes 5.0, which now EQUALS rather than sits below the 5.0 an exact title
#: earns. It is stated that way deliberately, because the tempting sentence ("the
#: text-search legs stay strictly below 5.0") is already false in this file with or
#: without this term: ``fuzzy_score * 2.0`` is in the same sum, so lexical+fuzzy
#: could reach 6.0 before this change and 7.0 after it.
COVERAGE_BONUS = 1.0


#: The boost ladder, as a constant so a test can assert the SHIPPED expression.
#:
#: WHY IT IS OUT HERE AND NOT INLINE (tripl-0qld)
#: The ladder is where "which text counts as what" turns into a number, and two
#: of its tiers read the same ``keywords`` column. Whether a harvested value can
#: buy a document the 3.5 tier is therefore a question with an exact answer, and
#: it was previously only askable by ranking a corpus and reading a total score
#: — a number the ladder, the trigram leg and ``ts_rank_cd`` all contribute to,
#: so a tier inversion could hide inside a green case. It did:
#: ``tests/relevance/cases.purchase-plural`` passed while three unrelated events
#: outranked ``purchase_completed`` ON THE LADDER, rescued by the trigram leg.
#: ``tests/relevance/test_keyword_tier_premise.py`` imports this and reads the
#: boost column alone.
#:
#: It expects the same bind parameters as :func:`postgres_lexical_search`
#: (``:query``, ``:has_token_regex``, ``:token_regex``, ``:prefix``,
#: ``:contains``) and a ``q.tsq`` in scope — i.e. it is only meaningful joined
#: against the ``q`` CTE built from :data:`TEXT_QUERY_EXPRESSION`.
BOOST_LADDER_EXPRESSION = """
                CASE
                    WHEN lower(d.title) = lower(:query) THEN 5.0
                    WHEN lower(d.keywords) = lower(:query) THEN 4.0
                    -- 3.5 READS THE SAME COLUMN AS 3.25 AND RESTS ON THE SAME
                    -- PREMISE (tripl-0qld): `keywords` is identity text, not the
                    -- stream of values a user's app emitted. When that stopped
                    -- being true for events, BOTH tiers paid for text nobody
                    -- wrote -- so the two move together or not at all, and the
                    -- docstring's 3.25 bullet is the statement of record for
                    -- both.
                    WHEN :has_token_regex AND d.keywords ~* :token_regex THEN 3.5
                    -- The stemmed tier (tripl-nh5s). Every other tier compares
                    -- literal characters, so a plural query can only ever reach
                    -- the ladder through a document that spells the plural --
                    -- which is the harvested value, not the entity. See the
                    -- docstring: this fires only on title/keywords, never body.
                    -- DERIVED FROM THE `q` CTE ABOVE, NOT A SECOND CONSTRUCTION
                    -- SITE: it consumes `q.tsq` and only builds the DOCUMENT
                    -- side. That document side has to carry the same two legs
                    -- as the stored text_vector (tripl-uojz) or the tier would
                    -- answer a two-leg query with a one-leg document and fire
                    -- for a strictly narrower set than it retrieves.
                    WHEN (
                        to_tsvector('tripl_search', concat_ws(' ', d.title, d.keywords))
                        || to_tsvector(
                            'tripl_search_surface', concat_ws(' ', d.title, d.keywords)
                        )
                    ) @@ q.tsq THEN 3.25
                    WHEN :has_token_regex AND d.body ~* :token_regex THEN 3.0
                    WHEN lower(d.title) LIKE lower(:prefix) THEN 3.0
                    WHEN lower(d.body) LIKE lower(:contains) THEN 2.25
                    WHEN lower(d.keywords) LIKE lower(:contains) THEN 1.5
                    ELSE 0.0
                END"""


async def postgres_lexical_search(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    query: str,
    entity_types: list[SearchEntityType] | None,
    include_archived: bool,
    limit: int,
) -> list[SearchResult]:
    """Rank one branch's documents: ts_rank_cd + trigram similarity + boost ladder.

    The three legs are meant to be on comparable scales: the boost ladder is a
    set of fixed constants (5.0 for "the title IS the query" down to 1.5), the
    trigram leg is a similarity in [0, 1] weighted x2.0, and the lexical leg is
    a ``ts_rank_cd`` weighted x4.0. That only holds if ``ts_rank_cd`` is
    bounded, and until tripl-gbxj it was called with NO normalization flag —
    i.e. as a raw sum over occurrences, which grows without limit as a document
    repeats a term.

    WHAT THAT COST (measured, 26 queries over three real projects)
    -------------------------------------------------------------
    ``q='spot'`` returned ``${property.spot_id}`` at 73.69 and ``${property.cube}``
    at 55.68 — the latter only because one harvested value is ``spot:reload:bento``
    — while the events actually NAMED ``spot`` and ``screen_spot`` did not appear
    at all. A harvested-value variable that repeats a token ~180 times reached a
    lexical leg of ~68: an order of magnitude more than the entire boost ladder,
    so no exact-title constant could ever catch up. ``q='screen_spot'`` fired the
    5.0 exact-title boost and the event still ranked 3rd.

    WHY FLAG 32 AND NOT 2
    ---------------------
    ``32`` is ``rank / (rank + 1)``: a monotone squash of [0, inf) onto [0, 1),
    so the lexical leg can never exceed 4.0 — below the 5.0 an exact title
    scores — while the NORMAL range is left almost untouched. A single ordinary
    match has a raw ``ts_rank_cd`` around 0.1 and still scores 0.09; the 18.4
    outlier above collapses to 0.95. It bends the outliers and nothing else,
    which is exactly the shape of the measured fault.

    ``2`` (divide by document length) was considered and rejected. It turns the
    score into a term DENSITY, but length in this index is an artifact of how
    many contexts an entity happens to be bound to rather than a property of the
    text, so an ordinary match lands near 0.0007 — three orders of magnitude
    below the boost ladder. That does not stop the lexical leg from dominating,
    it stops it from discriminating at all, and every ranking decision would
    fall to the ladder and the trigram leg.

    Normalization is only half of tripl-gbxj: harvested values are no longer
    joined into a variable's ``keywords`` either (see
    ``_search_documents._variable_document``), which removes the duplication
    that made those documents long in the first place. The measured 73.69 vs
    4.55 gap was the two compounding, so both landed together.

    ``token_boundary_regex`` below carries the query-time half of tripl-h9x2: a
    spaced query is folded into its underscored form, so the 3.5/3.0
    word-boundary tiers are reachable for ``q='screen spot'`` instead of being
    dead for every multi-word query.

    THE 3.25 TIER: THE BOOST LADDER HAD TO LEARN THE STEMMER TOO (tripl-nh5s)
    ------------------------------------------------------------------------
    Migration ``a7c3e1b9d5f2`` gives ``tripl_search`` an English and a Russian
    stemmer, which fixes RETRIEVAL: ``q='purchases'`` matches the
    ``purchase_completed`` event's tsvector at all, for the first time. It does
    NOT by itself fix the RANKING, because every tier of the ladder is a literal
    comparison
    (``=``, ``LIKE``, a regex), and the ladder is worth up to 5.0 against a
    lexical leg capped at 4.0. So a plural query kept losing to the same
    documents it lost to before:

        ``q='purchases'``: the ``purchase_completed`` event spells the SINGULAR
        everywhere, so no literal tier can fire for it and its boost is 0. The
        harvested-value variable ``${property.screen_name}`` literally contains
        the string ``purchases`` in its body, so ``body ~* '\\mpurchases\\M'``
        fires and pays it 3.0 — more than the entire stemming gain. Same shape
        for ``q='уловы'`` and ``q='spots'``.

    The 3.25 tier is the missing half: it asks whether the query, AFTER
    stemming, matches this document's own identity text. Three properties are
    load-bearing:

    * **title and keywords only, never body.** ``keywords`` carries an entity's
      IDENTITY — its name, that name's spaced alias, its type, its tags, the
      values a person authored into its spec, and its bindings. It carries no
      OBSERVED VALUE, for any entity type: tripl-gbxj took them out of a
      variable's keywords and tripl-0qld took them out of an event's, which is
      the same rule stated twice because it was implemented once. ``body`` is
      where the harvested text lives, and paying a stemmed body match would hand
      the noise documents the tier a second time.

      NOT "curated", and the difference is load-bearing. Two scan-written
      strings still reach this column — ``value_kind`` and ``observed_count``,
      from ``_search_documents._variable_context_text`` under
      ``include_values=False`` — and an auto-detected variable's own NAME was
      derived by the scanner rather than typed by anyone. What the tier can
      honestly rest on is that ``keywords`` is identity-and-binding text of
      bounded size, never the unbounded stream of values a user's app emitted.
      This bullet used to claim curation outright while ``_event_document``
      joined every harvested value into the column; do not restore the shorter
      sentence without first making it true.
    * **3.25, between the 3.5 literal-keyword-token tier and the 3.0 literal
      body-token tier.** A stemmed match on the entity's name is weaker evidence
      than the literal token appearing in its keywords, and stronger than the
      literal token appearing somewhere in its body. Ordered that way, the
      measured pairs invert: ``purchase_completed`` reaches 3.25 + rank + trigram
      while the harvested variable stays at its 3.0.
    * **No document's own boost can FALL because of it.** ``CASE`` returns the
      first branch that matches, so a document that already qualified for 5.0,
      4.0 or 3.5 never reaches this tier, and any document that does reach it
      was going to score 3.0 or less. The tier therefore adds evidence and never
      removes it: a result changes position only by being overtaken by a
      document whose name genuinely stems to what was typed, which is the whole
      point of the change.

    The cost is one ``to_tsvector`` per candidate row over two short columns.
    It is computed in the projection of the ``ranked`` CTE, i.e. only for rows
    that already passed the WHERE, and never over ``body`` — the column that can
    be megabytes on a harvested-value variable.

    THE QUERY IS NOW TWO TSQUERIES OR-ED (tripl-uojz)
    -------------------------------------------------
    Snowball over-stems the bare nominative — ``улов`` -> ``ул``, ``экран`` ->
    ``экра`` — onto a lexeme no inflected form of the same word reaches, so a
    stem-only index splits a word into two disjoint classes. The repair is
    symmetric: every document indexes ``stem(w) || surface(w)`` (see
    ``search_service.TEXT_VECTOR_EXPRESSION``) and every query is
    ``websearch_to_tsquery('tripl_search', q) || websearch_to_tsquery(
    'tripl_search_surface', q)``. There is exactly ONE place the tsquery is
    built — the ``q`` CTE — and exactly one place that derives from it and needs
    the same treatment on the DOCUMENT side, the 3.25 tier above; the WHERE and
    ``ts_rank_cd`` consume ``q.tsq`` unchanged.

    WHAT IT DOES TO RANKING, AND WHERE THE MARGIN IS THIN
    ------------------------------------------------------
    A document holds roughly twice the lexeme occurrences, so a raw
    ``ts_rank_cd`` roughly doubles — but not uniformly.

    * **The tripl-gbxj guarantees hold by construction.** Flag 32 bounds the
      lexical leg at 4.0 however large the raw rank grows, so the cases won by a
      5.0 exact-title boost cannot be caught by a leg that cannot reach 5.0. For
      ``q='spot'`` and ``q='screen_spot'`` the two legs are the same lexemes
      anyway (``stem('spot') == 'spot'``), so the query is literally unchanged.
    * **The repetition outlier gains less than double.** A tsvector caps a
      lexeme at 256 positions, so the 180 occurrences of ``screen`` in
      ``q='screen_settings'``'s outlier merge to 256 rather than 360.
    * **The plural cases have the least room of any case here.**
      ``purchase-plural`` / ``spots-plural`` / ``ulov-plural`` are won by 0.25 —
      the gap between the 3.25 stemmed tier the entity earns and the 3.0 literal
      body tier the harvested value earns — and the harvested value spells the
      queried form LITERALLY, so it matches both legs while the correctly-named
      entity matches only the stem leg. Re-measure these three first after
      touching any weight.

    All of it is executed only by ``tests/relevance/``: SQLite has no stemmer, so
    a green backend suite says nothing about any of it (see
    :func:`fallback_score`).

    A second, smaller semantic change: ``websearch_to_tsquery`` ANDs the terms
    WITHIN each leg, so the result is ``(a1 & b1) | (a2 & b2)`` and not the
    per-term ``(a1|a2) & (b1|b2)``. A multi-word query whose terms need DIFFERENT
    legs (one word matched by its stem, another only by its surface) still
    misses. That is strictly less broken than today, where only the stem leg
    exists, but it is not zero. Building the per-term form would mean parsing
    websearch syntax — quotes, ``or``, ``-`` negation — in Python, and a
    hand-rolled parser of that grammar is a larger risk than the residue.

    The same OR also weakens ``-`` exclusion: excluding ``-спота`` no longer
    excludes a document spelling ``спот``, because that document fails the stem
    leg's ``!спот`` but satisfies the surface leg's ``!спота``. Detecting
    negation in Python to skip the surface leg was rejected — a bare ``-`` scan
    misfires on the hyphenated identifiers this catalog is full of, and a
    heuristic that is wrong on identifiers is worse than a narrower NOT.

    NOTHING PAID FOR ANSWERING MORE OF THE QUERY (tripl-9t2s)
    ---------------------------------------------------------
    Three legs, three different things measured, and none of them coverage:
    ``ts_rank_cd`` pays for cover DENSITY, the ladder pays for match SHAPE, the
    trigram leg pays for string OVERLAP. So a document holding ONE of two query
    words, in a title short enough for that word to dominate its trigram set,
    beat a document that matched both.

    MEASURED ON THIS HARNESS, ``q='экран спота'`` (three documents, whole set)::

        before   Экран 1.0000   screen_spot 0.8294   spot 0.4799
        after    screen_spot 1.8294   spot 1.4799   Экран 1.0000

    ``Экран`` is the ``view_id`` FIELD document. It holds ``экра``/``экран`` and
    no Cyrillic ``спот``, so ``d.text_vector @@ q.tsq`` is FALSE, no ladder tier
    fires (the title is not the query, ``\\mэкран_спота\\M`` misses, the 3.25 tier
    reads title+keywords and misses too) and its ENTIRE score is the trigram leg:
    ``similarity('Экран','экран спота')`` is exactly 0.5 — 6 trigrams of 12, all
    shared — so ``2.0 x 0.5 = 1.0000``, to the digit. ``screen_spot`` is the event
    the user wants: its description is literally ``Показ экрана спота``, which is
    in ``body``, so it satisfies both terms of the stem leg and is the only
    document here that answered the WHOLE query — and its boost is 0 as well,
    because the 3.25 tier deliberately never reads ``body``. Its whole score was
    the lexical leg. The complete match lost to the partial one by 0.17.

    :data:`COVERAGE_BONUS` is what closes that, and its docstring carries the
    justification for the value, the cost argument, and the bound it does NOT
    provide. Three things about it belong here, next to the SQL:

    * **Flag 32 is untouched.** The coverage term is not rank-scaled, so the
      lexical leg is still bounded at 4.0; what moves is the TOTAL a text-search
      match can pay, 4.0 -> 5.0. See :data:`COVERAGE_BONUS`, "WHAT IT DOES TO THE
      LEG BOUNDS, WITHOUT THE FALSE INEQUALITY", for why that is deliberately not
      phrased as an inequality against the exact-title boost.
    * **It is now uniform in the result as well as in the SQL.** It was not,
      when this was written: ``_apply_event_type_boost`` ran after this query and
      was MULTIPLICATIVE, so an event whose type matched banked ``1.75 x`` the
      bonus while a sibling field or variable banked ``1.0 x`` — measured on the
      harness, ``q='улов'`` moved ``catch_report_created`` by +1.75 and ``Тип
      улова`` by +1.00. That boost was deleted (tripl-0tt4 item 4, see
      :func:`finalize_results`), and it was the only multiplicative step between
      this SQL and the final order. Every term downstream of here is additive,
      so a constant added to two documents cannot reorder them against each
      other any more.
    * **The residue is confidence, not ranking.** Confidence is
      ``score / _FULL_CONFIDENCE_SCORE``, so every tsquery-matching hit's reported
      certainty rises by up to 0.143 and some non-identity documents cross 1.0
      that did not before (measured: ``${property.card_target}`` for
      ``q='screen_settings'``, 6.2985 -> 7.2985). That erosion is pre-existing
      rather than introduced — ``screen_spot`` was already served at 1.0 for
      ``q='spot'`` on 7.64 — but it is wider now, and tripl-txcz's bound is worth
      re-tightening on its own terms rather than by shrinking this constant.
      A query that matches NOTHING is unaffected by construction: the term is
      gated on the same ``@@`` that is in the WHERE, and for ``q='asdkjhasd'`` no
      document satisfies it, so ``garbage-query-is-not-a-confident-answer`` moves
      by exactly 0.0000.
    """
    token_regex = token_boundary_regex(query)
    has_token_regex = bool(token_regex)
    statement = text(
        f"""
        WITH q AS (
            -- THE ONE TSQUERY CONSTRUCTION SITE (tripl-uojz). The expression
            -- itself lives in TEXT_QUERY_EXPRESSION above so the invariant test
            -- can assert the SHIPPED string instead of a copy of it; the stem
            -- leg is what a7c3e1b9d5f2 built, and the surface leg is what lets a
            -- query reach a document stranded on an over-stem (`экран` -> `экра`,
            -- a lexeme no inflected form produces). Both legs are also indexed
            -- into every document's text_vector, so the two sides meet.
            SELECT ({TEXT_QUERY_EXPRESSION}) AS tsq
        ),
        ranked AS (
            SELECT
                d.id,
                d.entity_type,
                d.entity_id,
                d.parent_event_id,
                d.title,
                d.subtitle,
                d.description,
                d.body,
                d.keywords,
                d.route_path,
                -- Normalization 32 == rank/(rank+1): bounded, so raw term
                -- frequency can no longer outweigh the boost ladder (tripl-gbxj,
                -- see the docstring for why not 2).
                COALESCE(ts_rank_cd(d.text_vector, q.tsq, 32), 0.0) AS lexical_score,
                GREATEST(
                    similarity(d.title, :query),
                    similarity(d.subtitle, :query),
                    similarity(d.keywords, :query),
                    similarity(d.body, :query) * 0.5
                ) AS fuzzy_score,
                {BOOST_LADDER_EXPRESSION} AS boost,
                -- COVERAGE, NOT SHAPE (tripl-9t2s). `@@` is the "answered every
                -- term" predicate, because websearch_to_tsquery ANDs within each
                -- leg -- so this is what stops a short almost-exact title from
                -- beating a document that matched the WHOLE query. Additive and
                -- outside the CASE on purpose: the ladder is first-match-wins and
                -- says WHERE the query appears, this says HOW MUCH was answered.
                -- The same predicate is already in the WHERE below, so this row
                -- has been through it once already. See COVERAGE_BONUS for why
                -- 1.0, for what that bound rests on, and for the NULL vector.
                CASE
                    WHEN d.text_vector @@ q.tsq THEN {COVERAGE_BONUS}
                    ELSE 0.0
                END AS coverage_score
            FROM search_documents d
            CROSS JOIN q
            WHERE d.project_id = :project_id
              AND d.branch_id = :branch_id
              AND (:include_archived OR d.archived IS FALSE)
              AND (:filter_entity_types IS FALSE OR d.entity_type IN :entity_types)
              AND (
                d.text_vector @@ q.tsq
                OR d.title % :query
                OR d.subtitle % :query
                OR d.keywords % :query
                OR lower(concat_ws(' ', d.title, d.subtitle, d.body, d.keywords))
                   LIKE lower(:contains)
              )
        )
        SELECT
            id,
            entity_type,
            entity_id,
            parent_event_id,
            title,
            subtitle,
            description,
            body,
            keywords,
            route_path,
            -- Projected as well as summed (tripl-d5u8): the ladder tier is the
            -- only term in this sum that says WHETHER the document is the thing
            -- named, and confidence needs that separately from the total. See
            -- :data:`_IDENTITY_BOOST_MIN`.
            boost,
            (
                (lexical_score * 4.0)
                + (fuzzy_score * 2.0)
                + boost
                + coverage_score
            ) AS score
        FROM ranked
        ORDER BY score DESC, title ASC
        LIMIT :limit
        """  # noqa: S608 - no user input is interpolated; the operand is a module constant
    )
    filter_entity_types = bool(entity_types)
    statement = statement.bindparams(bindparam("entity_types", expanding=True))
    params: dict[str, object] = {
        "project_id": project_id,
        "branch_id": branch_id,
        "query": query,
        "prefix": f"{query}%",
        "contains": f"%{query}%",
        "token_regex": token_regex,
        "has_token_regex": has_token_regex,
        "include_archived": include_archived,
        "filter_entity_types": filter_entity_types,
        # An expanding bindparam cannot expand an empty list, so feed a single
        # placeholder when no type filter is active — the boolean flag above
        # short-circuits the IN clause, so the placeholder is never compared.
        "entity_types": list(entity_types) if entity_types else [""],
        "limit": limit,
    }
    rows = (await session.execute(statement, params)).mappings().all()
    return [row_to_result(row, query, semantic_used=False) for row in rows]


async def postgres_semantic_search(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    embedding: list[float],
    embedding_model: str,
    entity_types: list[SearchEntityType] | None,
    include_archived: bool,
    limit: int,
) -> list[SearchResult]:
    """Nearest documents by cosine similarity, above a floor (tripl-txcz).

    This leg is a pure ``ORDER BY <=> LIMIT``: it has no notion of "no good
    answer". Every indexed document carries an embedding, so it always returned
    exactly ``limit`` rows for ANY query, and ``merge_results`` then paid each
    of them ``cosine * 2.5`` — enough for the nearest junk row to be served as a
    result, and (before the same issue's confidence fix) as a CERTAIN one.

    ``_SEMANTIC_MIN_COSINE`` is applied in SQL rather than after the fetch so
    the floor cannot be defeated by the ``LIMIT``: filtering in Python would
    still have had the k nearest rows chosen first and then thrown most of them
    away, returning fewer results than requested for no reason.

    This deliberately does NOT de-weight or disable the semantic leg: the floor
    only removes the tail below the point where cosine stops carrying meaning,
    and the misspellings it rescues are listed on :data:`_SEMANTIC_MIN_COSINE`
    with the cosines they land at.

    This function does not apply ``_SEMANTIC_SCORE_WEIGHT`` — :func:`merge_results`
    does, and why that weight is a ranking number rather than a certainty is
    argued once, on :func:`finalize_results`.
    """
    vector = "[" + ",".join(f"{value:.8f}" for value in embedding) + "]"
    statement = text(
        """
        SELECT
            d.id,
            d.entity_type,
            d.entity_id,
            d.parent_event_id,
            d.title,
            d.subtitle,
            d.description,
            d.body,
            d.keywords,
            d.route_path,
            (1.0 - (d.embedding <=> CAST(:embedding AS vector))) AS score
        FROM search_documents d
        WHERE d.project_id = :project_id
          AND d.branch_id = :branch_id
          AND (:include_archived OR d.archived IS FALSE)
          AND d.embedding IS NOT NULL
          AND d.embedding_status = 'ready'
          AND d.embedding_model = :embedding_model
          AND (:filter_entity_types IS FALSE OR d.entity_type IN :entity_types)
          -- A nearest neighbour is not automatically a match (tripl-txcz).
          AND (1.0 - (d.embedding <=> CAST(:embedding AS vector))) >= :min_cosine
        ORDER BY d.embedding <=> CAST(:embedding AS vector)
        LIMIT :limit
        """
    )
    filter_entity_types = bool(entity_types)
    statement = statement.bindparams(bindparam("entity_types", expanding=True))
    params: dict[str, object] = {
        "project_id": project_id,
        "branch_id": branch_id,
        "embedding": vector,
        "embedding_model": embedding_model,
        "min_cosine": _SEMANTIC_MIN_COSINE,
        "include_archived": include_archived,
        "filter_entity_types": filter_entity_types,
        # See postgres_lexical_search: a placeholder list keeps the expanding
        # bindparam valid; the boolean flag short-circuits the IN clause.
        "entity_types": list(entity_types) if entity_types else [""],
        "limit": limit,
    }
    rows = (await session.execute(statement, params)).mappings().all()
    return [row_to_result(row, "", semantic_used=True) for row in rows]


async def sqlite_search(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    query: str,
    entity_types: list[SearchEntityType] | None,
    include_archived: bool,
    limit: int,
) -> list[SearchResult]:
    statement = select(SearchDocument).where(
        SearchDocument.project_id == project_id,
        SearchDocument.branch_id == branch_id,
    )
    if entity_types:
        statement = statement.where(SearchDocument.entity_type.in_(entity_types))
    if not include_archived:
        statement = statement.where(SearchDocument.archived.is_(False))
    rows = list((await session.execute(statement)).scalars().all())
    scored: list[tuple[float, SearchDocument]] = []
    query_norm = _normalize(query)
    query_tokens = _tokens(query)
    for row in rows:
        haystack = _normalize(_join_str([row.title, row.subtitle, row.body, row.keywords]))
        score = fallback_score(query_norm, query_tokens, haystack, row)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1].title))
    return [
        document_to_result(row, score, query, semantic_used=False) for score, row in scored[:limit]
    ]


def _join_str(parts: list[str | None]) -> str:
    return " ".join(p for p in parts if p)


# The SQLite fallback's tier ladder. Values are on the same absolute scale as
# the Postgres score, so ``finalize_results`` can divide both by
# _FULL_CONFIDENCE_SCORE and mean the same thing. See :func:`fallback_score` for
# why every tier is the old value x 0.8, and for what that does and does not
# make the two dialects agree about.
_SQLITE_EXACT_TITLE = 8.0
_SQLITE_EXACT_KEYWORDS = 7.2
_SQLITE_TITLE_PREFIX = 5.6
_SQLITE_KEYWORD_TOKEN = 5.44
_SQLITE_BODY_TOKEN = 5.2
_SQLITE_TITLE_OR_KEYWORD_SUBSTRING = 4.8
_SQLITE_HAYSTACK_SUBSTRING = 3.2
_SQLITE_ALL_TOKENS = 2.4
_SQLITE_FUZZY_WEIGHT = 1.6
_SQLITE_FUZZY_MIN_RATIO = 0.55


def fallback_score(
    query_norm: str,
    query_tokens: list[str],
    haystack: str,
    document: SearchDocument,
) -> float:
    """Score one document for the SQLite path. READ THE GUARANTEES BELOW.

    This is not a port of ``postgres_lexical_search`` and cannot be one. It is a
    tier ladder over Python string operations, and the backend test suite runs
    on SQLite — so it is what almost every search assertion in the repo actually
    executes. Being explicit about the gap is the point of this docstring:
    silence here is how the next person concludes the suite covers ranking.

    WHAT HOLDS ON BOTH DIALECTS
    ---------------------------
    * **The identity tiers.** ``title == query`` and ``keywords == query`` are
      the only tiers that reach ``_FULL_CONFIDENCE_SCORE``, matching Postgres's
      5.0/4.0 exact boosts. See below for what that cost.
    * **Half of tripl-gbxj.** Harvested values are no longer joined into a
      variable's ``keywords`` (``_search_documents._variable_document``), which
      is an INDEXING change and therefore applies to every dialect. Its other
      half — bounding ``ts_rank_cd`` with normalization flag 32 — has no analogue
      here and needs none: this scorer returns one tier value per document and
      has no term-frequency term to run away with in the first place.
    * **tripl-h9x2, both halves.** The spaced alias of every identifier is in
      the index (again a document-building change), and the word-boundary tiers
      below additionally fold the QUERY into its identifier form through the
      same :func:`identifier_form` the Postgres regex tiers use. So
      ``q='screen spot'`` reaches the ``screen_spot`` event from either side
      here too.

    WHAT IS POSTGRESQL-ONLY — NOT APPROXIMATED, NOT TESTED HERE
    -----------------------------------------------------------
    * **All of tripl-nh5s: stemming, and the 3.25 boost tier that depends on
      it.** Both are ``tripl_search`` — a text-search configuration and a
      ``to_tsvector`` comparison. Python's standard library has no stemmer and
      this project has no dependency that provides one, so ``q='purchases'``
      does NOT reach ``purchase_completed`` on SQLite, and ``q='уловы'`` does not
      reach ``улов``. Every stemming case in the relevance table
      (``tests/relevance/cases.py``: purchase-plural, ulov-plural, spots-plural,
      russian-phrase) is a Postgres-only guarantee. tripl-uojz — indexing and
      querying the SURFACE form beside the stem, so an over-stemmed nominative
      (``улов`` -> ``ул``) is still reachable from its own inflections — is the
      same story for the same reason: no stemmer here means no over-stemmer
      either, so this dialect never had the fault and cannot demonstrate the fix.
    * **``ts_rank_cd`` and trigram similarity.** There is no lexical rank and no
      fuzzy-match leg beyond the crude ``SequenceMatcher`` tail below, so the
      x4.0/x2.0 weighting the whole boost ladder is calibrated against does not
      exist here. Two documents that Postgres separates by rank are separated
      here only if they land on different tiers.
    * **``COVERAGE_BONUS`` (tripl-9t2s), and it needs no analogue.** On Postgres
      a document is paid a flat 1.0 for satisfying the whole tsquery, which is
      how a complete match stops losing to a short almost-exact title. This
      ladder returns ONE tier per document and never sums evidence, so there is
      nothing here for a coverage term to be added to — and it already prefers
      coverage in its own crude way, since ``_SQLITE_ALL_TOKENS`` requires every
      query token to be present. What does NOT hold here is the ORDER that term
      produces: the ``q='экран спота'`` case is Postgres-only for the same
      reason the stemming cases are.

    The consequence, stated plainly: **the SQLite suite does not cover ranking.**
    A green ``uv run pytest`` says the search endpoint returns the right rows and
    the right shapes; only the relevance harness (``tests/relevance/``, a real
    PostgreSQL, its own CI job) says they come back in the right ORDER. See
    CONTRIBUTING.md, "Search relevance harness".

    WHY THE PARTIAL TIERS MOVED (tripl-txcz)
    ----------------------------------------
    The ladder used to run 10.0 / 9.0 / 7.0 / 6.8 / 6.5 / 6.0 / 4.0 / 3.0, with
    ``_FULL_CONFIDENCE_SCORE`` at 7.0 — so a bare ``title.startswith(query)``
    was served at confidence 1.0. A one-character query is a prefix of a great
    many titles; that is the weakest evidence in the ladder being presented as
    the strongest possible answer, i.e. exactly the "asdkjhasd at 100%" defect
    on the other dialect.

    The whole ladder — every tier AND the fuzzy tail's weight — is therefore
    multiplied by one uniform factor of 0.8. Uniform is the load-bearing word:
    the two identity tiers land on 8.0 and 7.2, still above the certainty line,
    while the strongest partial tier lands on 5.6 (confidence 0.80), and because
    every value moved by the same factor, EVERY ratio in the ladder is
    preserved. Ranking on this dialect is provably unchanged — all comparisons
    are between scaled values, and nothing downstream reorders them: the one
    step that could have, the multiplicative ``_apply_event_type_boost``, was
    deleted in tripl-0tt4 item 4. Only the number painted on a result moves,
    which is the whole of tripl-txcz.
    """
    if not query_norm:
        return 0.0
    title = _normalize(document.title)
    keywords = _normalize(document.keywords)
    if title == query_norm:
        return _SQLITE_EXACT_TITLE
    if keywords == query_norm:
        return _SQLITE_EXACT_KEYWORDS
    # The identifier fold is applied ONLY to the word-boundary tiers, never to
    # the identity tiers above (tripl-h9x2). That mirrors Postgres exactly:
    # there ``token_boundary_regex`` feeds the 3.5/3.0 tiers while
    # ``lower(title) = lower(:query)`` compares the raw query, so ``q='screen
    # spot'`` is a strong token match on ``screen_spot`` and not an exact-title
    # match. Folding it into the identity tiers here would make SQLite MORE
    # certain than Postgres about the same query.
    identifier_query = identifier_form(query_norm)
    if title.startswith(query_norm):
        return _SQLITE_TITLE_PREFIX
    if _matches_token(document.keywords, query_norm, identifier_query):
        return _SQLITE_KEYWORD_TOKEN
    if _matches_token(document.body, query_norm, identifier_query):
        return _SQLITE_BODY_TOKEN
    if query_norm in title or query_norm in keywords:
        return _SQLITE_TITLE_OR_KEYWORD_SUBSTRING
    if query_norm in haystack:
        return _SQLITE_HAYSTACK_SUBSTRING
    if query_tokens and all(token in haystack for token in query_tokens):
        return _SQLITE_ALL_TOKENS
    similarity = SequenceMatcher(None, query_norm, title or haystack[:200]).ratio()
    return similarity * _SQLITE_FUZZY_WEIGHT if similarity >= _SQLITE_FUZZY_MIN_RATIO else 0.0


def _matches_token(source: str | None, query_norm: str, identifier_query: str | None) -> bool:
    """Whether ``source`` carries the query as a whole word, spaced or underscored.

    The SQLite half of tripl-h9x2's query-time fold: ``q='screen spot'`` has to
    reach a document whose text spells ``screen_spot``, the same way
    ``token_boundary_regex`` lets it on Postgres.
    """
    if _contains_exact_token(source or "", query_norm):
        return True
    return identifier_query is not None and _contains_exact_token(source or "", identifier_query)


def token_boundary_regex(query: str) -> str | None:
    """PostgreSQL word-boundary pattern for the query, or ``None`` if it is not a token.

    QUERY-TIME HALF OF tripl-h9x2
    -----------------------------
    Entities in this product are named in snake_case (``screen_spot``,
    ``vip_segment``, ``catch_report_created``) but people type them with spaces.
    This function used to require ``[a-z0-9_]+`` over the RAW query, so it
    returned ``None`` for anything containing a space — which silently deleted
    the 3.5 (keywords) and 3.0 (body) word-boundary tiers from the ranking of
    every multi-word query, and for Cyrillic, which is not in ``[a-z0-9]`` at
    all. Measured: ``q='screen spot'`` ranked the ``screen_spot`` event 5th at
    4.545, behind harvested-value variables, because the only tiers left compare
    the spaced query against an underscored title and all miss.

    So the query is folded INTO the identifier form (spaces -> underscores)
    instead of being rejected for having spaces, and the token test is Unicode
    (``\\w``) instead of ASCII-only, so a Cyrillic query is a token like any
    other. ``\\m``/``\\M`` are PostgreSQL word boundaries and treat ``_`` as a
    word character, so ``\\mscreen_spot\\M`` still matches only the whole
    identifier and never a fragment of ``screen_spot_v2``.

    The index-time half lives in ``_search_documents._spaced_identifiers``,
    which puts the SPACED alias of each identifier into the document's
    keywords; between the two, ``screen_spot`` and ``screen spot`` reach each
    other whichever side the underscore is on. The SQLite fallback scorer gets
    the index-time half for free and applies the query-time half itself, through
    the shared :func:`identifier_form` — see :func:`fallback_score`, which also
    lists what it CANNOT reproduce.

    Queries that are not identifier-shaped (punctuation, quotes, ``100%``) still
    return ``None`` and keep the LIKE/trigram behaviour they always had — this
    only converts a whitespace-separated identifier, it does not invent tokens.
    """
    identifier_query = identifier_form(query)
    if identifier_query is None:
        return None
    return rf"\m{re.escape(identifier_query)}\M"


def identifier_form(query: str) -> str | None:
    """The query folded into its snake_case identifier form, or ``None``.

    Extracted from :func:`token_boundary_regex` (tripl-h9x2) so the SQLite
    fallback scorer can apply the identical fold to its own word-boundary tiers
    rather than carrying a second, subtly different idea of what an identifier
    is — see :func:`fallback_score`. Both dialects therefore agree on which
    queries are identifier-shaped, which is the part of tripl-h9x2 that does not
    need PostgreSQL to hold.
    """
    normalized_query = _normalize(query)
    identifier_query = re.sub(r"\s+", "_", normalized_query)
    if not re.fullmatch(r"\w+", identifier_query, flags=re.UNICODE):
        return None
    return identifier_query


def _contains_exact_token(source: str, query_norm: str) -> bool:
    if not source or not query_norm:
        return False
    return re.search(rf"(^|[^\w]){re.escape(query_norm)}([^\w]|$)", _normalize(source)) is not None


def merge_results(
    lexical_results: list[SearchResult],
    semantic_results: list[SearchResult],
    limit: int,
) -> list[SearchResult]:
    """Fold the semantic leg into the lexical one: one ranking, two certainties.

    A semantic row arrives with ``score`` set to its raw cosine similarity in
    [0, 1] (see :func:`postgres_semantic_search`). For RANKING it is converted
    onto the lexical score's scale by ``_SEMANTIC_SCORE_WEIGHT``, and a document
    both legs found keeps the sum — that arithmetic is unchanged.

    The raw cosine is recorded on the result before it is overwritten, because
    it is the only honest measure of how sure the semantic leg is and the merged
    score destroys it (tripl-txcz). ``finalize_results`` reads it back; nothing
    else does, and it never reaches the API response.

    PROVENANCE IS NEITHER OF THOSE TWO NUMBERS (tripl-wkwv.3)
    ---------------------------------------------------------
    The legs are SUMMED for ranking and ``max``-ed for confidence, and per-result
    ``semantic_used`` used to be set from a third thing again: whether the vector
    leg's window happened to contain the row. It always did. The vector leg is a
    plain ``ORDER BY <=> LIMIT`` kNN scan with a cosine floor (see
    :func:`postgres_semantic_search`), so it returns rows for ANY query, and a
    document both legs found is paid ``lexical + cosine * 2.5`` — exactly the
    rows that sort to the top. So the flag was dense at the head of the list and
    told the reader that an exact-name hit had been "matched by meaning".
    "The vector leg also listed this row" is a fact about the WINDOW, not about
    the row. The flag answers "why is this row here", and for a document the
    lexical ladder already produced the answer is the ladder — so only the
    vector-ONLY branch below sets it.

    The symmetric caveat, which every consumer's wording has to respect: the
    LEXICAL leg is a ``LIMIT``-ed scan too, so ``existing is None`` means "the
    keyword leg did not rank this inside its window", not "no keyword matched
    it". A weak match — a stem-only tsquery hit, which pays the coverage bonus
    and no boost tier — can be displaced from that window and re-enter here on
    the vector-only branch. Hence "found by meaning, the keyword ranking did not
    surface this" everywhere downstream, never "no keyword matched this".
    """
    merged: dict[uuid.UUID, SearchResult] = {item.id: item for item in lexical_results}
    for item in semantic_results:
        existing = merged.get(item.id)
        cosine = max(0.0, item.score)
        semantic_score = cosine * _SEMANTIC_SCORE_WEIGHT
        if existing is None:
            item.score = semantic_score
            # Redundant with ``row_to_result(..., semantic_used=True)``, and kept
            # explicit: this branch IS the rule, so the rule is legible where a
            # future reader looks for it.
            item.semantic_used = True
            item.record_semantic_cosine(cosine)
            merged[item.id] = item
            continue
        existing.score += semantic_score
        # No ``existing.semantic_used = True`` here — see the docstring. The
        # cosine below is still recorded, so a hybrid row keeps the stronger of
        # its two certainties (tripl-txcz); confidence and provenance are
        # different questions and this row can honestly answer 0.9 to the first
        # and "the keyword leg" to the second.
        existing.record_semantic_cosine(cosine)
    return sorted(merged.values(), key=lambda item: (-item.score, item.title))[:limit]


def finalize_results(items: list[SearchResult], limit: int) -> list[SearchResult]:
    """Rank, trim, and stamp confidence on a merged candidate set.

    WHAT USED TO HAPPEN FIRST HERE, AND WHY IT NO LONGER DOES (tripl-0tt4 item 4)
    ----------------------------------------------------------------------------
    An ``_apply_event_type_boost`` pass ran ahead of the sort: every event whose
    ``subtitle`` named an ``event_type`` document present in the candidate set was
    multiplied by up to 1.75. It was written for descriptive queries — "экран
    спота" resolving to the ``pageviews`` type and lifting that type's events.

    MEASURED ON PRODUCTION 2026-08-16, at the same 100-row window this function
    now receives, that intended case never occurred: across seven descriptive
    queries on each of the three real projects, no ``event_type`` document ever
    entered the window, so there was nothing to boost. The only queries that DID
    reach it were the type's own name — ``pv``, ``se``, ``old`` — and there the
    boost did the opposite of its purpose. Multiplying every event of the type
    while leaving the TYPE document itself unmultiplied buried the one document
    the query was actually naming:

        q='pv'  acme-web      "Pageview"          rank 100 of 100 -> 1 without it
        q='pv'  acme-ios      "Pageview"          rank 100 of 100 -> 1 without it
        q='se'  acme-android  "Structured Event"  rank  36 of 100 -> 1 without it
        q='old' acme-ios      "Old"               rank 100 of 100 -> 2 without it

    (Rank without the boost is exact, not estimated: with one type dominating the
    set its relevance is 1.0, so dividing a boosted score by 1.75 recovers the
    pre-boost score, and limit=100 equals the candidate window so nothing had
    been trimmed before the reading.)

    Ranking is now the merged score alone. Nothing replaced the boost: a document
    that answered the whole query is already paid for it by ``COVERAGE_BONUS``,
    which is the signal the type boost was reaching for and which does not
    require guessing a category.


    CONFIDENCE IS ABSOLUTE, NOT RELATIVE TO THE TOP HIT (tripl-txcz)
    ---------------------------------------------------------------
    This used to divide every score by the top score, which makes the best
    result of ANY result set exactly 1.0 by construction. The number therefore
    could not express the one thing a user needs from it — "I did not really
    find what you asked for" — and the UI paints it as a percentage badge.
    Measured on production: ``q='asdkjhasd'`` (pure keyboard mash) was served at
    confidence 1.0 on an absolute score of 0.636.

    The fraction is now taken against ``_FULL_CONFIDENCE_SCORE``, the score a
    document reaches when it IS the thing that was typed, so confidence means
    the same thing across queries: it is comparable between two searches, it
    falls when the match is weak, and it reaches 1.0 only when the ranking
    arithmetic actually says so. The relative ORDER of results is untouched —
    this changes the number attached to a result, not which result wins.

    BOTH LEGS HAVE TO BE ABLE TO SAY "CERTAIN"
    ------------------------------------------
    Taking that fraction off the merged score alone would have quietly done to
    the semantic leg what the same issue forbids doing to it deliberately. A
    vector-only hit is scored ``cosine * _SEMANTIC_SCORE_WEIGHT`` for ranking,
    so its score cannot exceed 2.5 and a PERFECT cosine of 1.0 would have been
    reported at 2.5 / 7.0 = 0.357 — a semantic rescue of 'пейволл' or 'forcast',
    the queries that leg exists for, served to the user as barely-a-guess.

    So confidence is the maximum of the two certainties the result actually
    carries:

    * the ABSOLUTE score certainty, ``score / _FULL_CONFIDENCE_SCORE`` capped at
      1.0, which is what the lexical ladder (and the SQLite fallback's tiers)
      express;
    * for a result the semantic leg produced, its cosine similarity, which is
      already on a [0, 1] scale and needs no rescaling.

    ``max`` rather than a sum or a blend: the two are alternative pieces of
    evidence for the same claim ("this is what you meant"), so a document that
    one leg is sure about is a confident answer even when the other leg is
    indifferent to it. Non-identity hits are capped after combining both legs:
    a high cosine alone does not prove the hit is the entity the user named.
    The RANKING still sees only the merged score — this function decides what
    number is painted on a result, never where it sits.
    """
    ranked = sorted(items, key=lambda item: (-item.score, item.title))
    trimmed = ranked[:limit]
    for item in trimmed:
        score_confidence = min(1.0, max(0.0, item.score) / _FULL_CONFIDENCE_SCORE)
        semantic_confidence = item.semantic_cosine or 0.0
        confidence = max(score_confidence, semantic_confidence)
        if not item.identity_match:
            confidence = min(confidence, _PARTIAL_CONFIDENCE_CEILING)
        item.confidence = round(confidence, 4)
    return trimmed


async def enrich_event_hits(
    session: AsyncSession,
    items: list[SearchResult],
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
) -> None:
    event_ids: set[uuid.UUID] = set()
    for item in items:
        if item.parent_event_id is not None:
            event_ids.add(item.parent_event_id)
    if not event_ids:
        return

    rows = (
        await session.execute(
            select(Event.id, Event.name, Event.status).where(
                Event.project_id == project_id,
                Event.branch_id == branch_id,
                Event.id.in_(event_ids),
            )
        )
    ).all()
    events_by_id: dict[uuid.UUID, tuple[str, str]] = {
        event_id: (name, status) for event_id, name, status in rows
    }

    variable_rows = (
        await session.execute(
            select(
                VariableValue.id,
                VariableValue.event_id,
                VariableValue.variable_id,
                Variable.name,
                VariableValue.field_definition_id,
                FieldDefinition.name,
                FieldDefinition.display_name,
                VariableValue.source_column,
                VariableValue.value_kind,
                VariableValue.observed_count,
                VariableValue.values,
            )
            .join(Variable, VariableValue.variable_id == Variable.id)
            .join(FieldDefinition, VariableValue.field_definition_id == FieldDefinition.id)
            .where(
                VariableValue.project_id == project_id,
                VariableValue.branch_id == branch_id,
                VariableValue.event_id.in_(event_ids),
                FieldDefinition.sensitivity == "none",
            )
            .order_by(
                VariableValue.event_id.asc(),
                FieldDefinition.order.asc(),
                FieldDefinition.name.asc(),
                Variable.name.asc(),
            )
        )
    ).all()
    variable_values_by_event: dict[uuid.UUID, list[SearchEventVariableValue]] = {}
    for (
        context_id,
        event_id,
        variable_id,
        variable_name,
        field_definition_id,
        field_name,
        field_display_name,
        source_column,
        value_kind,
        observed_count,
        values,
    ) in variable_rows:
        variable_values_by_event.setdefault(event_id, []).append(
            SearchEventVariableValue(
                id=context_id,
                variable_id=variable_id,
                variable_name=variable_name,
                field_definition_id=field_definition_id,
                field_name=field_name,
                field_display_name=field_display_name,
                source_column=source_column,
                value_kind=value_kind,
                observed_count=observed_count,
                values=list(values or []),
            )
        )

    for item in items:
        if item.parent_event_id is None:
            continue
        event_state = events_by_id.get(item.parent_event_id)
        if event_state is None:
            continue
        name, status = event_state
        item.event_id = item.parent_event_id
        item.name = name
        item.implemented = status in ("implemented", "live")
        item.variable_values = variable_values_by_event.get(item.parent_event_id, [])


def row_to_result(row: object, query: str, *, semantic_used: bool) -> SearchResult:
    mapping = cast(Mapping[str, object], row)
    score_raw = mapping["score"]
    score = float(str(score_raw)) if score_raw is not None else 0.0
    # Shared by both Postgres legs, and only the LEXICAL one projects a ladder
    # tier — the semantic SELECT has no `boost` column because it never runs the
    # ladder. Absent therefore means "no identity evidence", which is the honest
    # reading for a vector-only hit; its certainty comes from its cosine instead
    # (tripl-d5u8).
    boost_raw = mapping.get("boost")
    boost = float(str(boost_raw)) if boost_raw is not None else 0.0
    result = SearchResult(
        id=uuid.UUID(str(mapping["id"])),
        entity_type=cast(SearchEntityType, str(mapping["entity_type"])),
        entity_id=uuid.UUID(str(mapping["entity_id"])),
        parent_event_id=uuid.UUID(str(mapping["parent_event_id"]))
        if mapping["parent_event_id"] is not None
        else None,
        title=str(mapping["title"]),
        subtitle=str(mapping["subtitle"] or ""),
        description=str(mapping.get("description") or ""),
        snippet=snippet(str(mapping["body"] or ""), query),
        route_path=str(mapping["route_path"]),
        score=score,
        highlights=highlights(str(mapping["title"]), str(mapping["body"] or ""), query),
        semantic_used=semantic_used,
    )
    result.record_identity_match(identity=boost >= _IDENTITY_BOOST_MIN)
    return result


def document_to_result(
    document: SearchDocument,
    score: float,
    query: str,
    *,
    semantic_used: bool,
) -> SearchResult:
    result = SearchResult(
        id=document.id,
        entity_type=cast(SearchEntityType, document.entity_type),
        entity_id=document.entity_id,
        parent_event_id=document.parent_event_id,
        title=document.title,
        subtitle=document.subtitle,
        description=document.description or "",
        snippet=snippet(document.body, query),
        route_path=document.route_path,
        score=score,
        highlights=highlights(document.title, document.body, query),
        semantic_used=semantic_used,
    )
    # On this dialect the tier IS the score (``fallback_score`` returns one tier
    # value per document and never sums evidence), so the identity question is
    # answered by comparing against the lower of the two equality tiers. The
    # ceiling this feeds is already what SQLite produces unaided — see
    # :data:`_PARTIAL_CONFIDENCE_CEILING` — so nothing here changes; recording it
    # is what lets ONE rule in ``finalize_results`` cover both dialects.
    result.record_identity_match(identity=score >= _SQLITE_EXACT_KEYWORDS)
    return result


def snippet(body: str, query: str, *, length: int = 180) -> str:
    text_body = _clean(body)
    if not text_body:
        return ""
    query_norm = _normalize(query)
    body_norm, spans = _normalized_spans(text_body)
    idx = body_norm.find(query_norm) if query_norm else -1
    if idx < 0:
        return text_body[:length]
    start = max(0, spans[idx][0] - 60)
    end = min(len(text_body), start + length)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text_body) else ""
    return f"{prefix}{text_body[start:end]}{suffix}"


def highlights(title: str, body: str, query: str) -> list[str]:
    result: list[str] = []
    for token in _tokens(query)[:4]:
        for source in (title, body):
            source_norm, spans = _normalized_spans(source)
            idx = source_norm.find(token)
            if idx >= 0:
                start = max(0, spans[idx][0] - 20)
                end = spans[idx + len(token) - 1][1] + 40
                result.append(source[start:end].strip())
                break
    return result
