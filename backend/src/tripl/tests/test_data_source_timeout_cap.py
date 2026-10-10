"""``timeout_seconds`` stops at what ``data_sources.timeout_seconds`` stores.

The column is an INTEGER, so a larger value used to pass validation and fail
the INSERT with a 500. Every body that carries it (create, update, and the
unsaved connection test, which is the create body) is now a 422 naming the
field.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from tripl.schemas.data_source import (
    DataSourceConnectionTest,
    DataSourceCreate,
    DataSourceUpdate,
)
from tripl.schemas.integers import INT32_MAX

_CREATE: dict[str, Any] = {
    "name": "warehouse",
    "db_type": "clickhouse",
    "host": "ch.example.com",
    "database_name": "events",
}


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (DataSourceCreate, _CREATE),
        (DataSourceConnectionTest, _CREATE),
        (DataSourceUpdate, {}),
    ],
)
def test_timeout_seconds_is_capped_at_the_column_width(
    model: type[BaseModel], body: dict[str, Any]
) -> None:
    at_cap = model.model_validate({**body, "timeout_seconds": INT32_MAX})
    assert at_cap.timeout_seconds == INT32_MAX  # type: ignore[attr-defined]

    with pytest.raises(ValidationError) as refused:
        model.model_validate({**body, "timeout_seconds": INT32_MAX + 1})
    assert [error["loc"] for error in refused.value.errors()] == [("timeout_seconds",)]
