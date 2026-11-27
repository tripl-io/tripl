"""``--branch-if-exists``: the plan branch when it exists and is open, else main, and saying which.

Also the warning ``--branch`` gives on a merged or closed branch, whose reads
the instance answers from the copy the branch held when it ended.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tripl_cli.check.config import load
from tripl_cli.cli import main
from tripl_cli.errors import TriplConfigError
from tripl_cli.render import plan_branch_note

from .conftest import FakeInstance, make_branch
from .test_check_cmd import CONFIG, VALIDATE_URL, _write
from .test_check_cmd import repo as repo  # noqa: PLC0414 - the fixture
from .test_check_cmd import validator as validator  # noqa: PLC0414 - the fixture
from .test_check_revision import REVISION, _pinned

MAIN_ROW = make_branch("b-main", name="main", kind="main", status="merged")
TALLY = "2 call sites in 1 file: 2 ok, 0 warnings, 0 errors."


def test_an_open_branch_is_used(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [MAIN_ROW, make_branch()])
    assert main(["check", "--branch-if-exists", "checkout-redesign", "--json"]) == 0
    assert validator.calls.last.request.url.params["branch"] == "b-9f21"
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["branch"] == {"id": "b-9f21", "name": "checkout-redesign"}
    assert document["plan_branch"] == {
        "requested": "checkout-redesign",
        "status": "draft",
        "used": "branch",
    }
    assert "Plan branch" not in captured.err


def test_a_branch_nobody_made_reads_main_and_says_so(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [MAIN_ROW, make_branch()])
    assert main(["check", "--branch-if-exists", "feature/login"]) == 0
    assert "branch" not in validator.calls.last.request.url.params
    assert (
        capsys.readouterr()
        .out.rstrip()
        .endswith(f"{TALLY}\nPlan branch feature/login not found: used the main plan.")
    )


@pytest.mark.parametrize("status", ["merged", "closed"])
def test_a_merged_or_closed_branch_reads_main_and_says_so(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    tripl_api.branches("demo", [MAIN_ROW, make_branch(status=status)])
    assert main(["check", "--branch-if-exists", "checkout-redesign", "--json"]) == 0
    assert "branch" not in validator.calls.last.request.url.params
    captured = capsys.readouterr()
    # The note is a human line: beside a JSON document it goes to stderr.
    assert f"Plan branch checkout-redesign is {status}: used the main plan." in captured.err
    document = json.loads(captured.out)
    assert document["branch"] is None
    assert document["plan_branch"] == {
        "requested": "checkout-redesign",
        "status": status,
        "used": "main",
    }


@pytest.mark.parametrize("selector", ["main", "b-main"])
def test_naming_main_itself_is_main_with_nothing_to_report(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    selector: str,
) -> None:
    # Main is stored with status "merged"; that must not read as a merged branch.
    tripl_api.branches("demo", [MAIN_ROW])
    assert main(["check", "--branch-if-exists", selector, "--json"]) == 0
    assert len(validator.calls) == 1
    assert "branch" not in validator.calls.last.request.url.params
    captured = capsys.readouterr()
    assert json.loads(captured.out)["plan_branch"] is None
    assert "Plan branch" not in captured.err


def test_no_branch_asked_for_reports_none(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["plan_branch"] is None


@pytest.mark.parametrize("status", ["merged", "closed"])
def test_branch_on_an_ended_branch_warns_and_still_reads_it(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    tripl_api.branches("demo", [MAIN_ROW, make_branch(status=status)])
    assert main(["check", "--branch", "checkout-redesign"]) == 0
    assert validator.calls.last.request.url.params["branch"] == "b-9f21"
    assert (
        f"Warning: plan branch checkout-redesign is {status}, so this run read its frozen "
        "copy, not the main plan."
    ) in capsys.readouterr().out


def test_the_warning_goes_to_stderr_beside_a_json_document(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [make_branch(status="merged")])
    assert main(["check", "--branch", "checkout-redesign", "--json"]) == 0
    captured = capsys.readouterr()
    assert "Warning: plan branch checkout-redesign is merged" in captured.err
    assert json.loads(captured.out)["plan_branch"] == {
        "requested": "checkout-redesign",
        "status": "merged",
        "used": "branch",
    }


def test_branch_on_an_open_branch_or_on_main_says_nothing(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [MAIN_ROW, make_branch()])
    assert main(["check", "--branch", "checkout-redesign"]) == 0
    assert main(["check", "--branch", "main"]) == 0
    out = capsys.readouterr().out
    assert "Warning" not in out
    assert "Plan branch" not in out


def test_branch_still_refuses_a_branch_nobody_made(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [make_branch()])
    assert main(["check", "--branch", "feature/login"]) == 2
    assert "no branch matches 'feature/login'" in capsys.readouterr().err
    assert not validator.called


def test_the_markdown_summary_carries_the_note(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [make_branch(status="merged")])
    assert main(["check", "--branch-if-exists", "checkout-redesign", "--format", "markdown"]) == 0
    out = capsys.readouterr().out
    assert "Project `demo`.\n\nPlan branch checkout-redesign is merged: used the main plan." in out


# --- where the request comes from ---------------------------------------------------
def test_the_environment_variable_is_a_branch_if_exists(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [make_branch()])
    monkeypatch.setenv("TRIPL_PLAN_BRANCH", "checkout-redesign")
    assert main(["check"]) == 0
    assert validator.calls.last.request.url.params["branch"] == "b-9f21"
    monkeypatch.setenv("TRIPL_PLAN_BRANCH", "feature/login")
    assert main(["check"]) == 0
    assert "branch" not in validator.calls.last.request.url.params
    assert "Plan branch feature/login not found" in capsys.readouterr().out


def test_an_empty_environment_variable_is_unset(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = tripl_api.branches("demo", [make_branch()])
    monkeypatch.setenv("TRIPL_PLAN_BRANCH", "")
    assert main(["check"]) == 0
    assert not listing.called


def test_a_flag_wins_over_the_environment_and_the_environment_over_the_config(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tripl_api.branches("demo", [make_branch(), make_branch("b-2", name="other")])
    _write(repo, {".tripl/check.yml": CONFIG + "branch: missing\n"})
    monkeypatch.setenv("TRIPL_PLAN_BRANCH", "other")
    assert main(["check"]) == 0
    assert validator.calls.last.request.url.params["branch"] == "b-2"
    assert main(["check", "--branch", "checkout-redesign"]) == 0
    assert validator.calls.last.request.url.params["branch"] == "b-9f21"


def test_the_config_key_reads_main_when_the_branch_is_missing(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [make_branch()])
    _write(repo, {".tripl/check.yml": CONFIG + "branch_if_exists: feature/login\n"})
    assert main(["check"]) == 0
    assert "branch" not in validator.calls.last.request.url.params
    assert "Plan branch feature/login not found" in capsys.readouterr().out


def test_both_config_keys_in_one_file_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "check.yml"
    path.write_text("project: demo\nbranch: a\nbranch_if_exists: b\n", encoding="utf-8")
    with pytest.raises(TriplConfigError, match="`branch` and `branch_if_exists` both name"):
        load(path)


# --- refusals ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        (
            ["--branch", "a", "--branch-if-exists", "b"],
            "--branch and --branch-if-exists both name the plan branch",
        ),
        (["--branch-if-exists", "a", "--list"], "drop --branch-if-exists"),
        (["--branch-if-exists", "a", "--revision", REVISION], "a run checks against one plan"),
    ],
)
def test_a_flag_that_cannot_be_used_is_refused_before_any_request(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    reason: str,
) -> None:
    listing = tripl_api.branches("demo", [make_branch()])
    assert main(["check", *flags]) == 2
    assert reason in capsys.readouterr().err
    assert not listing.called
    assert not validator.called


def test_a_pinned_config_refuses_the_flag(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, {".tripl/check.yml": CONFIG + f"revision: {REVISION}\n"})
    assert main(["check", "--branch-if-exists", "checkout-redesign"]) == 2
    assert "`revision` in the check config pins plan revision" in capsys.readouterr().err


@pytest.mark.parametrize("pinned_by", ["flag", "config"])
def test_a_pinned_run_ignores_the_environment_variable_and_says_so(
    repo: Path,
    tripl_api: FakeInstance,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pinned_by: str,
) -> None:
    route = tripl_api.handler(VALIDATE_URL, _pinned, method="POST")
    listing = tripl_api.branches("demo", [make_branch()])
    monkeypatch.setenv("TRIPL_PLAN_BRANCH", "checkout-redesign")
    flags: list[str] = []
    if pinned_by == "flag":
        flags = ["--revision", REVISION]
    else:
        _write(repo, {".tripl/check.yml": CONFIG + f"revision: {REVISION}\n"})
    assert main(["check", *flags]) == 0
    params = route.calls.last.request.url.params
    assert params["revision"] == REVISION
    assert "branch" not in params
    assert not listing.called
    assert (
        f"TRIPL_PLAN_BRANCH is ignored: this run is pinned to plan revision {REVISION}."
        in capsys.readouterr().err
    )


def test_the_environment_variable_does_not_hide_a_config_that_contradicts_itself(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `branch` beside `revision` is refused; a variable the pipeline exports
    # for every job must not turn that refusal into a passing run.
    listing = tripl_api.branches("demo", [make_branch()])
    _write(repo, {".tripl/check.yml": CONFIG + f"branch: old\nrevision: {REVISION}\n"})
    monkeypatch.setenv("TRIPL_PLAN_BRANCH", "checkout-redesign")
    assert main(["check"]) == 2
    assert "and plan branch 'old' is asked for too" in capsys.readouterr().err
    assert not listing.called
    assert not validator.called


def test_the_environment_says_when_it_replaces_the_config_branch(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tripl_api.branches("demo", [make_branch()])
    _write(repo, {".tripl/check.yml": CONFIG + "branch: checkout-redesign\n"})
    monkeypatch.setenv("TRIPL_PLAN_BRANCH", "feature/x")
    assert main(["check"]) == 0
    assert "branch" not in validator.calls.last.request.url.params
    captured = capsys.readouterr()
    assert (
        "TRIPL_PLAN_BRANCH names plan branch feature/x, so `branch` in the check config "
        "(checkout-redesign) is not used." in captured.err
    )
    assert "Plan branch feature/x not found: used the main plan." in captured.out


def test_an_empty_flag_names_no_branch(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # What `--branch-if-exists "$VARIABLE"` passes when the pipeline has no such
    # variable: no branch is asked for, as with an empty TRIPL_PLAN_BRANCH.
    listing = tripl_api.branches("demo", [make_branch()])
    assert main(["check", "--branch-if-exists", "", "--format", "json"]) == 0
    assert not listing.called
    captured = capsys.readouterr()
    assert json.loads(captured.out)["plan_branch"] is None
    assert "Plan branch" not in captured.err
    # It still cannot stand beside --branch.
    assert main(["check", "--branch", "checkout-redesign", "--branch-if-exists", " "]) == 2


def test_list_ignores_the_environment_variable(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRIPL_PLAN_BRANCH", "checkout-redesign")
    assert main(["check", "--list"]) == 0
    assert "TRIPL_PLAN_BRANCH" not in capsys.readouterr().err


# --- the docs ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "page", ["website/docs/run/cli.md", "website/docs/integrate/tripl-check.md"]
)
def test_the_lines_the_docs_show_are_the_lines_a_run_prints(page: str) -> None:
    text = (Path(__file__).resolve().parents[2] / page).read_text(encoding="utf-8")
    shown = [
        plan_branch_note("checkout-v2", None, used_main=True),
        plan_branch_note("checkout-v2", "merged", used_main=True),
        plan_branch_note("checkout-v2", "merged", used_main=False),
    ]
    for line in shown:
        assert line is not None
        assert f"\n{line}\n" in text
