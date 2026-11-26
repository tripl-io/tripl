"""``tripl check --upload``: where the run comes from, and the run as tripl stores it.

The commit, the ref, the repository and the pipeline are read from the CI
system's own variables, never guessed from the checkout: a run tripl records
must name the pipeline that made it. GitLab CI and GitHub Actions are read;
anywhere else ``--upload`` is refused before anything is scanned or sent.

The default branch decides what the run is for. A branch pipeline whose ref is
the repository's default branch becomes what "used in code" shows on each
event; a run on any other ref (a feature branch, a tag) goes to the history
only, and so does every merge request or pull request pipeline, even one whose
source branch has the default branch's name (``main`` into ``release``, a
fork's ``main``): it checks a merged tree, not the default branch. GitLab names
the default branch (``CI_DEFAULT_BRANCH``); GitHub Actions has no variable for
it, so it is read from the event payload the runner writes
(``GITHUB_EVENT_PATH``, ``repository.default_branch``), and a scheduled run,
whose payload names no repository, runs on the default branch by GitHub's own
rule. ``--default-branch`` overrides either. With none known, the run goes to
the history only.

tripl keeps one such snapshot per repository and check directory: the check
config's ``root`` relative to the repository root (``.`` for the root), so
several apps checked in one repository keep one each. And it keeps the newest:
the pipeline's creation time (GitLab's ``CI_PIPELINE_CREATED_AT``) and the
commit's timestamp (GitLab's ``CI_COMMIT_TIMESTAMP``, the pushed commit's
``timestamp`` in a GitHub push payload) go with the run, and a default-branch
run older than the one tripl holds goes to the history only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tripl_cli.api import check_runs as api
from tripl_cli.check.model import CheckReport, CheckResult, UploadOutcome
from tripl_cli.errors import TriplConfigError
from tripl_cli.model import JsonDict

CI_GITLAB = "gitlab"
CI_GITHUB = "github"

# GitHub Actions events whose run checks a pull request's tree, not a branch.
GITHUB_PULL_REQUEST_EVENTS = frozenset({"pull_request", "pull_request_target", "merge_group"})
# GitLab pipeline sources that run for a merge request, or for a GitHub pull
# request mirrored into GitLab.
GITLAB_MERGE_REQUEST_SOURCES = frozenset({"merge_request_event", "external_pull_request_event"})
#: The check directory of a check of the whole repository.
REPOSITORY_ROOT = "."


@dataclass(frozen=True)
class CiContext:
    """What the CI system says about the run: the source of every run-level field sent."""

    system: str
    repository: str
    commit_sha: str
    ref: str
    default_branch: str | None
    repository_url: str | None
    pipeline_url: str | None
    # A merge request or pull request pipeline: history only, whatever its ref.
    merge_request: bool = False
    # ISO 8601, as the CI wrote them; they order two default-branch runs.
    pipeline_created_at: str | None = None
    committed_at: str | None = None

    @property
    def on_default_branch(self) -> bool:
        return (
            not self.merge_request
            and self.default_branch is not None
            and self.ref == self.default_branch
        )


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _gitlab_merge_request(env: Mapping[str, str]) -> bool:
    return (
        _text(env, "CI_PIPELINE_SOURCE") in GITLAB_MERGE_REQUEST_SOURCES
        or _text(env, "CI_MERGE_REQUEST_IID") is not None
        or _text(env, "CI_EXTERNAL_PULL_REQUEST_IID") is not None
    )


def _github_merge_request(env: Mapping[str, str]) -> bool:
    # GITHUB_HEAD_REF is set for the pull request events only.
    return (
        _text(env, "GITHUB_EVENT_NAME") in GITHUB_PULL_REQUEST_EVENTS
        or _text(env, "GITHUB_HEAD_REF") is not None
    )


def _gitlab(env: Mapping[str, str]) -> dict[str, str | None]:
    return {
        "repository": _text(env, "CI_PROJECT_PATH"),
        "commit_sha": _text(env, "CI_COMMIT_SHA"),
        # The branch or tag the pipeline runs for; a merge request pipeline's
        # source branch.
        "ref": _text(env, "CI_COMMIT_REF_NAME"),
        "default_branch": _text(env, "CI_DEFAULT_BRANCH"),
        "repository_url": _text(env, "CI_PROJECT_URL"),
        "pipeline_url": _text(env, "CI_PIPELINE_URL"),
        "pipeline_created_at": _text(env, "CI_PIPELINE_CREATED_AT"),
        "committed_at": _text(env, "CI_COMMIT_TIMESTAMP"),
    }


def _github_payload(env: Mapping[str, str]) -> dict[str, Any]:
    """The event payload the runner wrote; empty when it cannot be read."""
    path = _text(env, "GITHUB_EVENT_PATH")
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _nested_text(payload: Mapping[str, Any], outer: str, inner: str) -> str | None:
    """``payload[outer][inner]`` when it is a non-blank string, else None."""
    section = payload.get(outer)
    value = section.get(inner) if isinstance(section, dict) else None
    return (value.strip() or None) if isinstance(value, str) else None


def _github(env: Mapping[str, str]) -> dict[str, str | None]:
    server = _text(env, "GITHUB_SERVER_URL") or "https://github.com"
    repository = _text(env, "GITHUB_REPOSITORY")
    run_id = _text(env, "GITHUB_RUN_ID")
    web = f"{server.rstrip('/')}/{repository}" if repository else None
    payload = _github_payload(env)
    default_branch = _nested_text(payload, "repository", "default_branch")
    if default_branch is None and _text(env, "GITHUB_EVENT_NAME") == "schedule":
        # A schedule always runs on the default branch, and its payload has no
        # ``repository`` to name it.
        default_branch = _text(env, "GITHUB_REF_NAME")
    return {
        "repository": repository,
        "commit_sha": _text(env, "GITHUB_SHA"),
        # A pull request's source branch; else the branch or tag pushed.
        "ref": _text(env, "GITHUB_HEAD_REF") or _text(env, "GITHUB_REF_NAME"),
        "default_branch": default_branch,
        "repository_url": web,
        "pipeline_url": f"{web}/actions/runs/{run_id}" if web and run_id else None,
        # A workflow run's creation time is in no variable; a push names its commit's.
        "pipeline_created_at": None,
        "committed_at": _nested_text(payload, "head_commit", "timestamp"),
    }


def ci_context(env: Mapping[str, str], *, default_branch: str | None = None) -> CiContext:
    """The run as the CI system describes it; ``default_branch`` overrides the CI's.

    Raises ``TriplConfigError`` outside GitLab CI and GitHub Actions, or when a
    variable the upload needs is empty there.
    """
    if _text(env, "GITLAB_CI") == "true":
        system, found = CI_GITLAB, _gitlab(env)
        merge_request = _gitlab_merge_request(env)
        names = "CI_PROJECT_PATH, CI_COMMIT_SHA and CI_COMMIT_REF_NAME"
    elif _text(env, "GITHUB_ACTIONS") == "true":
        system, found = CI_GITHUB, _github(env)
        merge_request = _github_merge_request(env)
        names = "GITHUB_REPOSITORY, GITHUB_SHA and GITHUB_REF_NAME"
    else:
        raise TriplConfigError(
            "--upload records the run under its commit, ref and pipeline, read from GitLab CI "
            "or GitHub Actions variables, and this is neither; run it in a CI job, or drop "
            "--upload."
        )
    repository, commit_sha, ref = found["repository"], found["commit_sha"], found["ref"]
    if repository is None or commit_sha is None or ref is None:
        raise TriplConfigError(f"--upload needs {names} set by the CI job; one of them is empty.")
    override = default_branch.strip() if default_branch is not None else None
    return CiContext(
        system=system,
        repository=repository[: api.REPOSITORY_MAX_LENGTH],
        commit_sha=commit_sha,
        ref=ref[: api.REF_MAX_LENGTH],
        default_branch=(override or found["default_branch"] or None),
        repository_url=_url(found["repository_url"]),
        pipeline_url=_url(found["pipeline_url"]),
        merge_request=merge_request,
        pipeline_created_at=_timestamp(found["pipeline_created_at"]),
        committed_at=_timestamp(found["committed_at"]),
    )


def _timestamp(value: str | None) -> str | None:
    """An ISO 8601 timestamp as given, or None: one tripl could not read orders nothing."""
    if value is None:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def check_directory(scan_root: Path | None, repo_root: Path | None) -> str:
    """The directory the check scanned, relative to the repository root, POSIX style.

    ``.`` for the repository root itself, and when either is unknown or the
    scan root is outside the repository: one snapshot for the whole repository.
    """
    if scan_root is None or repo_root is None:
        return REPOSITORY_ROOT
    try:
        relative = scan_root.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return REPOSITORY_ROOT
    directory = relative.as_posix()
    if directory in ("", ".") or len(directory) > api.CHECK_ROOT_MAX_LENGTH:
        return REPOSITORY_ROOT
    return directory


def _url(value: str | None) -> str | None:
    if value is None or len(value) > api.URL_MAX_LENGTH:
        return None
    return value if value.startswith(("http://", "https://")) else None


def _cut(value: str | None, limit: int) -> str | None:
    return None if value is None else value[:limit]


def _call(report: CheckReport, result: CheckResult) -> JsonDict:
    origin = result.item.origin
    findings: list[JsonDict] = [
        {
            "code": finding.code[: api.CODE_MAX_LENGTH],
            "severity": finding.severity,
            "message": finding.message[: api.MESSAGE_MAX_LENGTH],
            "field": _cut(finding.field, api.FIELD_MAX_LENGTH),
            "suppressed": finding.suppression is not None,
        }
        for finding in result.findings[: api.MAX_FINDINGS_PER_CALL]
    ]
    return {
        "path": report.repo_path(origin)[: api.PATH_MAX_LENGTH],
        "line": origin.line,
        "column": origin.column,
        "status": result.status,
        "event_id": result.event_id,
        "event_type": _cut(result.item.event_type, api.EVENT_TYPE_MAX_LENGTH),
        "name": _cut(result.item.name, api.NAME_MAX_LENGTH),
        "identity": _cut(result.identity, api.NAME_MAX_LENGTH),
        "findings": findings,
    }


def run_body(
    report: CheckReport, context: CiContext, check_root: str = REPOSITORY_ROOT
) -> dict[str, Any]:
    """The ``POST /check-runs`` body: the CI's fields and every call of the run."""
    return {
        "repository": context.repository,
        "check_root": check_root,
        "repository_url": context.repository_url,
        "commit_sha": context.commit_sha,
        "ref": context.ref,
        "default_branch": context.default_branch,
        "pipeline_url": context.pipeline_url,
        "merge_request": context.merge_request,
        "pipeline_created_at": context.pipeline_created_at,
        "committed_at": context.committed_at,
        "strict": report.strict,
        "calls": [_call(report, result) for result in report.results],
    }


