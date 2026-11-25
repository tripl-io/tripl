"""``tripl check --revision``: the run is judged by a stored plan revision, and says so."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tripl_cli.cli import main

from .conftest import FakeInstance, make_branch
from .test_check_cmd import CONFIG, VALIDATE_URL, _respond, _write
from .test_check_cmd import repo as repo  # noqa: PLC0414 - the fixture

REVISION = "0a1b2c3d-0000-4000-8000-00000000beef"
PLAN_HASH = "ab" * 32
TAKEN = "2026-11-20T09:00:00Z"


def _pinned(request: httpx.Request) -> httpx.Response:
    """The validator, naming the revision it was asked for as the route does."""
    body = _respond(request).json()
    asked = request.url.params.get("revision")
    body["plan"] = (
        {"revision": asked, "plan_hash": PLAN_HASH, "created_at": TAKEN}
        if asked
        else {"revision": None, "plan_hash": None, "created_at": None}
    )
    return httpx.Response(200, json=body)


@pytest.fixture
def validator(tripl_api: FakeInstance) -> Any:
    return tripl_api.handler(VALIDATE_URL, _pinned, method="POST")


def test_a_revision_is_sent_and_named_in_the_report(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--revision", REVISION]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith(
        "2 call sites in 1 file: 2 ok, 0 warnings, 0 errors.\n"
        f"Plan: revision {REVISION} (hash abababababab, taken {TAKEN})."
    )
    params = validator.calls.last.request.url.params
    assert params["revision"] == REVISION
    assert "branch" not in params


def test_the_live_plan_sends_no_revision_and_reports_none(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--json"]) == 0
    captured = capsys.readouterr()
    assert "revision" not in validator.calls.last.request.url.params
    assert "plan" not in json.loads(captured.out)
    assert "Plan:" not in captured.err


def test_the_check_config_pins_and_the_flag_overrides_or_unpins(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    other = "11111111-2222-4333-8444-555555555555"
    _write(repo, {".tripl/check.yml": CONFIG + f"revision: {REVISION}\n"})

    assert main(["check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["plan"] == {
        "revision": REVISION,
        "plan_hash": PLAN_HASH,
        "created_at": TAKEN,
    }

    assert main(["check", "--revision", other, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["plan"]["revision"] == other

    assert main(["check", "--revision", "live", "--json"]) == 0
    assert "plan" not in json.loads(capsys.readouterr().out)
    assert "revision" not in validator.calls.last.request.url.params


def test_an_instance_that_ignores_the_revision_fails_the_run(
    repo: Path, tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """An older instance answers from the live plan: a pinned gate must not pass on that."""
    tripl_api.handler(VALIDATE_URL, _respond, method="POST")  # no `plan` in the reply
    assert main(["check", "--revision", REVISION]) == 1
    captured = capsys.readouterr()
    assert f"did not validate against plan revision {REVISION}" in captured.err
    assert "call sites" not in captured.out


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        (["--revision", "latest"], "is not a plan revision id: 'latest'"),
        (
            ["--revision", REVISION, "--branch", "checkout-redesign"],
            "a run checks against one plan",
        ),
        (["--revision", REVISION, "--coverage"], "--coverage reads the events of the live plan"),
        (["--revision", REVISION, "--list"], "--list sends nothing"),
    ],
)
def test_a_revision_that_cannot_be_used_is_refused_before_any_request(
    repo: Path,
    validator: Any,
    tripl_api: FakeInstance,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    reason: str,
) -> None:
    tripl_api.branches("demo", [make_branch()])
    assert main(["check", *flags]) == 2
    assert reason in capsys.readouterr().err
    assert not validator.called


def test_a_pinned_config_refuses_a_branch_and_says_how_to_unpin(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, {".tripl/check.yml": CONFIG + f"revision: {REVISION}\n"})
    assert main(["check", "--branch", "checkout-redesign"]) == 2
    err = capsys.readouterr().err
    assert "`revision` in the check config pins plan revision" in err
    assert "--revision live" in err


def test_the_markdown_summary_names_the_revision(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--revision", REVISION, "--format", "markdown"]) == 0
    assert f"Project `demo`, plan revision `{REVISION}`." in capsys.readouterr().out
