"""`check`, `codegen` and `export` read `.tripl/check.yml` through one module.

`tripl check` carried its own copies of the `--check-config` and `--project`
flags and of the lookup and "which project" rules that `codegen` and `export`
share, so a change to one (a new config file name, a new flag help) could miss
the other.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from tripl_cli.cli import build_parser, main


def _command(name: str) -> argparse.ArgumentParser:
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]
    raise AssertionError("the tripl parser has no subcommands")


def _option(parser: argparse.ArgumentParser, flag: str) -> argparse.Action:
    return next(action for action in parser._actions if flag in action.option_strings)


@pytest.mark.parametrize("flag", ["--check-config", "--project"])
def test_the_three_commands_take_the_same_flag(flag: str) -> None:
    actions = [_option(_command(name), flag) for name in ("check", "codegen", "export")]

    shapes = {(a.dest, a.metavar, a.help, type(a).__name__) for a in actions}
    assert len(shapes) == 1, shapes


def test_check_refuses_two_projects_naming_itself(
    configured_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["check", "--payloads", "-", "--project", "a", "--project", "b"]) == 2

    assert "--project was given 2 times; tripl check reads one project." in capsys.readouterr().err
