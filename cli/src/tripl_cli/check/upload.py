"""``tripl check --upload``: where the run comes from, and the run as tripl stores it.

The commit, the ref, the repository and the pipeline are read from the CI
system's own variables, never guessed from the checkout: a run tripl records
must name the pipeline that made it. GitLab CI and GitHub Actions are read;
anywhere else ``--upload`` is refused before anything is scanned or sent.

The default branch decides what the run is for. A run whose ref is the
repository's default branch becomes what "used in code" shows on each event; a
run on any other ref (a merge request, a feature branch, a tag) goes to the
history only. GitLab names it (``CI_DEFAULT_BRANCH``); GitHub Actions has no
variable for it, so it is read from the event payload the runner writes
(``GITHUB_EVENT_PATH``, ``repository.default_branch``). ``--default-branch``
overrides either. With none known, the run goes to the history only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tripl_cli.api import check_runs as api
from tripl_cli.check.model import CheckReport, CheckResult
from tripl_cli.errors import TriplConfigError
from tripl_cli.model import JsonDict

CI_GITLAB = "gitlab"
CI_GITHUB = "github"


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

    @property
    def on_default_branch(self) -> bool:
        return self.default_branch is not None and self.ref == self.default_branch


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


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
    }


def _github_default_branch(env: Mapping[str, str]) -> str | None:
    """``repository.default_branch`` of the event payload; None when it cannot be read."""
    path = _text(env, "GITHUB_EVENT_PATH")
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    repository = payload.get("repository") if isinstance(payload, dict) else None
    branch = repository.get("default_branch") if isinstance(repository, dict) else None
    return (branch.strip() or None) if isinstance(branch, str) else None


def _github(env: Mapping[str, str]) -> dict[str, str | None]:
    server = _text(env, "GITHUB_SERVER_URL") or "https://github.com"
    repository = _text(env, "GITHUB_REPOSITORY")
    run_id = _text(env, "GITHUB_RUN_ID")
    web = f"{server.rstrip('/')}/{repository}" if repository else None
    return {
        "repository": repository,
        "commit_sha": _text(env, "GITHUB_SHA"),
        # A pull request's source branch; else the branch or tag pushed.
        "ref": _text(env, "GITHUB_HEAD_REF") or _text(env, "GITHUB_REF_NAME"),
        "default_branch": _github_default_branch(env),
        "repository_url": web,
        "pipeline_url": f"{web}/actions/runs/{run_id}" if web and run_id else None,
    }


def ci_context(env: Mapping[str, str], *, default_branch: str | None = None) -> CiContext:
    """The run as the CI system describes it; ``default_branch`` overrides the CI's.

    Raises ``TriplConfigError`` outside GitLab CI and GitHub Actions, or when a
    variable the upload needs is empty there.
    """
    if _text(env, "GITLAB_CI") == "true":
        system, found = CI_GITLAB, _gitlab(env)
        names = "CI_PROJECT_PATH, CI_COMMIT_SHA and CI_COMMIT_REF_NAME"
    elif _text(env, "GITHUB_ACTIONS") == "true":
        system, found = CI_GITHUB, _github(env)
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
    )


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


def run_body(report: CheckReport, context: CiContext) -> dict[str, Any]:
    """The ``POST /check-runs`` body: the CI's fields and every call of the run."""
    return {
        "repository": context.repository,
        "repository_url": context.repository_url,
        "commit_sha": context.commit_sha,
        "ref": context.ref,
        "default_branch": context.default_branch,
        "pipeline_url": context.pipeline_url,
        "strict": report.strict,
        "calls": [_call(report, result) for result in report.results],
    }


def uploaded_line(context: CiContext, calls: int) -> str:
    """The stderr line after a recorded upload: what it recorded, and what it is for."""
    where = (
        f"the current {context.default_branch} run of {context.repository}: used in code shows it"
        if context.on_default_branch
        else f"{context.repository} at {context.ref}, history only (not the default branch)"
    )
    return f"Uploaded the check run ({calls} calls) as {where}."
