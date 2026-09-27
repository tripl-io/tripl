"""weight the title in the search vector, and pin everything else at D

Revision ID: b2d3f4a5c6e7
Revises: c1d2e3f4a5b6
Create Date: 2026-08-16 22:10:00.000000

tripl-dito. ``setweight`` had never been used anywhere in this product, so every
lexeme in every stored vector was weight D and ``ts_rank_cd`` scaled all of them
by the same 0.1 out of its default ``{0.1, 0.2, 0.4, 1.0}``. The lexical leg
could not distinguish a title match from a body match at all.

WHAT THAT COST, MEASURED
------------------------
1305 inverted pairs out of 8224 on the FINAL score (16%), and three of them are
real production searches on acme-ios main::

    q            the document that won        the one that should have
    paywall      ${property.newValue}         a paywall event      (gap 0.92)
    search       ${property.query}            spotlight_search     (gap 0.51)
    экран        spot_screen_community_...    Экран                (gap 0.71)

Each loser is a harvested-value variable that happens to hold the query inside
its text; each winner is the entity actually NAMED by it.

WHY TITLE-ONLY, AND WHY THE REST IS PINNED AT D EXPLICITLY
-----------------------------------------------------------
Three variants were measured against those three inversions, with weighted
vectors built on the fly against the real rows so the choice was made at the
real operating point rather than at an assumed rank::

    variant                                    paywall   search   экран
    V1  title A, keywords B, subtitle C, body D  +0.689 NO  +0.879 ok  +1.478 ok
    V2  title A, keywords C, subtitle C, body D  +0.751 NO  +0.877 ok  +1.257 ok
    V3  title A, everything else D               +0.900 ~   +0.947 ok  +1.118 ok

The intuitive variant loses. Weighting ``keywords`` lifts the OFFENDER almost as
much as the victim, because ``_search_documents._variable_document`` writes the
bound event's name into a variable's keywords — ``${property.query}`` holds
``search`` from ``spotlight_search`` — so the harvested-value documents in the
fault carry the query in exactly the column being promoted. V3 closes all three
and is also the simplest thing that could work.

``'D'`` is stated rather than left to the default because that is what makes the
blast radius checkable: a document with NO title match is scored exactly as it
was. Measured on the three offenders above, their final scores moved 0.000.

WHAT THIS DOES NOT CHANGE
-------------------------
The lexical leg stays bounded. ``postgres_lexical_search`` calls ``ts_rank_cd``
with normalization flag 32 (``rank/(rank+1)``), so ``lexical_score`` cannot
exceed 1.0 however far an A-weighted raw rank climbs, and the boost ladder it is
weighed against is untouched by this revision. What changes is discrimination
INSIDE the leg, which is the entire point.

REWRITING THE WHOLE TABLE IS THE POINT, NOT A SIDE EFFECT
----------------------------------------------------------
``text_vector`` stores the weights, so an unweighted row stays unweighted until
something rewrites it. Leaving old rows alone would rank half the corpus one way
and half the other with no error anywhere — the same silent split
``b6d1f0a3c7e2`` was written to avoid. ``search_service.TEXT_VECTOR_EXPRESSION``
holds the live copy of the expression below and
``tests/test_alembic_revisions.py`` asserts the two are byte-identical, because a
convention is not a check.
"""

from __future__ import annotations

from alembic import op

revision: str = "b2d3f4a5c6e7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | None = None
depends_on: str | None = None


#: Frozen copy of ``search_service.TEXT_VECTOR_EXPRESSION`` as of this revision.
#: Duplicated on purpose and never imported: a migration must keep meaning what
#: it meant on the day it ran, whatever the application later does.
_TEXT_VECTOR_EXPRESSION = """
    setweight(to_tsvector('tripl_search', coalesce(title, '')), 'A')
    || setweight(to_tsvector('tripl_search_surface', coalesce(title, '')), 'A')
    || setweight(
        to_tsvector('tripl_search', concat_ws(' ', subtitle, body, keywords)),
        'D'
    )
    || setweight(
        to_tsvector(
            'tripl_search_surface',
            concat_ws(' ', subtitle, body, keywords)
        ),
        'D'
    )
"""

_REBUILD_TEXT_VECTORS = f"""
    UPDATE search_documents
    SET text_vector = {_TEXT_VECTOR_EXPRESSION}
"""

#: The unweighted two-leg expression ``b6d1f0a3c7e2`` wrote, frozen here so
#: ``downgrade()`` restores exactly what ``upgrade()`` found — not a fresh
#: opinion about what that revision ought to have said.
_REBUILD_UNWEIGHTED_TEXT_VECTORS = """
    UPDATE search_documents
    SET text_vector = to_tsvector(
        'tripl_search',
        concat_ws(' ', title, subtitle, body, keywords)
    )
    || to_tsvector(
        'tripl_search_surface',
        concat_ws(' ', title, subtitle, body, keywords)
    )
"""


def upgrade() -> None:
    op.execute(_REBUILD_TEXT_VECTORS)


def downgrade() -> None:
    op.execute(_REBUILD_UNWEIGHTED_TEXT_VECTORS)
