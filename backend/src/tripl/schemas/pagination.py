"""The paging parameters list endpoints share."""

from __future__ import annotations

from typing import Annotated

from fastapi import Query

#: The largest ``offset`` a list endpoint accepts. Far past any real list, and
#: it keeps the number inside what the database driver can bind: an unbounded
#: offset let a 20-digit value through validation and fail in the driver as a
#: 500 instead of a 422.
MAX_OFFSET = 1_000_000_000

#: ``offset`` on a paged list: how many items to skip.
Offset = Annotated[
    int, Query(ge=0, le=MAX_OFFSET, description="How many items to skip, for paging.")
]
