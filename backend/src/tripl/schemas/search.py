from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

from tripl.models.variable_value import VariableValueKind

SearchEntityType = Literal[
    "event",
    "event_type",
    "field",
    "meta_field",
    "variable",
    "relation",
    "tag",
    "metric",
    "fact_table",
    # Project-scoped configuration, indexed exactly the way metrics and fact
    # tables already are: they carry project_id and no branch_id, so every
    # branch's index holds a copy (tripl-dfct).
    "scan_config",
    "alert_rule",
    # Docs catalog notes (F22): the project's own and its organization's. Not
    # branched either, so every branch's index carries a copy.
    "doc",
]


class SearchEventVariableValue(BaseModel):
    id: uuid.UUID
    variable_id: uuid.UUID
    variable_name: str
    field_definition_id: uuid.UUID
    field_name: str
    field_display_name: str
    source_column: str
    value_kind: VariableValueKind
    observed_count: int
    values: list[str] = []


class SearchVariant(BaseModel):
    """One folded member of a :class:`SearchVariantGroup` (JR-20).

    Deliberately slim — enough to render and open the event, not a second full
    :class:`SearchResult`: a group of 40 scan variants would otherwise carry 40
    copies of snippets, highlights and variable bindings nobody reads until the
    group is expanded, and then only the name.
    """

    id: uuid.UUID
    entity_id: uuid.UUID
    event_id: uuid.UUID | None = None
    title: str
    # The value this member substituted for the group's placeholder.
    value: str
    route_path: str
    score: float
    confidence: float = 0.0


class SearchVariantGroup(BaseModel):
    """Event hits folded under their best-ranked member (JR-20).

    Events of ONE event type whose names differ only in the value substituted
    for ONE naming-rule placeholder — the scan's ``event_name_format``, or the
    default ``column=value | column=value`` name a scan without one writes.
    ``search_service.group_event_variants`` owns the rule.
    """

    # Stable for the same group across two searches: the event type plus the
    # name with the varying placeholder left in, e.g.
    # ``<type id>:event_name=Home Screen View | screen={screen}``.
    key: str
    # The name with the varying value replaced by ``{placeholder}``.
    pattern: str
    placeholder: str
    # Every member INCLUDING the representative the group is attached to, so
    # ``count - 1 == len(variants)``. Members are folded from the retrieval
    # window only: when the response is ``truncated`` the count is a lower
    # bound, since more variants may rank past the window.
    count: int = Field(ge=2)
    # The other members, best-ranked first.
    variants: list[SearchVariant]