def outcome(
    context: CiContext,
    check_root: str,
    calls: int,
    *,
    answer: object = None,
    error: str | None = None,
) -> UploadOutcome:
    """What the upload did: tripl's ``answer`` to it, or the ``error`` that stopped it."""
    stored = answer if isinstance(answer, dict) else {}
    run_id = stored.get("id")
    current = stored.get("is_current")
    return UploadOutcome(
        ok=error is None,
        error=error,
        run_id=str(run_id) if error is None and run_id is not None else None,
        current=current if error is None and isinstance(current, bool) else None,
        repository=context.repository,
        check_root=check_root,
        commit_sha=context.commit_sha,
        ref=context.ref,
        default_branch=context.default_branch,
        merge_request=context.merge_request,
        calls=calls,
    )


def upload_document(upload: UploadOutcome) -> JsonDict:
    """``check --json``'s ``upload`` key."""
    return {
        "ok": upload.ok,
        "error": upload.error,
        "run_id": upload.run_id,
        "current": upload.current,
        "repository": upload.repository,
        "check_root": upload.check_root,
        "commit_sha": upload.commit_sha,
        "ref": upload.ref,
        "default_branch": upload.default_branch,
        "merge_request": upload.merge_request,
        "calls": upload.calls,
    }


def uploaded_line(
    context: CiContext,
    calls: int,
    *,
    check_root: str = REPOSITORY_ROOT,
    current: bool | None = None,
) -> str:
    """The stderr line after a recorded upload: what it recorded, and what it is for.

    ``current`` is tripl's answer: False for a default-branch run older than
    the one tripl holds, which went to the history only.
    """
    repository = context.repository
    if check_root != REPOSITORY_ROOT:
        repository = f"{repository} ({check_root})"
    if context.on_default_branch and current is False:
        where = (
            f"{repository} at {context.ref}, history only (older than the current "
            f"{context.default_branch} run)"
        )
    elif context.on_default_branch:
        where = f"the current {context.default_branch} run of {repository}: used in code shows it"
    elif context.merge_request:
        request = "pull request" if context.system == CI_GITHUB else "merge request"
        where = f"{repository} at {context.ref}, history only (a {request} run)"
    else:
        where = f"{repository} at {context.ref}, history only (not the default branch)"
    return f"Uploaded the check run ({calls} calls) as {where}."
