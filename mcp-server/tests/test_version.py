"""The version single-source guarantee, and the two places that report it.

Mirrors cli/tests/test_cli.py's TestVersion: pyproject is the single source and
importlib.metadata is the only reader, so a bump can never reach the wheel while
leaving the User-Agent or the --help banner behind.
"""

from __future__ import annotations

import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

import tripl_mcp
from tripl_mcp import DISTRIBUTION_NAME, USER_AGENT
from tripl_mcp.server import main

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


class TestVersion:
    def test_packaged_version_matches_the_runtime_one(self) -> None:
        """Drift guard: pyproject is the single source, metadata is the reader.

        Relies on `uv run` reinstalling the project when pyproject.toml moves,
        which it does. A failure here means either the bump never reached the
        installed metadata, or a second version literal was introduced.
        """
        declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

        assert tripl_mcp.__version__ == declared

    def test_user_agent_carries_the_distribution_version(self) -> None:
        # Asserting against importlib.metadata rather than a literal is the
        # point — a literal here would recreate the duplication just removed.
        expected = f"tripl-mcp/{version(DISTRIBUTION_NAME)}"

        assert expected == USER_AGENT

    def test_help_banner_reports_the_distribution_version(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The other consumer: argparse's description, which operators read."""
        monkeypatch.setattr(sys, "argv", ["tripl-mcp", "--help"])

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 0
        # Substring, not equality: argparse rewraps the description to the
        # terminal width, which the test process does not control.
        assert version(DISTRIBUTION_NAME) in capsys.readouterr().out