class SearchResult(BaseModel):
    id: uuid.UUID
    entity_type: SearchEntityType
    entity_id: uuid.UUID
    parent_event_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    name: str | None = None
    implemented: bool | None = None
    variable_values: list[SearchEventVariableValue] = []
    title: str
    subtitle: str = ""
    description: str = ""
    snippet: str = ""
    route_path: str
    score: float
    # How certain this result is, in [0, 1] — an ABSOLUTE property of the
    # result, NOT a fraction of the top hit of this response (tripl-txcz).
    # Surfaced in the UI as a percentage / colored badge, so it has to mean the
    # same thing across two different searches: a query that matched nothing
    # well comes back low instead of being 1.0 by construction.
    # ``_search_query.finalize_results`` computes it; see that docstring for the
    # two certainties it is the maximum of (the absolute score ladder, and — for
    # a result the semantic leg produced — that leg's cosine similarity).
    confidence: float = 0.0
    highlights: list[str] = []

    # Whether the MEANING LEG is why this row is here — the keyword leg's own
    # ranked candidate window did not hold it (tripl-wkwv.3).
    #
    # NOT "no keyword matched this", and the difference is reachable: the lexical
    # leg is itself a ``LIMIT``-ed scan, so a WEAK keyword match — a stem-only
    # tsquery hit pays the coverage bonus and no boost tier at all — can be
    # displaced from that leg's window and then re-enter through the vector leg,
    # which sets this True on a row the keyword index did match. Every consumer
    # is worded as "the keyword ranking did not surface this" for that reason.
    # Projecting the lexical predicate back onto the semantic rows would settle
    # it, at the cost of a second copy of that WHERE clause on every semantic
    # query — too much for a provenance label.
    #
    # Narrower than the envelope's ``SearchResponse.semantic_used``, which says
    # only that the leg RAN. A hit the lexical ladder produced reads False even
    # when the vector leg also ranked it and even when its ``confidence`` is then
    # taken from that leg's cosine — the two are different questions, and the
    # vector leg is a ``LIMIT``-ed kNN scan that returns rows for any query, so
    # "the vector leg also listed this row" is a fact about the window rather
    # than about the row. The palette paints a `semantic` chip from this, which
    # is a claim about WHY the reader is looking at the row.
    #
    # Do NOT "fix" this to track ``_semantic_cosine`` below: that cosine is still
    # recorded on a hybrid row so ``finalize_results`` can report the stronger of
    # the two certainties (tripl-txcz), and tripl-d5u8 deliberately lets only an
    # identity match be painted as certain. Confidence and provenance disagreeing
    # on one row is the intended shape, not a bug.
    semantic_used: bool = False
    # Set on the representative of a folded variant group, and only when the
    # caller asked for grouping (``group_variants=true``); None everywhere else.
    variant_group: SearchVariantGroup | None = None

    # Cosine similarity of the semantic leg for this result in [0, 1], or None
    # when that leg did not contribute to it.
    #
    # PRIVATE, and deliberately NOT a response field (tripl-txcz): it exists
    # only so ``finalize_results`` can express a semantic hit's certainty on the
    # same 0-1 scale as a lexical one. The merged ``score`` cannot carry that
    # information, because a vector-only hit is scored ``cosine * 2.5`` for
    # RANKING — read confidence off the score alone and a PERFECT cosine is
    # reported as 2.5 / 7.0 = 0.357, i.e. a semantic rescue served as
    # barely-a-guess. Keeping it off the wire means no new field for API clients
    # or the frontend TS types to track.
    _semantic_cosine: float | None = PrivateAttr(default=None)

    @property
    def semantic_cosine(self) -> float | None:
        """The semantic leg's cosine for this result, or ``None``."""
        return self._semantic_cosine

    def record_semantic_cosine(self, cosine: float) -> None:
        """Remember the semantic leg's cosine, clamped into [0, 1]."""
        self._semantic_cosine = max(0.0, min(1.0, cosine))

    # Whether the lexical leg matched this document BY IDENTITY — its title or
    # its keywords being the query, rather than merely containing it (tripl-d5u8).
    #
    # PRIVATE for the same reason as the cosine above: it exists so
    # ``finalize_results`` can enforce one dialect-independent rule — only an
    # identity match may be painted as a certain answer — without adding a field
    # for API clients to track. Each dialect answers the question its own way,
    # because their ladders are on different scales: Postgres asks whether the
    # boost tier reached the 4.0 exact-keywords rung, SQLite whether the tier
    # score reached its own 7.2 equivalent.
    #
    # Defaults to False so a result that never went through a lexical ladder at
    # all — a semantic-only hit — is not silently treated as an identity match.
    # Its cosine contributes to confidence, subject to the non-identity ceiling.
    _identity_match: bool = PrivateAttr(default=False)

    @property
    def identity_match(self) -> bool:
        """Whether the lexical leg matched this document's identity, not its text."""
        return self._identity_match

    def record_identity_match(self, *, identity: bool) -> None:
        """Remember whether the lexical ladder placed this result on an identity tier."""
        self._identity_match = identity


class SearchResponse(BaseModel):
    items: list[SearchResult]
    # The number of hits IN THIS RESPONSE, not a catalog-wide count
    # (tripl-wkwv.3). ``/search`` takes no ``offset`` and cannot be paged, and a
    # real count is not definable for a fused retrieval anyway: the retrieved set
    # is the union of the lexical predicate and every document above the vector
    # leg's cosine floor, and that second half is a kNN tail that fills its
    # window for any query — a ``count(*)`` over the union would report roughly
    # every embedded document as a "match". To see more hits, raise ``limit``.
    #
    # With ``group_variants=true`` it counts ROWS — a folded group is one — and
    # ``limit`` pages rows too, so folded members never eat into the page (JR-20).
    total: int
    # True when ranked hits exist that this response does not carry.
    #
    # Observed, not guessed: ``search_project`` retrieves one row PAST its
    # candidate window, so "the window filled" and "that was everything" are
    # distinguishable and only the first sets this (tripl-wkwv.3). A full page is
    # not enough on its own — a query matching exactly ``limit`` documents fills
    # the page while carrying every hit there is.
    #
    # With ``semantic_used: true`` the ranked tail is effectively unbounded, so
    # this is normally True and it is ``confidence`` — not this flag — that
    # decides whether the tail is worth fetching. It becomes genuinely
    # discriminating on a lexical-only answer, which is where "did I see
    # everything" is answerable at all.
    truncated: bool = False
    # Whether the semantic leg RAN. Deliberately wider than the per-hit
    # ``SearchResult.semantic_used``; the two disagreeing on one response is the
    # normal case (tripl-wkwv.3).
    semantic_used: bool = False


class SearchReindexResponse(BaseModel):
    documents_indexed: int = Field(ge=0)
    embeddings_scheduled: bool = False
