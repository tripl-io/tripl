"""Relink suggestions for broken docs catalog links (F24 part 2, GH #308).

A plan link is stored by name, so renaming an event breaks every note that
names it. The reader then gets up to three current names closest to the stored
one, ranked in Python (no database extension needed, so SQLite and Postgres
agree): trigram similarity first, the way ``pg_trgm`` measures it, then the
Levenshtein distance to break ties and to catch short names that share few
trigrams (``signup`` -> ``sign_up``).

The cost is bounded, because it runs inside a read request: a :class:`NamePool`
indexes a kind's current names by trigram ONCE per request, so a broken link
costs one pass over the names sharing a trigram with it, not a trigram build
per name; and :class:`SuggestionBudget` caps how many broken links of one
response get suggestions at all (the rest get none).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

MAX_SUGGESTIONS = 3
#: At most this many distinct broken links of one response get suggestions.
MAX_SUGGESTED_REFS = 20
# A candidate is offered when it shares this much of its trigram set with the
# stored name, or is this close by edits (relative to the longer name).
_MIN_SIMILARITY = 0.3
_MAX_RELATIVE_DISTANCE = 0.34
# How many trigram-ranked candidates get the (quadratic) edit distance.
_POOL = 50
_MAX_EDIT_CHARS = 120


def trigrams(text: str) -> set[str]:
    """``pg_trgm``'s trigrams: lower-cased, each word padded with two spaces before, one after."""
    grams: set[str] = set()
    for word in "".join(ch if ch.isalnum() else " " for ch in text.lower()).split():
        padded = f"  {word} "
        grams.update(padded[index : index + 3] for index in range(len(padded) - 2))
    return grams


def similarity(left: str, right: str) -> float:
    """Shared trigrams over all trigrams of the two names, in [0, 1]."""
    a, b = trigrams(left), trigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def levenshtein(left: str, right: str) -> int:
    """The edit distance between two strings (insert, delete, substitute)."""
    if left == right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, char_left in enumerate(left, start=1):
        current = [row]
        for column, char_right in enumerate(right, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + (char_left != char_right),
                )
            )
        previous = current
    return previous[-1]


class NamePool:
    """The current names of one kind, indexed by trigram once.

    Built once per (kind, pool) per request and reused for every broken link
    of that kind, so a lookup only visits the names that share a trigram with
    the stored one.
    """

    __slots__ = ("_by_gram", "_gram_counts", "_names")

    def __init__(self, names: Iterable[str]) -> None:
        self._names: list[str] = sorted({name for name in names if name})
        self._gram_counts: list[int] = []
        self._by_gram: dict[str, list[int]] = {}
        for index, name in enumerate(self._names):
            grams = trigrams(name)
            self._gram_counts.append(len(grams))
            for gram in grams:
                self._by_gram.setdefault(gram, []).append(index)

    def __len__(self) -> int:
        return len(self._names)

    def closest(self, name: str, limit: int = MAX_SUGGESTIONS) -> list[str]:
        """Up to ``limit`` names closest to ``name``, best first; ``name`` itself never."""
        wanted = name.strip()
        wanted_grams = trigrams(wanted)
        if not wanted_grams:
            return []
        shared: Counter[int] = Counter()
        for gram in wanted_grams:
            shared.update(self._by_gram.get(gram, ()))
        scored: list[tuple[float, str]] = []
        for index, count in shared.items():
            candidate = self._names[index]
            if candidate == wanted:
                continue
            union = len(wanted_grams) + self._gram_counts[index] - count
            scored.append((count / union, candidate))
        scored.sort(key=lambda item: (-item[0], item[1]))
        lowered = wanted.lower()[:_MAX_EDIT_CHARS]
        ranked: list[tuple[float, int, str]] = []
        for score, candidate in scored[:_POOL]:
            distance = levenshtein(lowered, candidate.lower()[:_MAX_EDIT_CHARS])
            longest = max(len(wanted), len(candidate), 1)
            if score < _MIN_SIMILARITY and distance / longest > _MAX_RELATIVE_DISTANCE:
                continue
            ranked.append((-score, distance, candidate))
        ranked.sort()
        return [candidate for _, _, candidate in ranked[:limit]]


def closest(name: str, candidates: Iterable[str], limit: int = MAX_SUGGESTIONS) -> list[str]:
    """Up to ``limit`` distinct ``candidates`` closest to ``name``, best first.

    A one-off lookup; a caller with several names to match against one pool
    builds a :class:`NamePool` once instead.
    """
    return NamePool(candidates).closest(name, limit)


class SuggestionBudget:
    """How many more broken links of one response may get relink suggestions."""

    __slots__ = ("_left",)

    def __init__(self, limit: int = MAX_SUGGESTED_REFS) -> None:
        self._left = limit

    def take(self) -> bool:
        """True (and one fewer left) while the budget lasts."""
        if self._left <= 0:
            return False
        self._left -= 1
        return True
