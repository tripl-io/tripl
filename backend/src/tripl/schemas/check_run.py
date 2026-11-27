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
import re
import uuid
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, field_validator

from tripl.core.bucketing import optional_to_utc
from tripl.schemas.plan_validation import MAX_VALIDATION_ITEMS
from tripl.schemas.text_filters import strip_nul_bytes

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
    """A path relative to the repository root, normalized; ``.`` for the root itself.

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


#: The history's ``?check_root=`` filter: normalized as an upload's is, so
#: ``apps/ios/`` finds the runs stored as ``apps/ios``, and a directory no
#: upload could name is a 422, not an empty page.
CheckRootFilter = Annotated[str, BeforeValidator(strip_nul_bytes), AfterValidator(check_root)]


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
    # What names the check in the repository, relative to its root: the
    # directory the check scanned (the check config's ``root``; ``.`` for the
    # root), or the check config's own path when it is not that directory's
    # ``.tripl/check.yml``. "Used in code" keeps one run per repository and
    # check directory, so several apps checked in one repository keep a
    # snapshot each.
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
        # No offset is read as UTC, so two runs always compare.
        return optional_to_utc(value)


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
    # The project's link template (``CheckRunSettings.code_url_template``), so
    # the event page links each place without a second request. NULL: plain text.
    code_url_template: str | None = None


class CheckRunDirectory(BaseModel):
    """One repository and check directory with runs still kept, for the history's filter."""

    repository: str
    check_root: str
    runs: int
    last_run_at: datetime


class CheckRunDirectoryListResponse(BaseModel):
    items: list[CheckRunDirectory]


#: What a code link template may name, filled in per place the check found:
#: the repository's web address and CI name, the run's ref and commit, the
#: file's path relative to the repository root, and the line.
CODE_URL_PLACEHOLDERS: tuple[str, ...] = (
    "repository_url",
    "repository",
    "ref",
    "commit",
    "path",
    "line",
)
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
# ``https://host`` followed by the end of the host: the scheme and host are
# literal text, never a placeholder.
_LITERAL_ORIGIN = re.compile(r"^https?://[^/?#{}@\\]+[/?#]", re.IGNORECASE)
# What stands in for each placeholder while a template is checked: filled in,
# the link must be an http(s) URL with a host.
_SAMPLE_VALUES = {
    "repository_url": "https://git.example.com/org/app",
    "repository": "org/app",
    "ref": "main",
    "commit": "0123456789abcdef",
    "path": "src/app.ts",
    "line": "1",
}


def code_url_template(value: str | None) -> str | None:
    """A link template that fills in to an http(s) URL, or None to show plain text.

    It names only :data:`CODE_URL_PLACEHOLDERS`, must name ``{path}``, and has
    no whitespace or control character. It starts with ``http://`` or
    ``https://`` and a host written out, or with ``{repository_url}`` (the
    address a run uploaded, which is http(s) too), and filled in with sample
    values it reads as an http(s) URL with a host.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in trimmed):
        raise ValueError("must not contain spaces or control characters")
    names = _PLACEHOLDER.findall(trimmed)
    known = ", ".join(f"{{{name}}}" for name in CODE_URL_PLACEHOLDERS)
    unknown = [name for name in names if name not in CODE_URL_PLACEHOLDERS]
    if unknown:
        raise ValueError(f"unknown placeholder {{{unknown[0]}}}; use {known}")
    rest = _PLACEHOLDER.sub("", trimmed)
    if "{" in rest or "}" in rest:
        raise ValueError(f"has an unmatched brace; the placeholders are {known}")
    if "path" not in names:
        raise ValueError("must contain {path}")
    if "repository_url" in names and not trimmed.startswith("{repository_url}"):
        raise ValueError("{repository_url} may only start the template")
    # The host is written out, so no value a run uploads (a path, a ref) can
    # move the link to another site.
    if not trimmed.startswith("{repository_url}") and not _LITERAL_ORIGIN.match(trimmed):
        raise ValueError("must start with http:// or https:// and a host, or with {repository_url}")
    sample = _PLACEHOLDER.sub(lambda match: _SAMPLE_VALUES[match.group(1)], trimmed)
    parsed = urlparse(sample)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("must be an http(s) URL, or start with {repository_url}")
    return trimmed


class CheckRunSettings(BaseModel):
    """How the project shows its check runs: the link from a place to its file."""

    # ``https://github.com/org/app/blob/{commit}/{path}#L{line}``; NULL shows
    # the places "used in code" lists as plain text.
    code_url_template: str | None = None


class CheckRunSettingsUpdate(BaseModel):
    # ``null`` or an empty string clears the template.
    code_url_template: str | None = Field(default=None, max_length=URL_MAX_LENGTH)

    @field_validator("code_url_template")
    @classmethod
    def _check_template(cls, value: str | None) -> str | None:
        return code_url_template(value)
