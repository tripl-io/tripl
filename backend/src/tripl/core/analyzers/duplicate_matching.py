"""Near-duplicate matching over a catalog of event names (GH #265, F12).

Pure, like :mod:`name_similarity` beneath it: the async API service and the
sync scan dry run both hand it plain rows. Comparing every pair is quadratic,
so a :class:`NameIndex` BLOCKS first — only names sharing a core token, the
same token bag, or the same four-letter start are scored at all. A pair that
shares none of those cannot reach the duplicate threshold on the lexical leg,
and :func:`name_similarity.combined_score` never lets an embedding lift a pair
from below :data:`MIN_LEXICAL_FOR_EMBEDDING`.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from tripl.core.analyzers.name_rules import NameRule, compile_rule
from tripl.core.analyzers.name_similarity import (
    EMBEDDING_WEIGHT,
    FILLER_TOKENS,
    PreparedName,
    combined_score,
    normalise,
    prepare_name,
    prepared_score,
)

#: The default duplicate threshold on the combined score (owner decision).
DUPLICATE_THRESHOLD = 0.88
#: The lowest lexical score an embedding cosine of 1.0 can lift to the threshold.
MIN_LEXICAL_FOR_EMBEDDING = round(
    (DUPLICATE_THRESHOLD - EMBEDDING_WEIGHT) / (1 - EMBEDDING_WEIGHT), 4
)
#: A blocking key shared by more names than this is too common to narrow
#: anything ("view"); the bag key is always used.
MAX_POSTING = 400
#: At most this many duplicates are returned per candidate.
TOP_DUPLICATES = 3
#: A single run creating more than this many names that differ in one slot
#: is a combinatorial explosion.
EXPLOSION_MIN_NAMES = 50
#: The most pairs one clustering pass scores. Blocking keeps a real catalog far
#: below this; a pathological one (thousands of names sharing a token bag) gets
#: a partial answer flagged ``truncated`` instead of a request that never ends.
MAX_SCORED_PAIRS = 200_000

Cosine = Callable[[uuid.UUID], float | None]


@dataclass(frozen=True)
class CatalogName:
    event_id: uuid.UUID
    name: str
    event_type_id: uuid.UUID
    status: str = "live"


@dataclass(frozen=True)
class Match:
    event_id: uuid.UUID
    name: str
    event_type_id: uuid.UUID
    status: str
    lexical: float
    score: float
    same_type: bool


def _blocking_keys(tokens: Sequence[str]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = {("bag", " ".join(sorted(set(tokens))))}
    core = [t for t in tokens if t not in FILLER_TOKENS] or list(tokens)
    keys.update(("tok", t) for t in core)
    joined = "".join(tokens)
    if len(joined) >= 4:
        keys.add(("p4", joined[:4]))
    return keys


def _slot_scored(a: PreparedName, b: PreparedName) -> bool:
    return a.slots is not None and b.slots is not None and len(a.slots) == len(b.slots)


@dataclass
class NameIndex:
    """Catalog names blocked for fast neighbour lookup.

    ``formats_by_type`` holds each event type's naming rule
    (``event_name_format``) so a pair under one rule is scored slot by slot
    (:func:`name_similarity.rule_aware_score`). Every entry is normalised and
    split ONCE here; scoring reads the cached tokens.
    """

    entries: list[CatalogName]
    formats_by_type: Mapping[uuid.UUID, str] = field(default_factory=dict)
    _rules: dict[uuid.UUID, NameRule] = field(init=False, default_factory=dict)
    _prepared: list[PreparedName] = field(init=False, default_factory=list)
    _keys: list[set[tuple[str, str]]] = field(init=False, default_factory=list)
    _postings: dict[tuple[str, str], list[int]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        for type_id, name_format in self.formats_by_type.items():
            rule = compile_rule(name_format)
            if rule is not None:
                self._rules[type_id] = rule
        postings: dict[tuple[str, str], list[int]] = defaultdict(list)
        for position, entry in enumerate(self.entries):
            prepared = prepare_name(entry.name, self._rules.get(entry.event_type_id))
            keys = _blocking_keys(prepared.whole.tokens)
            self._prepared.append(prepared)
            self._keys.append(keys)
            for key in keys:
                postings[key].append(position)
        self._postings = dict(postings)

    def rule_for(self, event_type_id: uuid.UUID | None) -> NameRule | None:
        return self._rules.get(event_type_id) if event_type_id is not None else None

    def _neighbours_by_keys(self, keys: Iterable[tuple[str, str]]) -> set[int]:
        found: set[int] = set()
        for key in keys:
            posting = self._postings.get(key, [])
            if key[0] != "bag" and len(posting) > MAX_POSTING:
                continue
            found.update(posting)
        return found

    def neighbours(self, name: str) -> set[int]:
        return self._neighbours_by_keys(_blocking_keys(normalise(name)))

    def neighbours_of(self, position: int) -> set[int]:
        """Neighbours of a catalog entry, from its cached blocking keys."""
        return self._neighbours_by_keys(self._keys[position])

    def slot_scored(self, first: int, second: int) -> bool:
        """Whether two entries are compared value by value under their type's rule."""
        return self.entries[first].event_type_id == self.entries[
            second
        ].event_type_id and _slot_scored(self._prepared[first], self._prepared[second])

    def score_positions(self, first: int, second: int) -> float:
        """Two catalog entries; slot by slot when they share a type with a rule."""
        a, b = self._prepared[first], self._prepared[second]
        if self.entries[first].event_type_id != self.entries[second].event_type_id:
            a, b = PreparedName(a.whole), PreparedName(b.whole)
        return prepared_score(a, b)

    def score_pair(self, name: str, event_type_id: uuid.UUID | None, position: int) -> float:
        """An outside ``name`` of ``event_type_id`` against one catalog entry."""
        entry = self._prepared[position]
        same_type = event_type_id is not None and (
            event_type_id == self.entries[position].event_type_id
        )
        candidate = prepare_name(name, self.rule_for(event_type_id) if same_type else None)
        return prepared_score(candidate, entry if same_type else PreparedName(entry.whole))

    def find(
        self,
        name: str,
        event_type_id: uuid.UUID | None,
        *,
        threshold: float = DUPLICATE_THRESHOLD,
        exclude: Iterable[uuid.UUID] = (),
        cosine: Cosine | None = None,
        limit: int = TOP_DUPLICATES,
        slot_threshold: float | None = None,
    ) -> list[Match]:
        """Catalog names at or above ``threshold``: same type first, then by score.

        A same-type match compared value by value under the type's naming rule
        must reach ``slot_threshold`` (default ``threshold``) LEXICALLY — see
        :func:`scored_pairs` — so a lexical-only first pass at a lower
        ``threshold`` still keeps rule-built names apart.
        """
        slot_floor = threshold if slot_threshold is None else slot_threshold
        excluded = set(exclude)
        plain = prepare_name(name)
        ruled = prepare_name(name, self.rule_for(event_type_id))
        matches: list[Match] = []
        for position in self._neighbours_by_keys(_blocking_keys(plain.whole.tokens)):
            entry = self.entries[position]
            if entry.event_id in excluded:
                continue
            same_type = entry.event_type_id == event_type_id
            own = self._prepared[position]
            by_slot = same_type and _slot_scored(ruled, own)
            lexical = (
                prepared_score(ruled, own)
                if same_type
                else prepared_score(plain, PreparedName(own.whole))
            )
            if lexical < (max(slot_floor, threshold) if by_slot else MIN_LEXICAL_FOR_EMBEDDING):
                continue
            similarity = cosine(entry.event_id) if cosine is not None else None
            score = combined_score(lexical, similarity)
            if score < threshold:
                continue
            matches.append(
                Match(
                    event_id=entry.event_id,
                    name=entry.name,
                    event_type_id=entry.event_type_id,
                    status=entry.status,
                    lexical=lexical,
                    score=score,
                    same_type=same_type,
                )
            )
        matches.sort(key=lambda m: (not m.same_type, -m.score, m.name, str(m.event_id)))
        return matches[:limit]


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cluster:
    event_ids: tuple[uuid.UUID, ...]
    score: float


