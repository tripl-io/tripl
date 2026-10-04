"""The relevance case table: query -> what must rank first, what must not beat it.

WHY A TABLE AND NOT PROSE ASSERTIONS
------------------------------------
Every entry here was measured against production before it was written down:
26 queries over three real projects, every response HTTP 200. Ten
cases, each of them "this query must produce exactly this ranking, and here is
what actually beat it in production".

TWO cases are NOT among those ten, and each says so in its own ``measured``
field rather than borrowing the credibility of the others:

* ``repetition-outlier-does-not-outrank-the-screen-it-names``. A measured
  production query could not reach the fault — the corpus had no document with
  the repetition profile the production fault needed, so the ``ts_rank_cd``
  normalization it pins was invisible to every other case. Its input
  (``corpus._SETTINGS_CARD_TARGETS``) is built to the measured production
  profile even though the query that reads it is constructed here.
* ``over-stemmed-nominative-is-reachable-from-an-inflected-query``.
  Same shape, different mechanism: no production query could
  distinguish the over-stem fault while every Russian document in the corpus
  held both lexical classes at once. Its input
  (``corpus._SCREEN_KINDS``) is built to the measured production shape — the
  3971 rows stranded on ``экра`` — and the query is constructed here.

RETRIEVAL AND ORDERING ARE SEPARATE TESTS, AND ONLY ORDERING MAY BE ``xfail``ED
-------------------------------------------------------------------------------
A case makes up to three claims, and they are not equally strong. "The right
document comes back at all" is retrieval; "it comes back FIRST, ahead of these
named competitors" is ordering; "this is not served as a certain answer" is
confidence. ``test_search_relevance`` asserts them in three separate test
functions on purpose.

The reason is a hole this table used to have. ``pytest.mark.xfail`` marks a
FUNCTION, so while one function asserted retrieval and ordering together, an
``xfail`` filed against a RANKING nuance also excused the document vanishing from
the results entirely. ``russian-phrase-finds-the-event-it-describes`` was the
worked example: it was xfailed because a short almost-exact title outranked a
complete match, and under the old arrangement ``screen_spot`` could have stopped
being retrieved at all — the fault four fixes were aimed at — with the case still
reporting a tidy expected failure. That marker is gone (tripl-9t2s shipped the
coverage term and the case now passes on its own), but the split it motivated
stays: the field is named ``xfail_ordering``, it is read by the ordering test and
by nothing else, and there is no way to express "this case is allowed not to find
the document".

``must_retrieve`` is the other half: documents that must appear SOMEWHERE in the
results, with no claim about where. It is for facts about the retrieval
mechanism that have no measured ranking, and for competitors whose PRESENCE is
what a case depends on — ``must_not_outrank`` is satisfied when a competitor is
absent, so a regression that stopped retrieving the wrong answer would otherwise
turn the plural cases green by deleting the thing they out-rank.

WHAT THE ``xfail_ordering`` FIELD IS, AND WHAT IT DOES NOT PROVE HERE
---------------------------------------------------------------------
A case whose ORDER today's ranking gets wrong is marked ``xfail(strict=True)`` —
an unexpected PASS is a hard failure, so a fix cannot land unnoticed with a stale
marker — and the commit DELETING an ``xfail_ordering`` is the proof that fix
worked.

For the FOUR fixed faults that proof does not exist in this repository's history,
and this docstring will not pretend otherwise: the harness and those four fixes
landed in ONE commit, so no case ever carried a marker on a merged branch. What
the table proves for them is weaker and worth saying plainly — it proves the
ranking is right NOW, and it will catch the next regression in it. Their real
proof was by MUTATION, named on each case: revert the source change, watch that
case go red.

The mechanism has now been exercised end to end, exactly once.
``russian-phrase-finds-the-event-it-describes`` carried an ``xfail_ordering`` for
a fault the four fixes did not address, filed rather than tuned
away — and the commit that added ``COVERAGE_BONUS`` to
``_search_query.postgres_lexical_search`` DELETED that marker. That deletion is
the proof for that issue, in the workflow this field exists for: the marker was
``strict=True``, so the fix could not have landed with a stale marker left behind
either. NO case carries ``xfail_ordering`` today; the field stays for the next
measured fault.

The rule that is NOT negotiable either way: an assertion is never weakened to
make a case pass.

WHAT EACH CASE CAN AND CANNOT PIN — THE AUDIT, WRITTEN DOWN
------------------------------------------------------------------------
"The harness passes" has now been insufficient evidence three times, so every
case below has been asked the only question that matters about it: could it be
green with the fix it names reverted? The answers are on the cases themselves.
Two are worth pulling up here because they contradict what this docstring used
to claim:

* **The two ``spot`` cases pin NEITHER half of tripl-gbxj on its own.** Deleting
  normalization flag 32 was MEASURED to leave the whole table green — that is
  why ``repetition-outlier-does-not-outrank-the-screen-it-names`` had to be
  built. And the keywords half is pinned by ``purchase-plural``, which is the
  case this docstring cited as evidence for the ``spot`` cases; a citation to a
  different case is not evidence about these. What they pin is the two mutations
  TOGETHER, and what they are genuinely good for is being a regression guard on
  a ranking that is correct today.
* **Neither ``улов`` case pins tripl-uojz, and no ordering case can.** See
  ``ulov-singular``. The mechanism is asserted by
  :mod:`tripl.tests.relevance.test_stemming_invariants`, which uses no corpus at
  all; the one corpus case that pins it through RETRIEVAL is
  ``over-stemmed-nominative-is-reachable-from-an-inflected-query``.

WHAT HAS BEEN FIXED
-------------------
Four faults were measured on production and a fifth was found by this harness.
All five are fixed, and EVERY case below passes with no ``xfail`` anywhere in the
table:

* **tripl-gbxj** (unbounded ``ts_rank_cd`` x4.0, harvested values joined into
  variable keywords) — ``spot`` and ``screen_spot`` now rank their own event
  first instead of the variables that merely mention it. Those two cases pin
  neither half ON ITS OWN, and this used to say otherwise. Deleting
  normalization flag 32 was measured to leave the whole table green, because
  ``rank / (rank + 1)`` only bends outliers and this corpus had none —
  ``repetition-outlier-does-not-outrank-the-screen-it-names`` is the case built
  to close that gap. The keywords half is pinned by ``purchase-plural``:
  restoring ``include_values=True`` in ``_search_documents._variable_document``
  fails THAT case, and nothing measured says it fails these two.
* **tripl-h9x2** (``token_boundary_regex`` refused every query with a space, and
  the surviving tiers compared a spaced query against an underscored title) —
  ``screen spot`` now finds ``screen_spot``.
* **tripl-txcz** (confidence normalized to the top hit) — a keyboard-mash query
  is no longer served as a certain answer.
* **tripl-nh5s** (``tripl_search`` was ``COPY = simple``, which stems in no
  language) — migration ``a7c3e1b9d5f2`` routes ASCII word tokens to an English
  Snowball dictionary and non-ASCII word tokens to a Russian one, so a plural
  reaches its singular and ``экрана спота`` reaches ``экран``. The same issue
  added the 3.25 tier to the boost ladder, without which the stemmer fixed
  RETRIEVAL and left the RANKING where it was: every other tier compares literal
  characters, so a plural query could still only reach the ladder through the
  harvested value that happens to spell the plural.
* **tripl-0qld** (``_event_document`` joined harvested values into an EVENT's
  ``keywords``, so the tier above it rested on a false premise) — the fix
  tripl-gbxj made to ``_variable_document`` was never made to the event builder,
  and ``keywords`` is the column BOTH the 3.5 literal-token tier and the 3.25
  stemmed tier read. ``${property.screen_name}`` is bound to the ``view_id``
  field of ``app_open``, ``screen_home`` and ``screen_settings``, so its
  harvested plurals sat in those three events' keywords and paid them 3.5 for
  ``q='purchases'``, ``q='spots'`` and ``q='уловы'`` — above the 3.25 the
  correctly-named entity earns, and none of the three was named in any
  ``must_not_outrank``. All three plural cases below were green anyway, on the
  trigram leg; that is the point of the entry. The ladder now orders them, the
  three carriers take the 3.0 body tier, and the claim is asserted on the boost
  column itself by :mod:`tripl.tests.relevance.test_keyword_tier_premise` rather
  than inferred from a total score.
* **tripl-9t2s** (nothing in the score rewarded matching MORE of the query) —
  the one fault on this list that no production query reported and that this
  harness found on its own, by ranking ``q='экран спота'`` and reading the
  numbers: ``Экран`` 1.0000, ``screen_spot`` 0.8294. ``COVERAGE_BONUS`` in
  ``_search_query`` pays a flat 1.0 to any document that satisfies the whole
  tsquery, which the AND-semantics of ``websearch_to_tsquery`` make the same
  thing as "answered every term". ``russian-phrase-finds-the-event-it-describes``
  is the case, its ``xfail_ordering`` is what the fix deleted, and the mechanism
  itself — that a one-word document does NOT satisfy a two-word tsquery — is
  asserted with no corpus and no ranking by
  :mod:`tripl.tests.relevance.test_coverage_invariants`.

Two cases (``purchase``, ``улов``) are load-bearing in the opposite direction:
they are the singular halves of the stemming pairs and they already passed on
production before any of this. If a "fix" ever breaks them, the harness has
caught a regression rather than a missing feature — and they are also what proves
the harness discriminates instead of failing everything it is shown. That role
extends to every pair here: singular and plural are asserted side by side, so a
stemmer that over-merges (or a configuration change that quietly reverts to
``simple``) shows up as one half of a pair going red.

WHAT THIS TABLE DOES NOT COVER
------------------------------
Every case here runs with the semantic leg OFF (see
:mod:`tripl.tests.relevance.conftest` — the fixture hard-fails if embeddings are
enabled, because a live provider makes rankings irreproducible). The semantic
leg's own guarantees — the cosine floor and the confidence it reports — are
covered by :mod:`tripl.tests.relevance.test_semantic_floor`, which builds its
vectors by hand and needs no provider.
"""

