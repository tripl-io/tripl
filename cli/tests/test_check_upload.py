"""``tripl check --upload``: the CI's account of the run, the body sent, and what never changes.

The run-level fields come from GitLab CI or GitHub Actions variables, never
from the checkout; the calls are the run's results with repository paths. A
failed upload is a warning, and the exit code is always the check's own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tripl_cli.check.upload import (
    CI_GITHUB,
    CI_GITLAB,
    check_directory,
    ci_context,
    uploaded_line,
)
from tripl_cli.cli import main
from tripl_cli.errors import TriplConfigError
from tripl_cli.model import ACCESS_CHECK_UPLOAD, access_of_key

from .conftest import API_BASE, FakeInstance, make_branch
from .test_check_cmd import CONFIG, VALIDATE_URL, _respond, _write
from .test_check_cmd import repo as repo  # noqa: PLC0414 - the fixture

UPLOAD_URL = f"{API_BASE}/projects/demo/check-runs"
SHA = "0123456789abcdef0123456789abcdef01234567"
REVISION = "0a1b2c3d-0000-4000-8000-00000000beef"

GITLAB = {
    "GITLAB_CI": "true",
    "CI_PROJECT_PATH": "shop/ios",
    "CI_PROJECT_URL": "https://gitlab.example.com/shop/ios",
    "CI_COMMIT_SHA": SHA,
    "CI_COMMIT_REF_NAME": "main",
    "CI_DEFAULT_BRANCH": "main",
    "CI_PIPELINE_URL": "https://gitlab.example.com/shop/ios/-/pipelines/42",
}
# Every variable either reader looks at, so the job running these tests
# (GitHub Actions sets its own) cannot leak into them.
CI_VARIABLES = (
    *GITLAB,
    "GITHUB_ACTIONS",
    "GITHUB_REPOSITORY",
    "GITHUB_SERVER_URL",
    "GITHUB_SHA",
    "GITHUB_REF_NAME",
    "GITHUB_HEAD_REF",
    "GITHUB_RUN_ID",
    "GITHUB_EVENT_PATH",
    "GITHUB_EVENT_NAME",
    "CI_PIPELINE_SOURCE",
    "CI_MERGE_REQUEST_IID",
    "CI_EXTERNAL_PULL_REQUEST_IID",
    "CI_PIPELINE_CREATED_AT",
    "CI_COMMIT_TIMESTAMP",
)


@pytest.fixture(autouse=True)
def _no_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CI_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def gitlab(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in GITLAB.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def validator(tripl_api: FakeInstance) -> Any:
    return tripl_api.handler(VALIDATE_URL, _respond, method="POST")


def _stored(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(201, json={"id": "run-1", "repository": body["repository"]})


@pytest.fixture
def uploads(tripl_api: FakeInstance) -> Any:
    return tripl_api.handler(UPLOAD_URL, _stored, method="POST")


# --- the CI's account --------------------------------------------------------------


def test_gitlab_variables_name_the_run() -> None:
    context = ci_context(GITLAB)
    assert context.system == CI_GITLAB
    assert (context.repository, context.commit_sha, context.ref) == ("shop/ios", SHA, "main")
    assert context.default_branch == "main"
    assert context.on_default_branch
    assert context.repository_url == "https://gitlab.example.com/shop/ios"
    assert context.pipeline_url == "https://gitlab.example.com/shop/ios/-/pipelines/42"


def test_a_merge_request_pipeline_is_not_the_default_branch() -> None:
    context = ci_context({**GITLAB, "CI_COMMIT_REF_NAME": "feature/cart"})
    assert context.ref == "feature/cart"
    assert not context.on_default_branch


@pytest.mark.parametrize(
    "variables",
    [
        {"CI_PIPELINE_SOURCE": "merge_request_event", "CI_MERGE_REQUEST_IID": "5"},
        {"CI_MERGE_REQUEST_IID": "5"},
        {"CI_PIPELINE_SOURCE": "external_pull_request_event"},
    ],
)
def test_a_merge_request_from_the_default_branch_is_history_only(
    variables: dict[str, str],
) -> None:
    # A merge request whose source branch is called like the default branch
    # (main into release, or a fork's main) checks a merged tree, not main.
    context = ci_context({**GITLAB, **variables})
    assert context.ref == "main"
    assert context.merge_request
    assert not context.on_default_branch
    assert "history only" in uploaded_line(context, 3)
    assert "merge request" in uploaded_line(context, 3)


def test_the_flag_overrides_the_ci_default_branch() -> None:
    context = ci_context({**GITLAB, "CI_COMMIT_REF_NAME": "trunk"}, default_branch="trunk")
    assert context.default_branch == "trunk"
    assert context.on_default_branch


def test_github_reads_the_default_branch_from_the_event_payload(tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"default_branch": "main"}}), encoding="utf-8")
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "shop/web",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_SHA": SHA,
        "GITHUB_REF_NAME": "42/merge",
        "GITHUB_HEAD_REF": "feature/cart",
        "GITHUB_RUN_ID": "99",
        "GITHUB_EVENT_PATH": str(event),
    }
    context = ci_context(env)
    assert context.system == CI_GITHUB
    # A pull request's source branch, not the merge ref.
    assert context.ref == "feature/cart"
    assert context.default_branch == "main"
    assert context.repository_url == "https://github.com/shop/web"
    assert context.pipeline_url == "https://github.com/shop/web/actions/runs/99"

    pushed = ci_context({**env, "GITHUB_HEAD_REF": "", "GITHUB_REF_NAME": "main"})
    assert pushed.on_default_branch

    unreadable = ci_context({**env, "GITHUB_EVENT_PATH": str(tmp_path / "missing.json")})
    assert unreadable.default_branch is None
    assert not unreadable.on_default_branch


def _github_env(tmp_path: Path, payload: object, **variables: str) -> dict[str, str]:
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload), encoding="utf-8")
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "shop/web",
        "GITHUB_SHA": SHA,
        "GITHUB_RUN_ID": "99",
        "GITHUB_EVENT_PATH": str(event),
        **variables,
    }


@pytest.mark.parametrize("event_name", ["pull_request", "pull_request_target", "merge_group"])
def test_a_github_pull_request_from_the_default_branch_is_history_only(
    tmp_path: Path, event_name: str
) -> None:
    env = _github_env(
        tmp_path,
        {"repository": {"default_branch": "main"}},
        GITHUB_EVENT_NAME=event_name,
        GITHUB_HEAD_REF="main",
        GITHUB_BASE_REF="release",
        GITHUB_REF_NAME="7/merge",
    )
    context = ci_context(env)
    assert (context.ref, context.default_branch) == ("main", "main")
    assert context.merge_request
    assert not context.on_default_branch
    assert "pull request" in uploaded_line(context, 3)


def test_a_scheduled_github_run_is_on_the_default_branch(tmp_path: Path) -> None:
    # GitHub runs a schedule on the default branch, and its payload names no repository.
    env = _github_env(
        tmp_path,
        {"schedule": "30 4 * * *", "workflow": ".github/workflows/tripl.yml"},
        GITHUB_EVENT_NAME="schedule",
        GITHUB_REF_NAME="main",
    )
    context = ci_context(env)
    assert context.default_branch == "main"
    assert context.on_default_branch
    assert not context.merge_request


def test_outside_ci_or_with_an_empty_variable_there_is_no_upload() -> None:
    with pytest.raises(TriplConfigError, match="GitLab CI or GitHub Actions"):
        ci_context({})
    with pytest.raises(TriplConfigError, match="CI_COMMIT_SHA"):
        ci_context({**GITLAB, "CI_COMMIT_SHA": ""})


def test_a_url_that_is_not_http_is_left_out() -> None:
    context = ci_context({**GITLAB, "CI_PIPELINE_URL": "javascript:alert(1)"})
    assert context.pipeline_url is None


# --- the command ------------------------------------------------------------------


def test_the_run_is_uploaded_with_repository_paths_and_verdicts(
    repo: Path,
    validator: Any,
    uploads: Any,
    configured_env: None,
    gitlab: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check", "--upload"]) == 0
    captured = capsys.readouterr()
    assert uploads.call_count == 1
    body = json.loads(uploads.calls.last.request.content)
    assert {key: body[key] for key in ("repository", "commit_sha", "ref", "default_branch")} == {
        "repository": "shop/ios",
        "commit_sha": SHA,
        "ref": "main",
        "default_branch": "main",
    }
    assert body["merge_request"] is False
    assert body["pipeline_url"] == GITLAB["CI_PIPELINE_URL"]
    assert body["strict"] is False
    calls = body["calls"]
    assert [(call["path"], call["line"], call["status"]) for call in calls] == [
        ("Sources/Feed.swift", 2, "ok"),
        ("Sources/Feed.swift", 5, "ok"),
    ]
    assert [call["event_id"] for call in calls] == ["ev-1", "ev-10"]
    assert calls[0]["identity"] == "home:open:card"
    assert "Uploaded the check run (2 calls)" in captured.err
    assert "used in code shows it" in captured.err
    # Nothing about the upload reaches stdout, which may carry a document.
    assert "Uploaded" not in captured.out


def test_a_branch_run_is_said_to_be_history_only(
    repo: Path,
    validator: Any,
    uploads: Any,
    configured_env: None,
    gitlab: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CI_COMMIT_REF_NAME", "feature/cart")
    assert main(["check", "--upload", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["project"] == "demo"
    assert "history only" in captured.err
    assert json.loads(uploads.calls.last.request.content)["ref"] == "feature/cart"


def test_a_merge_request_run_is_sent_as_one(
    repo: Path,
    validator: Any,
    uploads: Any,
    configured_env: None,
    gitlab: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "merge_request_event")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "5")
    assert main(["check", "--upload"]) == 0
    body = json.loads(uploads.calls.last.request.content)
    assert (body["ref"], body["merge_request"]) == ("main", True)
    assert "history only" in capsys.readouterr().err


def test_the_default_branch_flag_is_sent(
    repo: Path,
    validator: Any,
    uploads: Any,
    configured_env: None,
    gitlab: None,
) -> None:
    assert main(["check", "--upload", "--default-branch", "release"]) == 0
    assert json.loads(uploads.calls.last.request.content)["default_branch"] == "release"


def test_a_failing_check_still_uploads_and_keeps_its_exit_code(
    repo: Path,
    validator: Any,
    uploads: Any,
    configured_env: None,
    gitlab: None,
) -> None:
    _write(
        repo,
        {"Sources/Bad.swift": 'Analytics.shared.log(category: "nowhere", action: .open)\n'},
    )
    assert main(["check", "--upload"]) == 1
    statuses = [call["status"] for call in json.loads(uploads.calls.last.request.content)["calls"]]
    assert "error" in statuses


@pytest.mark.parametrize("status", [403, 503])
def test_a_failed_upload_is_a_warning_and_never_the_exit_code(
    repo: Path,
    validator: Any,
    tripl_api: FakeInstance,
    configured_env: None,
    gitlab: None,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    tripl_api.handler(
        UPLOAD_URL,
        lambda _request: httpx.Response(status, json={"detail": "API key has read-only scope"}),
        method="POST",
    )
    assert main(["check", "--upload"]) == 0
    err = capsys.readouterr().err
    assert "Not uploaded:" in err
    assert "The check result stands." in err


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), 500],
)
def test_an_unreachable_instance_is_not_said_to_have_checked_nothing(
    repo: Path,
    validator: Any,
    tripl_api: FakeInstance,
    configured_env: None,
    gitlab: None,
    capsys: pytest.CaptureFixture[str],
    failure: Exception | int,
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        if isinstance(failure, Exception):
            raise failure
        return httpx.Response(failure, json={"detail": "down"})

    tripl_api.handler(UPLOAD_URL, respond, method="POST")
    assert main(["check", "--upload"]) == 0
    err = capsys.readouterr().err
    last = err.strip().splitlines()[-1]
    assert last.startswith("Not uploaded:")
    assert last.endswith("The check result stands.")
    # The check did run: the gate's "nothing was checked (exit 4)" is not this.
    assert "nothing was checked" not in last
    assert "exit 4" not in last


@pytest.mark.parametrize(
    ("flags", "config_extra", "reason"),
    [
        (["--branch", "checkout-redesign"], "", "checks plan branch 'checkout-redesign'"),
        ([], "branch: checkout-redesign\n", "checks plan branch 'checkout-redesign'"),
        (["--revision", REVISION], "", f"pinned to plan revision {REVISION}"),
        ([], f"revision: {REVISION}\n", f"pinned to plan revision {REVISION}"),
        (["--list"], "", "--upload records a check's verdicts, and --list checks nothing"),
        (["--payloads", "-"], "", "captured events are not in the code"),
    ],
)
def test_an_upload_that_cannot_happen_is_refused_before_any_request(
    repo: Path,
    validator: Any,
    uploads: Any,
    tripl_api: FakeInstance,
    configured_env: None,
    gitlab: None,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    config_extra: str,
    reason: str,
) -> None:
    tripl_api.branches("demo", [make_branch()])
    if config_extra:
        _write(repo, {".tripl/check.yml": CONFIG + config_extra})
    assert main(["check", "--upload", *flags]) == 2
    assert reason in capsys.readouterr().err
    assert not validator.called
    assert not uploads.called


def test_the_live_revision_may_be_uploaded(
    repo: Path, validator: Any, uploads: Any, configured_env: None, gitlab: None
) -> None:
    _write(repo, {".tripl/check.yml": CONFIG + f"revision: {REVISION}\n"})
    assert main(["check", "--upload", "--revision", "live"]) == 0
    assert uploads.call_count == 1


def test_outside_ci_upload_is_refused_before_any_request(
    repo: Path,
    validator: Any,
    uploads: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check", "--upload"]) == 2
    assert "GitLab CI or GitHub Actions" in capsys.readouterr().err
    assert not validator.called
    assert not uploads.called


def test_the_default_branch_flag_needs_upload(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--default-branch", "main"]) == 2
    assert "pass --upload, or drop --default-branch" in capsys.readouterr().err
    assert not validator.called


def test_without_upload_nothing_is_uploaded(
    repo: Path, validator: Any, uploads: Any, configured_env: None, gitlab: None
) -> None:
    assert main(["check"]) == 0
    assert not uploads.called


def test_whoami_tells_a_check_upload_key_by_its_prefix() -> None:
    # A project-bound key cannot read /auth/me, so its access comes from the prefix.
    assert access_of_key("tk_c_abcdef") == ACCESS_CHECK_UPLOAD


# --- the check directory, the run's time, and the JSON document --------------------


def test_the_check_directory_is_relative_to_the_repository(tmp_path: Path) -> None:
    app = tmp_path / "apps" / "ios"
    app.mkdir(parents=True)
    assert check_directory(app, tmp_path) == "apps/ios"
    assert check_directory(tmp_path, tmp_path) == "."
    # Unknown, or outside the repository: one snapshot for the whole repository.
    assert check_directory(None, tmp_path) == "."
    assert check_directory(app, None) == "."
    assert check_directory(tmp_path, app) == "."


def test_an_app_in_a_subdirectory_is_uploaded_under_its_check_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    uploads: Any,
    configured_env: None,
    gitlab: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "mono"
    _write(
        root,
        {
            "apps/ios/.tripl/check.yml": CONFIG,
            "apps/ios/Sources/Feed.swift": """
                func show() {
                    Analytics.shared.log(category: "home", action: "open", label: "card")
                }
            """,
        },
    )
    (root / ".git").mkdir()
    monkeypatch.chdir(root / "apps" / "ios")
    assert main(["check", "--upload"]) == 0
    body = json.loads(uploads.calls.last.request.content)
    assert body["check_root"] == "apps/ios"
    assert [call["path"] for call in body["calls"]] == ["apps/ios/Sources/Feed.swift"]
    assert "shop/ios (apps/ios)" in capsys.readouterr().err


def test_a_run_at_the_repository_root_is_sent_as_one(
    repo: Path, validator: Any, uploads: Any, configured_env: None, gitlab: None
) -> None:
    assert main(["check", "--upload"]) == 0
    assert json.loads(uploads.calls.last.request.content)["check_root"] == "."


def test_gitlab_sends_when_the_pipeline_and_the_commit_were_made() -> None:
    context = ci_context(
        {
            **GITLAB,
            "CI_PIPELINE_CREATED_AT": "2026-11-20T10:00:00Z",
            "CI_COMMIT_TIMESTAMP": "2026-11-20T09:58:12+01:00",
        }
    )
    assert context.pipeline_created_at == "2026-11-20T10:00:00Z"
    assert context.committed_at == "2026-11-20T09:58:12+01:00"
    # A value that is no timestamp orders nothing, and is not sent.
    unreadable = ci_context({**GITLAB, "CI_PIPELINE_CREATED_AT": "yesterday"})
    assert unreadable.pipeline_created_at is None
    assert ci_context(GITLAB).committed_at is None


def test_github_sends_the_pushed_commit_s_timestamp(tmp_path: Path) -> None:
    env = _github_env(
        tmp_path,
        {
            "repository": {"default_branch": "main"},
            "head_commit": {"timestamp": "2026-11-20T09:58:12-05:00"},
        },
        GITHUB_EVENT_NAME="push",
        GITHUB_REF_NAME="main",
    )
    context = ci_context(env)
    assert context.committed_at == "2026-11-20T09:58:12-05:00"
    assert context.pipeline_created_at is None


def test_the_times_go_in_the_body(
    repo: Path,
    validator: Any,
    uploads: Any,
    configured_env: None,
    gitlab: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI_PIPELINE_CREATED_AT", "2026-11-20T10:00:00Z")
    monkeypatch.setenv("CI_COMMIT_TIMESTAMP", "2026-11-20T09:58:12Z")
    assert main(["check", "--upload"]) == 0
    body = json.loads(uploads.calls.last.request.content)
    assert (body["pipeline_created_at"], body["committed_at"]) == (
        "2026-11-20T10:00:00Z",
        "2026-11-20T09:58:12Z",
    )


def test_an_older_default_branch_run_is_said_to_be_history_only(
    repo: Path,
    validator: Any,
    tripl_api: FakeInstance,
    configured_env: None,
    gitlab: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.handler(
        UPLOAD_URL,
        lambda _request: httpx.Response(201, json={"id": "run-2", "is_current": False}),
        method="POST",
    )
    assert main(["check", "--upload"]) == 0
    err = capsys.readouterr().err
    assert "history only (older than the current main run)" in err
    assert "used in code shows it" not in err


def test_json_says_what_the_upload_recorded(
    repo: Path,
    validator: Any,
    tripl_api: FakeInstance,
    configured_env: None,
    gitlab: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.handler(
        UPLOAD_URL,
        lambda _request: httpx.Response(201, json={"id": "run-1", "is_current": True}),
        method="POST",
    )
    assert main(["check", "--upload", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["upload"] == {
        "ok": True,
        "error": None,
        "run_id": "run-1",
        "current": True,
        "repository": "shop/ios",
        "check_root": ".",
        "commit_sha": SHA,
        "ref": "main",
        "default_branch": "main",
        "merge_request": False,
        "calls": 2,
    }
    assert document["exit_code"] == 0


def test_json_says_why_an_upload_failed_and_keeps_the_exit_code(
    repo: Path,
    validator: Any,
    tripl_api: FakeInstance,
    configured_env: None,
    gitlab: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.handler(
        UPLOAD_URL,
        lambda _request: httpx.Response(403, json={"detail": "API key has read-only scope"}),
        method="POST",
    )
    assert main(["check", "--upload", "--json"]) == 0
    captured = capsys.readouterr()
    upload = json.loads(captured.out)["upload"]
    assert upload["ok"] is False
    assert "read-only scope" in upload["error"]
    assert (upload["run_id"], upload["current"]) == (None, None)
    # Still a warning on stderr, after the document.
    assert captured.err.strip().splitlines()[-1].startswith("Not uploaded:")


def test_json_has_no_upload_key_without_upload(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--json"]) == 0
    assert "upload" not in json.loads(capsys.readouterr().out)
