import json
import sys
from pathlib import Path

import pytest

from tripl_cli.cli import main


def test_record_checkout(tmp_path: Path) -> None:
    output = tmp_path / "capture.json"
    checkout = Path(__file__).parents[1] / "examples/checkout/checkout.py"
    assert (
        main(
            [
                "journey-capture",
                "--build",
                "v1",
                "--journey",
                "checkout",
                "--fixture",
                "synthetic-order-v1",
                "--output",
                str(output),
                "--allow-event",
                "purchase",
                "--",
                sys.executable,
                str(checkout),
                "healthy",
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["runs"] == [["purchase"], ["purchase"]]


def test_failed_journey_does_not_publish(tmp_path: Path) -> None:
    output = tmp_path / "capture.json"
    assert (
        main(
            [
                "journey-capture",
                "--build",
                "v1",
                "--journey",
                "checkout",
                "--fixture",
                "order",
                "--output",
                str(output),
                "--allow-event",
                "purchase",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(1)",
            ]
        )
        != 0
    )
    assert not output.exists()


def test_it_reads_no_connection_settings_so_a_malformed_one_does_not_stop_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRIPL_BASE_URL", "tripl.example.com")  # no scheme
    output = tmp_path / "capture.json"
    checkout = Path(__file__).parents[1] / "examples/checkout/checkout.py"
    code = main(
        [
            "journey-capture",
            "--config",
            str(tmp_path / "missing.toml"),
            "--build",
            "v1",
            "--journey",
            "checkout",
            "--fixture",
            "synthetic-order-v1",
            "--output",
            str(output),
            "--allow-event",
            "purchase",
            "--",
            sys.executable,
            str(checkout),
            "healthy",
        ]
    )
    assert code == 0
    assert "TRIPL_BASE_URL" not in capsys.readouterr().err
    assert json.loads(output.read_text())["runs"] == [["purchase"], ["purchase"]]