def _find(parent: dict[uuid.UUID, uuid.UUID], node: uuid.UUID) -> uuid.UUID:
    root = node
    while parent[root] != root:
        root = parent[root]
    while parent[node] != root:
        parent[node], node = root, parent[node]
    return root


def scored_pairs(
    index: NameIndex,
    *,
    threshold: float = DUPLICATE_THRESHOLD,
    max_pairs: int = MAX_SCORED_PAIRS,
) -> tuple[list[tuple[tuple[uuid.UUID, uuid.UUID], float]], bool]:
    """Same-type pairs whose LEXICAL score could still reach the threshold.

    The first of two passes, so a caller can load embeddings for just these
    events before scoring (:func:`cluster_pairs`). Only names of one event type
    are compared: the identity lives inside a type. Pairs come back ordered
    (:func:`ordered_pair`). At most ``max_pairs`` pairs are SCORED; the second
    value says the budget ran out and the answer is partial.

    A pair compared value by value under a naming rule must reach
    ``threshold`` on its own: two rule-built names whose values differ are two
    events, and an embedding of their near-identical documents must not merge
    them.
    """
    pairs: list[tuple[tuple[uuid.UUID, uuid.UUID], float]] = []
    budget = max_pairs
    for position, entry in enumerate(index.entries):
        for other in sorted(index.neighbours_of(position)):
            if other <= position:
                continue
            peer = index.entries[other]
            if peer.event_type_id != entry.event_type_id or peer.event_id == entry.event_id:
                continue
            if budget <= 0:
                return pairs, True
            budget -= 1
            lexical = index.score_positions(position, other)
            floor = threshold if index.slot_scored(position, other) else MIN_LEXICAL_FOR_EMBEDDING
            if lexical < floor:
                continue
            pairs.append((ordered_pair(entry.event_id, peer.event_id), lexical))
    return pairs, False


