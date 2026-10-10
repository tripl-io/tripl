"""Split a large id list into bounded ``IN (...)`` chunks.

Postgres caps bind parameters per statement (32767 for asyncpg); a catalog read
over tens of thousands of events has to split its ``IN`` lists. A statement
with a bind budget of its own passes ``size`` and explains it where that
constant is defined.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import overload

IN_CHUNK_SIZE = 1000


@overload
def chunked[T](items: list[T], size: int = IN_CHUNK_SIZE) -> Iterator[list[T]]: ...


@overload
def chunked[T](items: Sequence[T], size: int = IN_CHUNK_SIZE) -> Iterator[Sequence[T]]: ...


def chunked[T](items: Sequence[T], size: int = IN_CHUNK_SIZE) -> Iterator[Sequence[T]]:
    """Consecutive slices of ``items``, each at most ``size`` long.

    A slice keeps the input's type, so a list comes back as lists.
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]
