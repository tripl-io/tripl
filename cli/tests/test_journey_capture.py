import json
import sys
from pathlib import Path

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