def cluster_pairs(
    pairs: Iterable[tuple[tuple[uuid.UUID, uuid.UUID], float]],
    *,
    threshold: float = DUPLICATE_THRESHOLD,
    order: Mapping[uuid.UUID, int] | None = None,
) -> list[Cluster]:
    """Union-find over ``(pair, score)`` links at or above ``threshold``.

    Sorted by the strongest link inside each cluster, then size. ``order``
    (event id -> position) orders the members; unknown ids sort first.
    """
    parent: dict[uuid.UUID, uuid.UUID] = {}
    best: dict[uuid.UUID, float] = {}
    for (a, b), score in pairs:
        if score < threshold:
            continue
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        root_a, root_b = _find(parent, a), _find(parent, b)
        merged = max(score, best.get(root_a, 0.0), best.get(root_b, 0.0))
        if root_a != root_b:
            parent[root_b] = root_a
        best[root_a] = merged

    groups: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for node in parent:
        groups[_find(parent, node)].append(node)
    positions = order or {}
    clusters = [
        Cluster(
            event_ids=tuple(sorted(members, key=lambda m: (positions.get(m, -1), str(m)))),
            score=round(best.get(root, 0.0), 4),
        )
        for root, members in groups.items()
        if len(members) > 1
    ]
    clusters.sort(key=lambda c: (-c.score, -len(c.event_ids), str(c.event_ids[0])))
    return clusters


def ordered_pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if str(a) <= str(b) else (b, a)


# ---------------------------------------------------------------------------
# Combinatorial explosion (scan dry run)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Explosion:
    slot: int
    label: str
    count: int
    pattern: str
    samples: tuple[str, ...]


def find_combinatorial_explosions(
    names: Sequence[str],
    rule: NameRule | None,
    *,
    cardinality_threshold: int | None = None,
    min_names: int = EXPLOSION_MIN_NAMES,
    max_reports: int = 5,
) -> list[Explosion]:
    """Groups of more than ``min_names`` names that differ in exactly one rule slot.

    Only a naming rule can explode: without placeholders every name is typed
    by a person or a plan, and many similar names there are a catalog, not a
    high-cardinality column. A group means one slot carries a high-cardinality
    value (an id, a price, a free-text label) into the event NAME.

    ``cardinality_threshold`` is the scan's: a column with more distinct values
    than it becomes a ``${...}`` template and cannot explode. So a group is
    reported only while its distinct slot values still fit under the threshold
    — the one case where lowering it is the fix.
    """
    if rule is None:
        return []
    by_shape: dict[tuple[int, tuple[str, ...]], list[str]] = defaultdict(list)
    values_by_shape: dict[tuple[int, tuple[str, ...]], set[str]] = defaultdict(set)
    for name in dict.fromkeys(names):
        slots = rule.split(name)
        if slots is None:
            continue
        for index, value in enumerate(slots):
            masked = tuple("*" if i == index else s for i, s in enumerate(slots))
            by_shape[(index, masked)].append(name)
            values_by_shape[(index, masked)].add(" ".join(normalise(value)))

    found: list[Explosion] = []
    for (index, masked), members in by_shape.items():
        distinct = len(values_by_shape[(index, masked)])
        if distinct <= min_names:
            continue
        if cardinality_threshold is not None and distinct > cardinality_threshold:
            continue
        found.append(
            Explosion(
                slot=index,
                label=rule.slots[index],
                count=distinct,
                pattern=rule.render(masked),
                samples=tuple(members[:5]),
            )
        )
    found.sort(key=lambda e: (-e.count, e.pattern))
    return found[:max_reports]
