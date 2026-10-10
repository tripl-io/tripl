"""Bounds for request integers that are stored in an ``INTEGER`` column.

A PostgreSQL ``INTEGER`` holds -2**31 to 2**31 - 1, and an INSERT or UPDATE
binds the value with its column's type. A larger number from a request body
would pass validation and then fail in the driver as a 500; with these bounds
it is a 422 that names the field.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1

#: Any integer an ``INTEGER`` column can store, such as a sort ``order``.
Int32 = Annotated[int, Field(ge=INT32_MIN, le=INT32_MAX)]
