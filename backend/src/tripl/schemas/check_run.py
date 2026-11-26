"""Uploaded ``tripl check`` runs: the upload, the history, and "used in code" on an event.

``POST /projects/{slug}/check-runs`` takes one run as a CI job saw it: the
repository, the commit and ref, the pipeline, and every call with its verdict.
A run on the repository's default branch keeps its calls (until the next
default-branch run of that repository replaces them); any other run is kept as
a summary only. See :mod:`tripl.services.check_run_service`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from tripl.schemas.plan_validation import MAX_VALIDATION_ITEMS

#: The most calls one upload may carry: one validate request's worth.
MAX_CHECK_RUN_CALLS = MAX_VALIDATION_ITEMS
MAX_FINDINGS_PER_CALL = 50
URL_MAX_LENGTH = 2000

CheckCallStatus = Literal["ok", "warning", "error", "dynamic"]
CheckFindingSeverity = Literal["error", "warning", "info"]


def _http_url(value: str | None) -> str | None:
    """An http(s) link with a host, or None: the UI renders it as an anchor."""
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    parsed = urlparse(trimmed)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("must be an http(s) URL")
    return trimmed


class CheckRunFinding(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    severity: CheckFindingSeverity
    message: str = Field(max_length=2000)
    field: str | None = Field(default=None, max_length=255)
    # A ``tripl-check-ignore`` marker waives it: reported, never failing.
    suppressed: bool = False


class CheckRunCallIn(BaseModel):
    """One call as the check saw it: where it is, what it resolved to, and its verdict."""

    # Relative to the repository root.
    path: str = Field(min_length=1, max_length=1000)
    line: int = Field(ge=0, le=10_000_000)
    column: int | None = Field(default=None, ge=0, le=10_000_000)
    status: CheckCallStatus
    # The plan event the call resolved to; an id of another project is dropped.
    event_id: uuid.UUID | None = None
    event_type: str | None = Field(default=None, max_length=100)
    name: str | None = Field(default=None, max_length=500)
    identity: str | None = Field(default=None, max_length=500)
    findings: list[CheckRunFinding] = Field(default_factory=list, max_length=MAX_FINDINGS_PER_CALL)


class CheckRunCreate(BaseModel):
    """One ``tripl check --upload`` run."""

    # What the CI system calls the repository: GitLab's CI_PROJECT_PATH,
    # GitHub's GITHUB_REPOSITORY. "Used in code" keeps one run per repository.
    repository: str = Field(min_length=1, max_length=255)
    repository_url: str | None = Field(default=None, max_length=URL_MAX_LENGTH)
    commit_sha: str = Field(min_length=4, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    ref: str = Field(min_length=1, max_length=255)
    # The repository's default branch. A run whose ``ref`` is it feeds "used in
    # code"; any other run, or one with no default branch, goes to the history only.
    default_branch: str | None = Field(default=None, min_length=1, max_length=255)
    pipeline_url: str | None = Field(default=None, max_length=URL_MAX_LENGTH)
    strict: bool = False
    calls: list[CheckRunCallIn] = Field(default_factory=list, max_length=MAX_CHECK_RUN_CALLS)

    @field_validator("repository_url", "pipeline_url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return _http_url(value)


class CheckRunCounts(BaseModel):
    total: int
    ok: int
    warning: int
    error: int
    dynamic: int


class CheckRunResponse(BaseModel):
    """A run as the history lists it: a summary, whatever was stored with it."""

    id: uuid.UUID
    repository: str
    repository_url: str | None
    commit_sha: str
    ref: str
    default_branch: str | None
    is_default_branch: bool
    # The latest default-branch run of its repository: its calls are what
    # "used in code" shows. Every other run is a summary.
    is_current: bool
    pipeline_url: str | None
    strict: bool
    calls: CheckRunCounts
    created_at: datetime


class CheckRunListResponse(BaseModel):
    items: list[CheckRunResponse]
    total: int


class CheckRunCallLocation(BaseModel):
    path: str
    line: int
    column: int | None
    status: CheckCallStatus
    findings: list[CheckRunFinding]


class EventCodeUsageRepository(BaseModel):
    """One repository's latest default-branch run, and the calls in it that send the event.

    ``calls`` is empty when that run found no call of the event: the event is
    not used in that repository's code, as of ``run``.
    """

    repository: str
    repository_url: str | None
    run: CheckRunResponse
    calls: list[CheckRunCallLocation]


class EventCodeUsageResponse(BaseModel):
    """Where the code sends an event, per repository that uploads check runs."""

    event_id: uuid.UUID
    repositories: list[EventCodeUsageRepository]
