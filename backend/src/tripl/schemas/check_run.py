"""Uploaded ``tripl check`` runs: the upload, the history, and "used in code" on an event.

``POST /projects/{slug}/check-runs`` takes one run as a CI job saw it: the
repository and the directory checked in it, the commit and ref, the pipeline,
and every call with its verdict. The newest run on the repository's default
branch keeps its calls, per repository and check directory (until a newer
default-branch run of the same two replaces them); any other run is kept as a
summary only. See :mod:`tripl.services.check_run_service`.
"""

from __future__ import annotations

import posixpath
import uuid
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from tripl.schemas.plan_validation import MAX_VALIDATION_ITEMS

#: The most calls one upload may carry: one validate request's worth.
MAX_CHECK_RUN_CALLS = MAX_VALIDATION_ITEMS
MAX_FINDINGS_PER_CALL = 50
URL_MAX_LENGTH = 2000
CHECK_ROOT_MAX_LENGTH = 1000
#: The check directory of a check run at the repository's root.
REPOSITORY_ROOT = "."

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


def check_root(value: str | None) -> str:
    """A directory relative to the repository root, normalized; ``.`` for the root itself.

    ``apps/ios/`` and ``./apps/ios`` are one directory, so they key one
    snapshot. An absolute path, or one that leaves the repository, is refused.
    """
    trimmed = (value or "").strip().replace("\\", "/")
    if not trimmed:
        return REPOSITORY_ROOT
    if trimmed.startswith("/"):
        raise ValueError("must be relative to the repository root")
    normalized = posixpath.normpath(trimmed)
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("must be inside the repository")
    return normalized


def _aware(value: datetime | None) -> datetime | None:
    """A timestamp with no offset is read as UTC, so two runs always compare."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


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
    # GitHub's GITHUB_REPOSITORY.
    repository: str = Field(min_length=1, max_length=255)
    # The directory the check scanned, relative to the repository root (the
    # check config's ``root``); ``.`` for the root. "Used in code" keeps one run
    # per repository and check directory, so several apps checked in one
    # repository keep a snapshot each.
    check_root: str = Field(default=REPOSITORY_ROOT, max_length=CHECK_ROOT_MAX_LENGTH)
    repository_url: str | None = Field(default=None, max_length=URL_MAX_LENGTH)
    commit_sha: str = Field(min_length=4, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    ref: str = Field(min_length=1, max_length=255)
    # The repository's default branch. A branch run whose ``ref`` is it feeds
    # "used in code"; any other run, or one with no default branch, goes to the
    # history only.
    default_branch: str | None = Field(default=None, min_length=1, max_length=255)
    pipeline_url: str | None = Field(default=None, max_length=URL_MAX_LENGTH)
    # A merge request or pull request pipeline. It checks a merged tree, not the
    # default branch, so it goes to the history only even when its source
    # branch has the default branch's name (``main`` into ``release``, a fork's
    # ``main``).
    merge_request: bool = False
    # When the pipeline was created (GitLab's CI_PIPELINE_CREATED_AT) and when
    # the commit was made (GitLab's CI_COMMIT_TIMESTAMP, the pushed commit's
    # timestamp on GitHub). They order two default-branch runs: one older than
    # the current run goes to the history only, so a re-run of an old pipeline
    # never puts old code back in "used in code".
    pipeline_created_at: datetime | None = None
    committed_at: datetime | None = None
    strict: bool = False
    calls: list[CheckRunCallIn] = Field(default_factory=list, max_length=MAX_CHECK_RUN_CALLS)

    @field_validator("repository_url", "pipeline_url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return _http_url(value)

    @field_validator("check_root", mode="before")
    @classmethod
    def _check_root(cls, value: str | None) -> str:
        return check_root(value)

    @field_validator("pipeline_created_at", "committed_at")
    @classmethod
    def _check_time(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


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
    check_root: str
    repository_url: str | None
    commit_sha: str
    ref: str
    default_branch: str | None
    merge_request: bool
    is_default_branch: bool
    # The newest default-branch run of its repository and check directory: its
    # calls are what "used in code" shows. Every other run is a summary, and so
    # is a default-branch run older than the current one when it arrived.
    is_current: bool
    pipeline_url: str | None
    pipeline_created_at: datetime | None
    committed_at: datetime | None
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
    """One repository's current run of one check directory, and its calls that send the event.

    ``calls`` is empty when that run found no call of the event: the event is
    not used in that directory's code, as of ``run``. ``check_root`` is ``.``
    for a check of the whole repository.
    """

    repository: str
    check_root: str
    repository_url: str | None
    run: CheckRunResponse
    calls: list[CheckRunCallLocation]


class EventCodeUsageResponse(BaseModel):
    """Where the code sends an event, per repository and check directory that upload runs."""

    event_id: uuid.UUID
    repositories: list[EventCodeUsageRepository]