from __future__ import annotations

from dataclasses import dataclass

# Document titles, which is what a search result carries. Events are titled by
# name, variables by ``${name}``, fields and event types by display name — see
# ``services/_search_documents``.
EVENT_SPOT = "spot"
EVENT_SCREEN_SPOT = "screen_spot"
EVENT_SCREEN_HOME = "screen_home"
EVENT_SCREEN_SETTINGS = "screen_settings"
EVENT_CATCH_REPORT = "catch_report_created"
EVENT_PURCHASE = "purchase_completed"

VAR_SPOT_ID = "${property.spot_id}"
VAR_CUBE = "${property.cube}"
VAR_PAGE_DATA_SPOT_ID = "${page_data.extra.spot_id}"
VAR_SCREEN_NAME = "${property.screen_name}"
VAR_CARD_TARGET = "${property.card_target}"

VAR_SCREEN_KIND = "${property.screen_kind}"

FIELD_SCREEN = "Экран"
FIELD_CATCH_KIND = "Тип улова"
FIELD_CATCH_WEIGHT = "Вес улова"


@dataclass(frozen=True)
class RelevanceCase:
    """One measured query and the ranking it must produce.

    ``expect_top`` is the document title that must come back first. It makes TWO
    claims which are asserted by two different test functions: that the document
    is retrieved at all (never xfailable), and that it is retrieved first
    (xfailable). Nothing in ``must_not_outrank`` may be ranked above it — those
    are the documents that actually beat it on production, so they are named
    rather than implied, and a failure says which competitor won instead of just
    "wrong order".

    ``must_retrieve`` names documents that must appear somewhere in the results,
    with no claim about position. Two uses: a fact about the RETRIEVAL mechanism
    that has no measured ranking to assert, and a competitor whose presence a
    case depends on — ``must_not_outrank`` is vacuously satisfied by a competitor
    that is not returned at all, so a plural case would go green if the wrong
    answer it exists to out-rank silently stopped being indexed.

    ``max_top_confidence`` is the claim about the SCORE rather than the order,
    and it is used in two directions. From tripl-txcz: confidence used to be
    normalized to the top hit, so a query that matched nothing was still served
    as a perfect answer (``garbage-query-...``). From tripl-9t2s: a bound from
    ABOVE on a ranking constant — ``russian-phrase-...`` pins its top hit at
    <= 0.5 so that ``COVERAGE_BONUS`` cannot be enlarged into a blunt instrument
    without a case going red (it bites near 2.7).

    ``xfail_ordering`` is the reason a case's ORDER is EXPECTED to be wrong, or
    ``None``. It excuses nothing else — retrieval and confidence are asserted
    unconditionally, which is what stops a ranking marker from covering a
    retrieval regression (see the module docstring). NO case sets it
    today: the one that did (``russian-phrase-...``, for tripl-9t2s) had it
    deleted by the fix, which is the workflow this field exists for.
    """

    id: str
    query: str
    measured: str
    expect_top: str | None = None
    must_not_outrank: tuple[str, ...] = ()
    must_retrieve: tuple[str, ...] = ()
    max_top_confidence: float | None = None
    xfail_ordering: str | None = None

    def __post_init__(self) -> None:
        """Refuse a case that asserts nothing.

        A case with no ``expect_top``, no ``must_retrieve`` and no
        ``max_top_confidence`` is collected, parametrized, reported as passing
        and checks nothing at all. Cheap to write by accident while editing the
        table, and indistinguishable from coverage in a CI summary.
        """
        if not (self.expect_top or self.must_retrieve or self.max_top_confidence is not None):
            msg = (
                f"case {self.id!r} asserts nothing: set expect_top, must_retrieve "
                f"or max_top_confidence"
            )
            raise ValueError(msg)
        if self.xfail_ordering and not self.expect_top:
            msg = (
                f"case {self.id!r} has xfail_ordering but no expect_top, so the "
                f"marker would be strict-xfail on a test that asserts nothing"
            )
            raise ValueError(msg)


