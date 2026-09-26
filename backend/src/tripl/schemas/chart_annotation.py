from __future__ import annotations

import uuid
from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from tripl.models.domain_enums import ChartAnnotationScopeType, ChartAnnotationSource

# Project-wide markers leave both fields NULL; scoped markers must pair a
# scope_type with a scope_ref so chart filters never silently match the wrong
# series.
ALLOWED_SCOPES = {scope.value for scope in ChartAnnotationScopeType}

# ``release`` is drawn only by the metrics worker, from the activation gate; a
# client claiming it could forge a marker the UI presents as observed data.
CLIENT_ANNOTATION_SOURCES = frozenset(
    {ChartAnnotationSource.manual.value, ChartAnnotationSource.api.value}
)
ANNOTATION_URL_MAX_LENGTH = 500


class ChartAnnotationCreate(BaseModel):
    bucket: datetime
    label: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    color: str = Field(default="#ef4444", max_length=20)
    scope_type: ChartAnnotationScopeType | None = None
    scope_ref: str | None = Field(default=None, max_length=120)
    source: ChartAnnotationSource = ChartAnnotationSource.manual
    url: str | None = Field(default=None, max_length=ANNOTATION_URL_MAX_LENGTH)

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: ChartAnnotationSource) -> ChartAnnotationSource:
        if value not in CLIENT_ANNOTATION_SOURCES:
            raise ValueError(
                f"source must be one of {sorted(CLIENT_ANNOTATION_SOURCES)}; "
                "release annotations are created by the metrics worker"
            )
        return value

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        # An http(s) link with a host, nothing else: the UI renders it as an
        # anchor, and a ``javascript:`` or ``data:`` URL there is script.
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        parsed = urlparse(trimmed)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url must be an http(s) URL")
        return trimmed

    @model_validator(mode="after")
    def validate_scope(self) -> ChartAnnotationCreate:
        if self.scope_type is None and self.scope_ref is None:
            return self
        if self.scope_type is None or self.scope_ref is None:
            raise ValueError("scope_type and scope_ref must both be provided or both be null")
        if self.scope_type not in ALLOWED_SCOPES:
            raise ValueError(f"scope_type must be one of {sorted(ALLOWED_SCOPES)}")
        return self


class ChartAnnotationResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    scope_type: ChartAnnotationScopeType | None
    scope_ref: str | None
    bucket: datetime
    label: str
    description: str | None
    color: str
    source: ChartAnnotationSource
    url: str | None
    created_by_user_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}
