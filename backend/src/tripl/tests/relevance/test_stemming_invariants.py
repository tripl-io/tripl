"""The stemming invariant, asserted on the mechanism itself.

WHY THIS FILE EXISTS, AND WHY IT IS THE PRIMARY GUARD
-----------------------------------------------------
Three times now the relevance harness has certified a search change that was
wrong on production, and every one of those false greens had the same shape: the
claim was about RETRIEVAL — can this form of this word reach that form of that
word — and it was tested through RANKING, over a corpus, where a dozen unrelated
mechanisms (trigram similarity, the ``LIKE`` tiers, the boost ladder, term
frequency) can supply the right answer for the wrong reason.

* the ``ts_rank_cd`` normalization flag could be deleted with the whole table
  staying green, because no corpus document had the repetition profile that
  makes normalization observable;
* the ``улов``/``уловы`` pair asserted on ONE document that carried BOTH
  lexemes, so either query retrieved it by a different lexeme and the asymmetry
  between them was invisible;
* on the strength of that pair, a query-side-only repair for the over-stemming fault was
  certified and was wrong on production.

So this file tests the mechanism directly. It has NO corpus, seeds NOTHING,
executes no ranking SQL and reads no score. It asks Postgres one question, over
and over: does a document consisting of exactly one word form get matched by a
query for exactly one other form of the same word? That question has no second
path to a yes. There is no title to be trigram-similar to, no body for a
``LIKE`` to find a substring in, no boost tier to fire. Either the tsvector and
the tsquery share a lexeme or they do not.

THE INVARIANT
-------------
Every token ``w`` contributes ``{stem(w), surface(w)}`` on BOTH sides — see
``search_service.TEXT_VECTOR_EXPRESSION`` (document) and
``_search_query.TEXT_QUERY_EXPRESSION`` (query). Two forms A and B therefore
meet iff those sets intersect. They already met when ``stem(A) == stem(B)``,
which is what a7c3e1b9d5f2 bought. What was missing, and what the surface-form leg adds,
is ``surface(A) == stem(B)``: Snowball over-stems the shortest form of a word
onto a lexeme that SOME of its inflections never produce (measured on
production: ``to_tsvector('tripl_search', 'уловы улов уловов')`` is
``'ул':2 'улов':1,3``), and the form that gets over-stripped is spelled the same
as the stem those inflections do land on.

How many of a word's forms fall on each side is a per-word fact and not a rule:
for ``улов`` only the bare nominative over-stems, while for ``экран`` the
nominative, the genitive AND the nominative plural all land on ``экра`` and only
``экране`` reaches ``экран``. See the measured table below before writing down a
pair.

THIS FILE IMPORTS THE SHIPPED EXPRESSIONS; IT DOES NOT COPY THEM
-----------------------------------------------------------------
Both constants are imported from the service modules and interpolated into the
SQL below verbatim. A copied literal would keep passing after either side was
edited — and "the two strings agree by convention" is exactly the arrangement
that let the index and the query drift apart in the first place. If someone
changes what a document indexes without changing what a query asks for, these
assertions are what goes red.

WHY THE WORD FORMS BELOW AND NOT OTHERS
----------------------------------------
Every form here is production vocabulary, taken from the read-only measurement
of the deployed database (image d47b55b2, alembic_version a7c3e1b9d5f2) that
this issue is built on: lemma / over-stem, with the number of production
``search_documents`` rows holding each::

    экран/экра   414/3971     показа/показ   341/429
    открыт/откр  302/260      закрыт/закр    183/100
    создан/созда 124/172      найден/найд     18/181
    выключен/выключ 10/137    архив/арх       23/53
    улов/ул      151/0

WHICH INDIVIDUAL FORMS WERE STEMMED, AND WHICH WERE ONLY REASONED ABOUT
------------------------------------------------------------------------
The row counts above say which LEXEMES exist in production. They do not say
which FORM produces which lexeme, and assuming the obvious mapping is how the
first version of this file ended up asserting a vacuous pair. Every form
stemmed directly against the deployed database::

    экран -> экра     улов  -> ул      архив  -> арх
    экрана -> экра    уловы -> улов    архивы -> архив
    экраны -> экра    улова -> улов
    экране -> экран

The ``улов``, ``экран`` and ``архив`` groups below contain those forms and no
others. The remaining groups (``показ``, ``открыт``, ``закрыт``, ``создан``,
``найден``, ``выключен``) were NOT stemmed form by form; they rest on the
base-vs-derived argument in the next paragraph. If one of them goes red, read it
as the measurement this file never had — that paradigm splits the way ``экран``
does — before reading it as a regression.

The forms in a group are ONE inflectional paradigm — a base form plus that base
form with an ending on it. That restriction is not tidiness, it is what makes
the assertion safe to make without a database in hand. For a base ``B`` and a
form ``B+suffix``, a stemmer either strips only the added suffix (landing on
``B``, which is ``surface(B)``) or strips the added suffix together with what it
would have stripped from ``B`` anyway (landing on ``stem(B)``). Either way the
sets intersect, so the invariant holds for every BASE-vs-DERIVED pair.

That argument does NOT extend to two derived forms, and adding a third form to a
group silently asserts the derived-vs-derived pairs as well. Read the next
section before widening any group here.

WHAT IS DELIBERATELY ABSENT, AND WHY THAT IS A FINDING AND NOT A GAP
---------------------------------------------------------------------
Two INFLECTED forms of one word can still land in different classes, and then
neither the stem leg nor the identity leg joins them. Measured, not guessed:

    экран -> экра    экрана -> экра    экраны -> экра    экране -> экран

``экрана`` indexes ``{экра, экрана}`` and ``экране`` asks ``{экран, экране}``;
those sets do not intersect, so that pair misses in both directions, and
``экраны``/``экране`` misses for the same reason. Those pairs are NOT listed
here, and the reason is stated rather than hidden: this fix
does not repair them, the implementation docstrings say so ("two forms that both
over-stem, to different lexemes, whose surfaces match neither stem, would still
miss"), and a test that quietly omitted them while claiming to assert "every
form reaches every other form" would be one more comfortable lie. What is
asserted is what the fix actually delivers. Widening it is its own issue, and it
needs a different mechanism (a query-time expansion, or a dictionary) than a
second configuration.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.services._search_query import TEXT_QUERY_EXPRESSION
from tripl.services.search_service import TEXT_VECTOR_EXPRESSION

#: One document holding exactly one word form, matched against a query holding
#: exactly one other form, using the SHIPPED expressions on both sides.
#:
#: The document's text is fed in as ``title`` with the other three columns
#: empty; which column carries it is irrelevant, because
#: ``TEXT_VECTOR_EXPRESSION`` concatenates all four before tokenizing. The casts
#: are there because ``concat_ws`` is variadic-``any`` and the driver would
#: otherwise send a parameter of undetermined type.
_REACHES = text(
    f"""
    SELECT (
        SELECT {TEXT_VECTOR_EXPRESSION}
        FROM (
            SELECT
                CAST(:form AS text) AS title,
                CAST('' AS text) AS subtitle,
                CAST('' AS text) AS body,
                CAST('' AS text) AS keywords
        ) AS d
    ) @@ ({TEXT_QUERY_EXPRESSION}) AS reached
    """  # noqa: S608 - both operands are module constants, not user input
)

#: The SAME question asked of the STEM LEG ALONE — i.e. of the index and query
#: a7c3e1b9d5f2 shipped and b6d1f0a3c7e2 replaced.
#:
#: This is a deliberate frozen copy and must NOT be refactored to import
#: ``TEXT_VECTOR_EXPRESSION`` / ``TEXT_QUERY_EXPRESSION``: its whole job is to
#: express the OLD behaviour, so that
#: :func:`test_the_invariant_would_fail_without_the_surface_leg` can prove the
#: invariant above is not vacuous. If it followed the live constants it would
#: become a tautology the day someone reverted the fix — which is the failure
#: mode this entire file exists to close.
_STEM_LEG_ONLY_REACHES = text(
    """
    SELECT to_tsvector('tripl_search', CAST(:form AS text))
           @@ websearch_to_tsquery('tripl_search', :query) AS reached
    """
)

#: What each leg makes of one form. Used only to build failure messages: a red
#: assertion here should say WHICH lexemes failed to intersect, so the reader
#: can tell "the fix regressed" from "this form does not behave as assumed"
#: without a psql session.
_LEXEMES = text(
    """
    SELECT
        CAST(to_tsvector('tripl_search', CAST(:form AS text)) AS text) AS stem,
        CAST(to_tsvector('tripl_search_surface', CAST(:form AS text)) AS text) AS surface
    """
)


@dataclass(frozen=True)
class WordForms:
    """One inflectional paradigm of one production word.

    ``forms`` is a base form followed by forms built from it. ``measured`` is
    what the production database said about this word, quoted rather than
    paraphrased, so a future reader can tell measurement from inference.
    """

    word: str
    forms: tuple[str, ...]
    measured: str


WORDS: tuple[WordForms, ...] = (
    WordForms(
        word="улов",
        forms=("улов", "уловы", "улова"),
        measured=(
            "улов -> 'ул', уловы -> 'улов', улова -> 'улов'; улов/ул 151/0 — the one "
            "word whose over-stem class is EMPTY on production, which is why the "
            "nominative seeded in the corpus was a form the product does not produce. "
            "'уловов' and 'улове' were listed here and have been dropped: the "
            "the earlier re-measurement covered улов/уловы/улова only, and after "
            "'экрана' turned out not to stem where this file assumed, a form nobody "
            "re-stemmed does not get to carry an assertion"
        ),
    ),
    WordForms(
        word="экран",
        forms=("экран", "экране"),
        measured=(
            "экран/экра 414/3971 — the over-stem class is the MAJORITY, which is what "
            "killed the query-side-only repair. THE CUT IS NOT WHERE IT LOOKS: 'экран', "
            "'экрана' and 'экраны' ALL stem to 'экра', and only 'экране' stems to "
            "'экран'. It is prepositional-vs-the-rest, NOT nominative-vs-genitive, so "
            "the pair ('экран','экрана') this group used to hold asserted nothing — "
            "both sides land on 'экра' and meet on the stem leg alone. Do not add "
            "'экрана' or 'экраны' back: paired with 'экране' they are the measured "
            "MISS this fix does not repair (see the module docstring), and the group "
            "would go red for a real reason"
        ),
    ),
    WordForms(
        word="архив",
        forms=("архив", "архивы"),
        measured=(
            "архив -> 'арх', архивы -> 'архив'; архив/арх 23/53. 'архива' was listed "
            "here and has been dropped as never stemmed against the database "
            ""
        ),
    ),
    WordForms(
        word="показ",
        forms=("показ", "показа", "показы"),
        measured=(
            "показа/показ 341/429. NOTE: the 341 rows holding 'показа' are the "
            "PARTICIPLE family (показан, показанного — 'Идентификатор показанного "
            "экрана' is in the harness corpus), not this noun paradigm, whose forms "
            "all stem to 'показ'. Listed as a CONTROL: it passes on the stem leg "
            "alone, so a run where every group is green only because of the surface "
            "leg would look different from this one"
        ),
    ),
    WordForms(
        word="открыт",
        forms=("открыт", "открыта", "открыто", "открыты"),
        measured="открыт/откр 302/260",
    ),
    WordForms(
        word="закрыт",
        forms=("закрыт", "закрыта", "закрыто", "закрыты"),
        measured="закрыт/закр 183/100",
    ),
    WordForms(
        word="создан",
        forms=("создан", "создана"),
        measured="создан/созда 124/172",
    ),
    WordForms(
        word="найден",
        forms=("найден", "найдена"),
        measured="найден/найд 18/181",
    ),
    WordForms(
        word="выключен",
        forms=("выключен", "выключена"),
        measured="выключен/выключ 10/137",
    ),
)

#: Pairs the DEPLOYED database was measured to keep apart, one form per lexical
#: class. These are the pairs whose reachability the surface leg is the sole
#: cause of, so they are the ones that prove the invariant above is discriminating
#: rather than vacuously true.
#:
#: Only pairs whose BOTH forms were stemmed against the deployed database are
#: listed. The other paradigms above may or may not need the surface leg — the
#: honest answer is that this was not measured for them, and inventing a claim
#: here would be the same mistake as the corpus comments that asserted a case
#: pinned a fix it did not.
#:
#: ``('экран','экрана')`` was here and is gone: ``экрана`` stems to
#: ``экра``, exactly like ``экран``, so the pair met on the stem leg alone. This
#: parametrization was therefore RED against the shipped code while claiming to
#: certify it, and the ``экран`` group above was GREEN with the surface leg
#: reverted. The genuinely split form is ``экране``.
#:
#: ``('улов','уловов')`` is gone for a weaker but sufficient reason: the pair is
#: plausible, and nobody re-stemmed ``уловов``. ``('улов','улова')`` and
#: ``('архив','архивы')`` replace it and were.
STEM_LEG_ISOLATED_PAIRS: tuple[tuple[str, str], ...] = (
    ("улов", "уловы"),
    ("уловы", "улов"),
    ("улов", "улова"),
    ("улова", "улов"),
    ("архив", "архивы"),
    ("архивы", "архив"),
    ("экран", "экране"),
    ("экране", "экран"),
)

#: Forms of DIFFERENT words that must stay apart. A "fix" that indexed every
#: prefix, or lowered the stemmer to nothing, would satisfy every assertion
#: above and destroy search; these are what makes that visible. Each pair is
#: chosen so that neither leg can join them: the stems differ and neither
#: surface equals the other's stem — for the Cyrillic pairs that is read off the
#: measured table in the module docstring (улов {ул, улов} vs экран {экра,
#: экран}; уловы {улов, уловы} vs экрана {экра, экрана}), not assumed.
#:
#: What this CANNOT rule out, and no list of pairs can: the surface
#: leg joins A to B whenever ``surface(A) == stem(B)``, and nothing in the
#: mechanism checks that A and B are forms of the same word. Two unrelated words
#: where one's spelling is the other's stem now match. These pairs show the
#: collapse is not WHOLESALE; they are not a proof that it never happens.
DISTINCT_WORDS: tuple[tuple[str, str], ...] = (
    ("улов", "экран"),
    ("уловы", "экрана"),
    ("открыт", "закрыт"),
    ("создан", "найден"),
    ("архив", "показ"),
)


# ``unseeded_session`` (the corpus-free session every test here runs on) lives in
# this package's conftest, because test_coverage_invariants.py
# needs the identical fixture.


async def _reaches(session: AsyncSession, *, form: str, query: str) -> bool:
    result = await session.scalar(_REACHES, {"form": form, "query": query})
    return bool(result)


async def _reaches_on_the_stem_leg(session: AsyncSession, *, form: str, query: str) -> bool:
    result = await session.scalar(_STEM_LEG_ONLY_REACHES, {"form": form, "query": query})
    return bool(result)


async def _explain(session: AsyncSession, *form_list: str) -> str:
    parts: list[str] = []
    for form in form_list:
        row = (await session.execute(_LEXEMES, {"form": form})).mappings().one()
        parts.append(f"{form!r}: stem={row['stem']} surface={row['surface']}")
    return "; ".join(parts)


@pytest.mark.parametrize("word", WORDS, ids=[entry.word for entry in WORDS])
async def test_every_form_of_a_word_reaches_every_other_form(
    word: WordForms,
    unseeded_session: AsyncSession,
) -> None:
    """A document holding only form A must be found by a query for form B.

    Asserted in BOTH directions for every ordered pair, because the fault was
    directional and a one-directional assertion is how it stayed hidden: a
    document holding the inflected form was always reachable from the inflected
    query, and only the nominative was stranded.

    Nothing here can be satisfied by a second mechanism. The document is one
    word, so there is no substring for the ``LIKE`` tiers, no title for trigram
    similarity, and no ranking at all — this is ``tsvector @@ tsquery`` and
    nothing else.
    """
    for form in word.forms:
        for other in word.forms:
            if form == other:
                continue
            reached = await _reaches(unseeded_session, form=form, query=other)
            assert reached, (
                f"a document containing only {form!r} is NOT reachable by a query "
                f"for {other!r}, though both are forms of {word.word!r}. "
                f"{await _explain(unseeded_session, form, other)}. "
                f"Measured on production: {word.measured}."
            )


@pytest.mark.parametrize(
    ("form", "query"),
    STEM_LEG_ISOLATED_PAIRS,
    ids=[f"{form}-from-{query}" for form, query in STEM_LEG_ISOLATED_PAIRS],
)
async def test_the_invariant_would_fail_without_the_surface_leg(
    form: str,
    query: str,
    unseeded_session: AsyncSession,
) -> None:
    """Prove the assertion above is discriminating, not vacuously true.

    THIS IS THE TEST THAT MAKES THE HARNESS UNABLE TO CERTIFY A BROKEN FIX
    ----------------------------------------------------------------------
    A test that passes is worthless until you know what would make it fail. The
    previous harness never had that check, and it is precisely how three
    consecutive false greens were possible: the ``ts_rank_cd`` flag could be
    deleted with the whole table still green, and the ``улов`` pair passed under
    a repair that was wrong on production.

    So this asks Postgres the same reachability question with the STEM LEG
    ALONE — the exact index and query a7c3e1b9d5f2 shipped — and requires the
    answer to be NO for these measured pairs. Two different regressions turn it
    red, which is the point:

    * revert the surface leg and the test above goes red while this one stays
      green — the fix is gone and the harness says so;
    * weaken the WORD FORMS above to pairs that already met on the stem leg (the
      corpus's original sin, in a new place) and THIS test goes red, because the
      pairs it names would no longer be isolated.

    The second bullet is not hypothetical. This list shipped with
    ``('экран','экрана')`` in it, both of which stem to ``экра``: the pair met on
    the stem leg, so this parametrization was RED against the very fix it was
    written to certify, while the ``экран`` group above was GREEN with the
    surface leg reverted. Both are fixed by using ``экране``, the one measured
    form of that word that lands in the other class.
    """
    isolated = not await _reaches_on_the_stem_leg(unseeded_session, form=form, query=query)
    assert isolated, (
        f"{form!r} is reachable from {query!r} on the stem leg alone, so the "
        f"invariant test proves nothing about the surface-form leg for this pair. Either the "
        f"stemmer changed under us or this pair is no longer the measured one. "
        f"{await _explain(unseeded_session, form, query)}."
    )


@pytest.mark.parametrize(
    ("form", "query"),
    DISTINCT_WORDS,
    ids=[f"{form}-vs-{query}" for form, query in DISTINCT_WORDS],
)
async def test_distinct_words_do_not_reach_each_other(
    form: str,
    query: str,
    unseeded_session: AsyncSession,
) -> None:
    """The cheapest way to pass every other assertion here is to match everything.

    Indexing prefixes, or dropping the stemmer and matching on substrings, would
    make every pair above reachable and make search useless.

    Note what this does and does not establish. The surface leg is purely
    ADDITIVE — both legs are OR-ed on both sides — so it cannot take a match
    away, but "it only adds a more specific lexeme, therefore it cannot merge two
    classes" is false and is not the claim being checked here. The
    added lexeme is a plain string, and a match on it is a string equality that
    knows nothing about words: ``surface(A) == stem(B)`` is the identity the
    whole repair rests on, and it fires just as happily when A and B are
    unrelated. What this test establishes is the empirical fact for THIS
    vocabulary: none of these pairs is joined by either leg.
    """
    reached = await _reaches(unseeded_session, form=form, query=query)
    assert not reached, (
        f"a document containing only {form!r} is reachable by a query for "
        f"{query!r}; these are different words and matching them collapses "
        f"precision. {await _explain(unseeded_session, form, query)}."
    )


async def test_the_two_legs_under_test_are_the_ones_that_ship(
    unseeded_session: AsyncSession,
) -> None:
    """Guard the imports themselves: both sides must really carry both legs.

    Everything above interpolates ``TEXT_VECTOR_EXPRESSION`` and
    ``TEXT_QUERY_EXPRESSION``, so if BOTH were reverted to stem-only at once
    every assertion in this file would keep its shape and quietly measure the old
    behaviour — the invariant test would go red, but a reader would have to work
    out why. This says it in one line instead.

    It also asserts the configurations exist in the database, which is the other
    way this file could be testing something other than production: the harness
    runs the real revision chain, so ``tripl_search_surface`` being absent means
    the migration did not run, not that the name is wrong.
    """
    for expression in (TEXT_VECTOR_EXPRESSION, TEXT_QUERY_EXPRESSION):
        normalized = " ".join(expression.split())
        assert "'tripl_search'" in normalized, f"the stem leg is missing from {normalized!r}"
        assert "'tripl_search_surface'" in normalized, (
            f"the surface leg is missing from {normalized!r}; the surface-form fix has been "
            "reverted on one side and this file would silently measure the old "
            "behaviour"
        )

    configurations = (
        await unseeded_session.scalars(
            text(
                "SELECT cfgname FROM pg_catalog.pg_ts_config "
                "WHERE cfgname IN ('tripl_search', 'tripl_search_surface') "
                "ORDER BY cfgname"
            )
        )
    ).all()
    assert list(configurations) == ["tripl_search", "tripl_search_surface"], (
        f"expected both text search configurations, found {list(configurations)}; "
        "the migration chain did not run"
    )
