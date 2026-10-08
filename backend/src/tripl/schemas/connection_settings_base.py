"""The base every per-warehouse connection settings model extends."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _ConnectionSettingsBase(BaseModel):
    # The whole point: an unknown key is an error, not a silently stored no-op.
    model_config = ConfigDict(extra="forbid")
