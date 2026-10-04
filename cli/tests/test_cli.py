"""Parser wiring, exit codes, and the version single-source guarantee."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

import pytest

import tripl_cli
from tripl_cli import cli as cli_module
from tripl_cli.cli import main
from tripl_cli.config import Config
from tripl_cli.errors import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, TriplAPIError, TriplConfigError

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# Installs a dummy subcommand whose handler raises the given exception.
InstallRaisingHandler = Callable[[Exception], None]


class TestVersion:
    def test_version_flag_prints_distribution_version_to_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse's version action exits; asserting against importlib.metadata
        # rather than a literal is the point — a literal here would recreate the
        # duplication the design removes.
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])

        assert excinfo.value.code == EXIT_OK
        captured = capsys.readouterr()
        assert captured.out == f"tripl {version('tripl')}\n"
        assert captured.err == ""

    def test_packaged_version_matches_the_runtime_one(self) -> None:
        """Drift guard: pyproject is the single source, metadata is the reader.

        Relies on `uv run` reinstalling the project when pyproject.toml moves,
        which it does. A failure here means either the bump never reached the
        installed metadata, or a second version literal was introduced.
        """
        declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

        assert tripl_cli.__version__ == declared

    def test_module_entry_point_works(self) -> None:
        """`python -m tripl_cli` — the console-script-free path, for real."""
        result = subprocess.run(
            [sys.executable, "-m", "tripl_cli", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == EXIT_OK
        assert result.stdout.strip() == f"tripl {version('tripl')}"


class TestParser:
    def test_help_exits_zero_and_lists_the_global_flags(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])

        assert excinfo.value.code == EXIT_OK
        out = capsys.readouterr().out
        assert "--url" in out
        assert "--base-url" in out
        assert "--api-key" in out
        assert "--config" in out

    def test_bare_invocation_prints_help_to_stderr_and_fails(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exiting 0 would let a script with a missing subcommand look successful."""
        code = main([])

        captured = capsys.readouterr()
        assert code == EXIT_USAGE
        assert captured.out == ""
        assert "usage: tripl" in captured.err

    def test_unknown_subcommand_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["nope"])

        assert excinfo.value.code == EXIT_USAGE


def _register_dummy(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
    handler: object,
) -> None:
    sub = subparsers.add_parser("dummy", parents=[parent])
    sub.set_defaults(handler=handler)


class TestGlobalFlagSeam:
    """The reason this file exists before any subcommand does.

    ``doctor`` and ``status`` rely on the global flags working on
    both sides of the subcommand name. That works only because every shared
    option uses ``default=argparse.SUPPRESS``: _SubParsersAction copies its whole
    namespace back over the parent's, so an ordinary ``default=None`` would
    erase a value given before the subcommand. Pinned here, one issue early.
    """

    @pytest.fixture
    def seen(self, monkeypatch: pytest.MonkeyPatch) -> list[Config]:
        captured: list[Config] = []

        def handler(_: argparse.Namespace, config: Config) -> int:
            captured.append(config)
            return EXIT_OK

        def register_all(
            subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
            parent: argparse.ArgumentParser,
        ) -> None:
            _register_dummy(subparsers, parent, handler)

        monkeypatch.setattr(cli_module, "register_all", register_all)
        return captured

    def test_flag_before_the_subcommand(self, seen: list[Config]) -> None:
        assert main(["--url", "https://before.example.com", "dummy"]) == EXIT_OK

        assert seen[0].base_url == "https://before.example.com"

    def test_flag_after_the_subcommand(self, seen: list[Config]) -> None:
        assert main(["dummy", "--url", "https://after.example.com"]) == EXIT_OK

        assert seen[0].base_url == "https://after.example.com"

    def test_base_url_is_the_same_option_under_another_name(self, seen: list[Config]) -> None:
        assert main(["dummy", "--base-url", "https://alias.example.com"]) == EXIT_OK

        assert seen[0].base_url == "https://alias.example.com"
        assert seen[0].sources["base_url"] == "--url"


class TestErrorRendering:
    """Expected failures are a message and an exit code, never a traceback."""

    @pytest.fixture
    def raising(self, monkeypatch: pytest.MonkeyPatch) -> InstallRaisingHandler:
        def install(exc: Exception) -> None:
            def handler(_: argparse.Namespace, __: Config) -> int:
                raise exc

            def register_all(
                subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
                parent: argparse.ArgumentParser,
            ) -> None:
                _register_dummy(subparsers, parent, handler)

            monkeypatch.setattr(cli_module, "register_all", register_all)

        return install

    def test_api_error_exits_one(
        self, raising: InstallRaisingHandler, capsys: pytest.CaptureFixture[str]
    ) -> None:
        raising(TriplAPIError(503, "down", "tripl API error (503): down"))

        code = main(["dummy"])

        assert code == EXIT_FAILURE
        assert capsys.readouterr().err == "tripl: tripl API error (503): down\n"

    def test_config_error_exits_two(
        self, raising: InstallRaisingHandler, capsys: pytest.CaptureFixture[str]
    ) -> None:
        raising(TriplConfigError("no tripl instance URL configured."))

        code = main(["dummy"])

        assert code == EXIT_USAGE
        assert "tripl: no tripl instance URL configured." in capsys.readouterr().err
