"""Split a large id list into bounded ``IN (...)`` chunks.

Postgres caps bind parameters per statement (32767 for asyncpg); a catalog read
over tens of thousands of events has to split its ``IN`` lists.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

IN_CHUNK_SIZE = 1000


def chunked[T](items: Sequence[T], size: int = IN_CHUNK_SIZE) -> Iterator[Sequence[T]]:
    """Consecutive slices of ``items``, each at most ``size`` long."""
    for start in range(0, len(items), size):
        yield items[start : start + size]