CASES: tuple[RelevanceCase, ...] = (
    RelevanceCase(
        id="spot-event-beats-harvested-variables",
        query="spot",
        measured=(
            "production: ${property.spot_id} 73.69, ${property.cube} 55.68, "
            "${page_data.extra.spot_id} 51.12; the events named 'spot' and "
            "'screen_spot' did not appear at all"
        ),
        # Fixed by tripl-gbxj: ts_rank_cd is normalized (flag 32) so the lexical
        # leg is bounded below the 5.0 exact-title boost, and harvested values
        # are no longer joined into a variable's keywords.
        #
        # WHAT THIS CASE PINS, CORRECTED (tripl-uojz audit)
        # Not flag 32: deleting it was MEASURED to leave this case, and every
        # other outcome in the table, byte-identical — see the comment on
        # `repetition-outlier-does-not-outrank-the-screen-it-names`, which exists
        # because of that measurement. Not the keywords half either, on any
        # evidence recorded here: the mutation named for that half is pinned by
        # `purchase-plural`. What is defensible is narrower and is what this case
        # is now claimed to be — the full tripl-gbxj revert (both mutations at
        # once) puts `${property.spot_id}` back above the event, and the ranking
        # being right today is guarded against future regression.
        expect_top=EVENT_SPOT,
        must_not_outrank=(VAR_SPOT_ID, VAR_CUBE, VAR_PAGE_DATA_SPOT_ID),
    ),
    RelevanceCase(
        id="screen_spot-exact-title-beats-harvested-variables",
        query="screen_spot",
        measured=(
            "production: the 5.0 exact-title boost fires and the screen_spot event "
            "still ranks 3rd at 9.00, behind variables at 11.93 and 10.82"
        ),
        # Fixed by tripl-gbxj: with the lexical leg capped at 4.0, the 5.0
        # exact-title boost can no longer be outweighed by repetition alone.
        # Same audit result as the case above: it pins the two mutations
        # together, not either one alone, and the measurement that says so is on
        # `repetition-outlier-does-not-outrank-the-screen-it-names`.
        expect_top=EVENT_SCREEN_SPOT,
        must_not_outrank=(VAR_SPOT_ID, VAR_CUBE, VAR_PAGE_DATA_SPOT_ID),
    ),
    RelevanceCase(
        id="repetition-outlier-does-not-outrank-the-screen-it-names",
        query="screen_settings",
        measured=(
            "production: the same fault as q='spot' (73.69 vs a correct answer at "
            "4.55) at the scale that actually caused it — 1526 harvested-value "
            "variables, one document repeating the query token ~180 times for a raw "
            "ts_rank_cd of ~17 and a lexical leg of ~68. The QUERY is a harness "
            "construction rather than one of the 26 production queries, and this "
            "field says so rather than inventing a number: the two cases above are "
            "ranked against variables holding four to eight values per context, "
            "which is nowhere near enough repetition for the fix to be observable"
        ),
        # THE MUTATION THIS CASE EXISTS TO FAIL, NAMED EXACTLY
        # Reverting `ts_rank_cd(d.text_vector, q.tsq, 32)` to
        # `ts_rank_cd(d.text_vector, q.tsq)` in
        # `services/_search_query.postgres_lexical_search` must turn this case red.
        # Nothing else in the table can do that: measured on this corpus, deleting
        # the flag left all fourteen other outcomes byte-identical, because flag 32
        # is `rank / (rank + 1)` and therefore only bends OUTLIERS — an ordinary
        # match at raw 0.5 moves from a 2.0 lexical leg to 1.33 and reorders
        # nothing. So the normalization half of tripl-gbxj shipped with no guard,
        # and a contributor deleting the flag would have seen a clean suite. The
        # other half (harvested values out of a variable's `keywords`) has always
        # been pinned by `purchase-plural`; this is its missing twin.
        #
        # THE ARITHMETIC, MEASURED ON THE REAL tripl_search CONFIGURATION
        # `${property.card_target}` (corpus._SETTINGS_CARD_TARGETS: 90 values x 2
        # bindings = 180 occurrences of the `screen`+`settings` cover) has a raw
        # ts_rank_cd of 18.0 for this query, and a boost of 2.25 — the body-LIKE
        # tier, because the values are underscore-joined and `\m...\M` treats `_`
        # as a word character, so no token tier fires on it:
        #
        #   normalized:   4.0 * (18.0/19.0) + 2.25 + 2*0.100 = 6.24
        #   unnormalized: 4.0 * 18.0        + 2.25 + 2*0.100 = 74.45
        #
        # The `screen_settings` EVENT scores 5.0 (exact title) + 2.0 (perfect
        # trigram on that title) + its own lexical leg: 8.33 normalized, 9.00
        # unnormalized. So the fix wins by ~2.1 and the revert loses by ~65 — the
        # verdict is not sitting on a rounding error in either direction. Every
        # other document this query retrieves is a variable bound to the
        # screen_settings event, topping out at ~4.7 on the 3.5 keyword-token tier.
        #
        # The corpus document is invisible to every case above it: its text carries
        # no `spot`, `purchase` or `улов` lexeme, no trigram over the 0.3 threshold
        # for any of their queries, and no substring any of them LIKE-match, so it
        # is not even retrieved by them. See its comment in corpus.py for the
        # bindings that keep the repetitions out of every EVENT document too.
        expect_top=EVENT_SCREEN_SETTINGS,
        must_not_outrank=(VAR_CARD_TARGET,),
    ),
    RelevanceCase(
        id="spaced-query-finds-underscored-event",
        query="screen spot",
        measured="production: screen_spot ranked 5th at 4.545",
        # Fixed by tripl-h9x2: token_boundary_regex folds 'screen spot' into
        # 'screen_spot', so the 3.5 keyword-token tier fires on the event's own
        # name, and every identifier is additionally indexed in its spaced form.
        #
        # AUDIT: NOT VERIFIED EITHER WAY, and this now says so
        # instead of implying otherwise. The 3.25 stemmed tier arrived AFTER
        # tripl-h9x2 (with tripl-nh5s) and fires on this event's own title
        # regardless of the token regex, and pg_trgm scores 'screen_spot' against
        # 'screen spot' at 1.0 because it treats `_` as a separator. So there is
        # a plausible path by which this case stays green with tripl-h9x2
        # reverted. Nobody has run that mutation. Treat the case as a regression
        # guard on today's ranking, not as proof of that issue.
        expect_top=EVENT_SCREEN_SPOT,
        must_not_outrank=(VAR_SPOT_ID, VAR_CUBE, EVENT_SCREEN_HOME),
    ),
    RelevanceCase(
        id="russian-phrase-finds-the-event-it-describes",
        query="экран спота",
        measured=(
            "production: never returned screen_spot at all, though its description "
            "is literally 'Показ экрана спота'"
        ),
        # Fixed by tripl-nh5s, the half tripl-h9x2 could not reach. The regex
        # tiers were made Unicode-aware there, but a regex matches literal
        # characters and the document says 'экрана спота' where the query says
        # 'экран спота'. With the Russian Snowball dictionary mapped to non-ASCII
        # word tokens, both sides stem to 'экра' & 'спот' and the event is
        # RETRIEVED — which is all it needs, since its two competitors survive on
        # trigram similarity alone (a subtitle every pageview event shares, and
        # the 'Экран' field whose title is a substring of the query).
        #
        # `экра` and not `экран`: measured for tripl-uojz, 'экран' and 'экрана'
        # BOTH over-stem to 'экра'. The two meet on the stem leg, which is why
        # this case is evidence about tripl-nh5s and says nothing about the
        # surface leg in either direction.
        expect_top=EVENT_SCREEN_SPOT,
        must_not_outrank=(EVENT_SCREEN_HOME, FIELD_SCREEN),
        # THE COVERAGE FAULT, AND THE ARITHMETIC THAT CLOSED IT
        # This case carried an `xfail_ordering` and no longer does. Measured on
        # this harness, whole result set, three documents:
        #
        #   before   Экран 1.0000   screen_spot 0.8294   spot 0.4799
        #   after    screen_spot 1.8294   spot 1.4799   Экран 1.0000
        #
        # (The issue records 1.000/0.791/0.433. The two event scores moved when
        # tripl-0qld took harvested values out of event keywords and shrank their
        # lexical legs; the fault and the ORDER were identical.)
        #
        # `Экран` is a FIELD document whose entire title is ONE of the query's two
        # words. It holds no Cyrillic `спот`, so it satisfies no tsquery and earns
        # no ladder tier: its whole score is the trigram leg, and
        # similarity('Экран','экран спота') is exactly 0.5 — 6 trigrams of 12, all
        # shared — so 2.0 x 0.5 = 1.0000 to the digit. `screen_spot` is the event
        # the user wants: its description is literally 'Показ экрана спота', which
        # lives in `body`, so it answered BOTH words — and its boost is 0 too,
        # because the 3.25 stemmed tier deliberately never reads `body`. Nothing
        # in the score paid it for that, so the complete match lost by 0.17.
        # `COVERAGE_BONUS` pays a flat 1.0 for satisfying the whole tsquery; the
        # field document gets nothing because it satisfies none of it.
        #
        # WHAT EACH FIELD BELOW PINS, AND WHY THREE ARE NEEDED
        # * `expect_top` + `must_not_outrank` — the ordering claim, and the
        #   acceptance criterion for tripl-9t2s. MUTATION: delete
        #   `+ coverage_score` from the outer SELECT in postgres_lexical_search
        #   and screen_spot falls back to 0.8294 against Экран at 1.0000.
        # * `must_retrieve=(FIELD_SCREEN,)` — out-ranking `Экран` is only a real
        #   claim while `Экран` is still RETRIEVED. `must_not_outrank` is
        #   vacuously satisfied by an absent competitor, so a regression in the
        #   trigram retrieval leg (raising the `%` threshold, dropping
        #   `d.title % :query` from the WHERE) would turn the ordering claim green
        #   for free. This is the assertion that refuses that.
        # * `max_top_confidence=0.5` — the bound from ABOVE on the constant.
        #   1.8294 / 7.0 = 0.2613: a complete but WEAK match is not a certain
        #   answer. MUTATION: set COVERAGE_BONUS = 4.0 and the top hit is served
        #   at 0.684, so the fix cannot be enlarged into a blunt instrument.
        #
        # THE (NOW DELETED) MARKER COVERED THE ORDER AND NOTHING ELSE
        # `screen_spot` going ABSENT from these results — the production fault
        # itself, returning — was asserted against unconditionally by the
        # retrieval test, which reads no xfail field. Until that split, the marker
        # excused both claims at once, so the four fixes could have been undone
        # and this case would still have reported a tidy expected failure.
        must_retrieve=(FIELD_SCREEN,),
        max_top_confidence=0.5,
    ),
    RelevanceCase(
        id="purchase-singular",
        query="purchase",
        measured="production: 19.565, correct entity on top — the control for the pair below",
        # AUDIT: pins NOTHING, by design, and that is its job. It
        # passed before any fix and must keep passing after every one; it is what
        # shows the harness discriminates instead of failing whatever it is
        # shown. `stem('purchase') == 'purchas'` for the query and the entity
        # alike, so no stemming change can separate them.
        expect_top=EVENT_PURCHASE,
        must_not_outrank=(VAR_SCREEN_NAME,),
    ),
    RelevanceCase(
        id="purchase-plural",
        query="purchases",
        measured="production: q='purchase' 19.565 vs q='purchases' 5.160",
        # Fixed by tripl-nh5s. Both halves of that issue are needed here and the
        # case is the reason to say so: the English Snowball dictionary stems
        # 'purchases' and 'purchase_completed' to the same 'purchas', which gets
        # the event RETRIEVED, but the harvested screen name literally spells
        # 'purchases' in its body and was still collecting the 3.0 body-token
        # boost the event could not reach.
        #
        # WHAT THIS COMMENT USED TO CLAIM, AND WHAT WAS ACTUALLY HAPPENING
        # It said the 3.25 stemmed tier "is what puts the event back on top". On
        # the ladder it did not. `${property.screen_name}` is bound to the
        # `view_id` field of `app_open`, `screen_home` and `screen_settings`, and
        # `_search_documents._event_document` joined a bound variable's harvested
        # values into an EVENT's keywords as well as its body — so those three
        # unrelated pageview events spelled `purchases` in their KEYWORDS and
        # took the 3.5 literal keyword-token tier, a quarter of a point ABOVE the
        # 3.25 `purchase_completed` earns. This case was green regardless,
        # because the trigram leg (similarity('purchase_completed','purchases')
        # ~ 0.38 -> +0.76) covered the 0.25 the ladder had given away. A green
        # ordering case sitting on top of an inverted ladder is exactly the kind
        # of evidence this table has been wrong about before.
        #
        # tripl-0qld removed the harvested values from event keywords, so the
        # three carriers now take the 3.0 body-token tier and the TIER ORDERING
        # decides this case. That claim is not re-derived from the total score
        # here — it is asserted on the boost column directly by
        # `test_keyword_tier_premise.test_a_harvested_value_does_not_buy_an_event
        # _the_keyword_tier`, which is where to look when this goes red.
        #
        # AUDIT: this one does pin what it claims. Without the
        # stemmer, `purchases` reaches `purchase_completed` by no path at all —
        # not the tsvector, not `LIKE '%purchases%'` against a document spelling
        # `purchase_completed`, not trigram similarity between those two strings.
        expect_top=EVENT_PURCHASE,
        must_not_outrank=(VAR_SCREEN_NAME,),
        # The wrong answer has to BE there for out-ranking it to mean anything:
        # `must_not_outrank` is satisfied by a competitor that is simply absent.
        must_retrieve=(VAR_SCREEN_NAME,),
    ),
    RelevanceCase(
        id="ulov-singular",
        query="улов",
        measured="production: 3.006 with the catch-report events on top — the control",
        # WHAT THIS CASE IS NOW, AND WHY THE `measured` LINE ABOVE IS HISTORY
        # It was measured on the PRE-stemming configuration, where `улов` matched
        # the corpus literally. On the deployed database it would not pass today:
        # `tripl_search` stems the bare nominative to `ул`, a lexeme none of the
        # 151 production rows holding this word contain, so the stemmed query
        # reaches none of them. Reading this line as "production is fine here" is
        # exactly the inversion tripl-uojz is about.
        #
        # THE SEED IT ASSERTS ON WAS ANTI-REPRESENTATIVE AND HAS BEEN FIXED
        # `catch_report_created` used to carry the bare nominative twice in its
        # description and once as its `catch_kind` value, ALONGSIDE the inflected
        # forms in `Отчёт об улове` / `Тип улова`. So the one document both ulov
        # cases assert on held BOTH lexical classes, each query retrieved it by a
        # different lexeme, and the split between them was unobservable. The seed
        # now carries inflected forms only (see corpus.py).
        #
        # IT STILL DOES NOT PIN tripl-uojz, AND SAYING SO IS THE POINT
        # A Russian nominative is a PREFIX of its own inflections, so the WHERE
        # clause's `LIKE '%улов%'` matches `улове` and retrieves this document
        # whether or not the surface leg exists. What the fix changes here is the
        # BOOST — 3.25 stemmed tier instead of 2.25 body-LIKE — i.e. an ordering
        # margin, which is the weakest evidence this harness produces and the
        # kind it has already been wrong about twice. The mechanism is pinned by
        # `test_stemming_invariants.py` (no corpus, no ranking) and, through
        # retrieval, by `over-stemmed-nominative-is-reachable-from-an-inflected-
        # query` below.
        # THE EXPECTATION FLIPPED, BY AN OWNER DECISION, NOT BY A GREEN RUN
        # (2026-08-16)
        # This case asserted that the EVENT wins. Weighting the title (setweight
        # 'A') puts the two fields whose names ARE the query above it:
        #
        #     q='улов'  Вес улова 7.238  Тип улова 7.238
        #               Отчёт об улове 7.044  catch_report_created 6.678
        #
        # `catch_report_created` is spelled in English and gains nothing from a
        # Russian query; the fields are literally called "catch type" and "catch
        # weight". Whether a field titled exactly the query should outrank an
        # event that only describes it is a PRODUCT question, not an
        # implementation detail — quietly updating the expectation to match new
        # output is how a regression gets laundered — so it was put to the owner,
        # who chose the field: "I typed a name, give me the thing with that name".
        #
        # WHY `Вес улова` AND NOT `Тип улова`
        # The two fields score IDENTICALLY (7.238 both), because both titles
        # carry the same one matching word. The order between them is the
        # tie-break in the ranking SQL, `ORDER BY score DESC, title ASC`, and
        # 'В' precedes 'Т'. Pinning it here pins that tie-break; if a future
        # change separates the two scores this case will say so rather than
        # silently accepting either.
        #
        # WHAT IS DELIBERATELY UNCHANGED: `${property.screen_name}`, the
        # harvested-value variable, still must not outrank. That was the original
        # tripl-gbxj fault and it is not what the owner decided about.
        expect_top=FIELD_CATCH_WEIGHT,
        must_not_outrank=(VAR_SCREEN_NAME,),
        must_retrieve=(EVENT_CATCH_REPORT, FIELD_CATCH_KIND),
    ),
    RelevanceCase(
        id="ulov-plural",
        query="уловы",
        measured="production: max 0.900, every catch-report event gone",
        # Fixed by tripl-nh5s, Russian half. 'уловы', 'улове' and 'улова' all
        # stem to 'улов', so the event, its 'Отчёт об улове' type and the 'Тип
        # улова' field are all retrieved by the plural for the first time. The
        # event carries 'улове' in the type name folded into its keywords, so it
        # takes the 3.25 tier while the harvested screen name — whose keywords
        # lost their values to tripl-gbxj — is left on its literal 3.0.
        #
        # WHAT THIS PAIR DOES NOT COVER
        # It cannot detect the over-stemming fault, in either direction, and it
        # certified a fix that was wrong on production — the harness's third
        # false green. The corpus document both cases asserted on carried the
        # bare nominative 'улов' AND the inflected 'улове'/'улова', so its
        # tsvector held the lemma no matter which side produced it and both
        # queries retrieved it by a different lexeme without ever meeting. That
        # seed is fixed (corpus.py), which makes the pair honest; it does not
        # make it discriminating. This half in particular never could be:
        # 'уловы' stems to 'улов' and the document has held 'улов' since
        # a7c3e1b9d5f2, so it is retrieved on the stem leg alone. Green here is
        # not evidence about tripl-uojz and never was.
        # SAME FLIP, SAME DECISION. The plural behaves like the
        # singular once the title carries weight:
        #
        #     q='уловы'  Вес улова 7.178  Тип улова 7.178
        #                Отчёт об улове 7.011  catch_report_created 6.645
        #
        # The stemming property this case exists for is untouched: 'уловы',
        # 'улове' and 'улова' still all stem to 'улов', so the event, its type
        # and both fields are all RETRIEVED by the plural — which is what
        # tripl-nh5s fixed and what `must_retrieve` below now pins explicitly
        # instead of leaving implied by a first-place assertion that has moved.
        expect_top=FIELD_CATCH_WEIGHT,
        must_not_outrank=(VAR_SCREEN_NAME,),
        must_retrieve=(VAR_SCREEN_NAME, EVENT_CATCH_REPORT, FIELD_CATCH_KIND),
    ),
    RelevanceCase(
        id="spots-plural",
        query="spots",
        measured="production: q='spots' never returns 'spot' or 'screen_spot'",
        # Fixed by tripl-nh5s. 'spots' stems to 'spot', which every spot_* value
        # in the corpus already indexes, so the event named 'spot' is retrieved
        # and its title stems to the query — the 3.25 tier — while the harvested
        # screen name spelled 'spots' keeps only its literal body tier. This is
        # also the case that proves the tier discriminates rather than lifting
        # everything: 'screen_spot' stems to 'spot' too and takes the same tier,
        # and 'spot' still wins it on trigram similarity to the query.
        #
        # AUDIT: pins what it claims, with one caveat worth naming.
        # Without the stemmer the `spot` event IS still retrieved — trigram
        # similarity between 'spot' and 'spots' is ~0.57, well over the 0.3
        # threshold — but it earns no boost at all, and `${property.screen_name}`
        # spells 'spots' literally and takes 3.0. So the mutation is caught in
        # the ORDER here, not in the retrieval set.
        expect_top=EVENT_SPOT,
        must_not_outrank=(VAR_SCREEN_NAME,),
        must_retrieve=(VAR_SCREEN_NAME,),
    ),
    RelevanceCase(
        id="over-stemmed-nominative-is-reachable-from-an-inflected-query",
        query="экране",
        measured=(
            "production: экран/экра = 414/3971 search_documents rows, and the forms "
            "themselves stem экран -> 'экра', экрана -> 'экра', экраны -> 'экра', "
            "экране -> 'экран'. So the MAJORITY of documents holding this word sit on "
            "'экра', and the only measured query form that lands in the other class — "
            "the one that cannot reach them without the surface leg — is 'экране'. The "
            "QUERY is a harness construction over a corpus seed built to the measured "
            "shape, not one of the 26 production queries, and this field says so rather "
            "than inventing a number"
        ),
        # THE ONE CORPUS CASE THAT PINS tripl-uojz THROUGH RETRIEVAL
        # `${property.screen_kind}` is the only document in this corpus that holds
        # a bare nominative and NO inflected form of the same word (see
        # corpus._SCREEN_KINDS for how it is kept that way and why the word is
        # `экран` rather than `улов`). Its lexemes for this word are `экра` from
        # the stem leg and `экран` from the surface leg; `q='экране'` asks for
        # `экран` on the stem leg and `экране` on the surface leg. The single
        # point of contact is stem(query) == surface(document) — the identity the
        # whole fix is built on — so removing the surface leg from EITHER side
        # makes this document unreachable.
        #
        # WHY `экране` AND NOT `экрана`, WHICH IS WHAT THIS CASE FIRST SHIPPED WITH
        # `экрана` stems to `экра`, the SAME lexeme the seeded `экран` stems to, so
        # the two met on the stem leg alone and this case passed with the whole fix
        # reverted — a fourth false green, in the very case written to end them.
        # Measured: экран, экрана and экраны all stem to `экра`; only `экране`
        # stems to `экран`. The split in this word is prepositional-vs-the-rest,
        # NOT nominative-vs-genitive, which is the guess that produced the broken
        # version and will produce it again if this comment is deleted.
        #
        # WHY NO OTHER PATH CAN SUPPLY THE ANSWER, WHICH IS THE WHOLE VALUE
        # `postgres_lexical_search` retrieves on five conditions and four of them
        # are closed here by construction: harvested values are body-only since
        # tripl-gbxj so `keywords % :query` sees no Cyrillic; `title` is
        # `${property.screen_kind}` and `subtitle` is `string`, neither of which
        # is trigram-similar to a Russian word; and `LIKE '%экране%'` cannot match
        # a document whose longest spelling of the word is the five letters
        # `экран`. This is the difference between this case and every other
        # Russian case in the table, all of which the LIKE tier retrieves anyway.
        #
        # NO ORDERING CLAIM, ON PURPOSE
        # There is no production measurement of where this document SHOULD rank
        # for this query, and the corpus is full of documents that legitimately
        # beat it. Asserting a position would be fitting to a sample; asserting
        # retrieval is asserting the mechanism, and `must_retrieve` is not
        # xfailable.
        must_retrieve=(VAR_SCREEN_KIND,),
    ),
    RelevanceCase(
        id="garbage-query-is-not-a-confident-answer",
        query="asdkjhasd",
        measured="production: served at confidence 1.0 on an absolute score of 0.636",
        # No ordering expectation on purpose: there is no right answer to this
        # query. The claim under test is that whatever comes back must not be
        # presented as certain.
        # Fixed by tripl-txcz: confidence is a fraction of an ABSOLUTE reference
        # score (_FULL_CONFIDENCE_SCORE) instead of a fraction of the top hit,
        # so the best of a bad set is no longer 1.0 by construction. The same
        # issue put a cosine floor under the semantic leg, which is the other
        # way a junk query used to acquire confident-looking results.
        #
        # AUDIT: pins what it claims. Reverting confidence to a
        # fraction of the top hit makes the top hit 1.0 by construction, which is
        # twice this bound. The one way it could go quietly vacuous is the corpus
        # ceasing to retrieve anything for this query — `${property.session_key}`
        # losing the value that contains 'asdkjhasd' — and the assertion in the
        # test names that fixture in its failure message for exactly that reason.
        max_top_confidence=0.5,
    ),
)
