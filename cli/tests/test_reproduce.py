import json
from pathlib import Path

import pytest

from tripl_cli.cli import main

FIXTURE = Path(__file__).with_name("journey_fixture")


def test_known_regression(capsys: pytest.CaptureFixture[str]) -> None:
    args = [
        "reproduce",
        "--event",
        "purchase",
        "--healthy",
        str(FIXTURE / "healthy.json"),
        "--suspect",
        str(FIXTURE / "suspect.json"),
        "--json",
    ]
    assert main(args) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"] == "reproduced"
    assert report["healthy"]["count"] == 1
    assert report["suspect"]["count"] == 0
    assert main([*args, "--candidate", str(FIXTURE / "candidate.json")]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "verified"


@pytest.mark.parametrize(
    ("build", "runs", "fixture", "expected", "code"),
    [
        ("bad", [["purchase", "purchase"]] * 2, "synthetic-order-v1", "reproduced", 3),
        ("bad", [["purchase"]] * 2, "synthetic-order-v1", "no_regression", 0),
        ("bad", [["purchase"]], "synthetic-order-v1", "inconclusive", 4),
        ("bad", [["purchase"], []], "synthetic-order-v1", "inconclusive", 4),
        ("v1", [[], []], "synthetic-order-v1", "inconclusive", 4),
        ("bad", [], "synthetic-order-v1", "inconclusive", 4),
        ("bad", [[], []], "other-order", "inconclusive", 4),
    ],
)
def test_comparison(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    build: str,
    runs: list[list[str]],
    fixture: str,
    expected: str,
    code: int,
) -> None:
    capture = tmp_path / "suspect.json"
    capture.write_text(
        json.dumps(
            dict(
                build=build, runs=runs, fixture=fixture, journey="checkout", allowlist=["purchase"]
            )
        )
    )
    args = [
        "reproduce",
        "--event",
        "purchase",
        "--healthy",
        str(FIXTURE / "healthy.json"),
        "--suspect",
        str(capture),
        "--json",
    ]
    assert main(args) == code
    assert json.loads(capsys.readouterr().out)["verdict"] == expected


def test_missing_and_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "capture.json"
    args = [
        "reproduce",
        "--event",
        "purchase",
        "--healthy",
        str(path),
        "--suspect",
        str(FIXTURE / "suspect.json"),
        "--json",
    ]
    assert main(args) == 4
    assert json.loads(capsys.readouterr().out)["healthy"] is None
    path.write_text(json.dumps({"unapproved_property": "customer secret"}))
    assert main(args) == 2
    assert "customer secret" not in capsys.readouterr().err


def test_candidate_still_failing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidate = tmp_path / "candidate.json"
    capture = json.loads((FIXTURE / "suspect.json").read_text())
    candidate.write_text(json.dumps({**capture, "build": "candidate"}))
    assert (
        main(
            [
                "reproduce",
                "--event",
                "purchase",
                "--healthy",
                str(FIXTURE / "healthy.json"),
                "--suspect",
                str(FIXTURE / "suspect.json"),
                "--candidate",
                str(candidate),
                "--json",
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out)["verdict"] == "still_failing"


def test_event_filtered_from_suspect(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suspect = tmp_path / "filtered.json"
    capture = json.loads((FIXTURE / "suspect.json").read_text())
    suspect.write_text(json.dumps({**capture, "allowlist": ["checkout"]}))
    assert (
        main(
            [
                "reproduce",
                "--event",
                "purchase",
                "--healthy",
                str(FIXTURE / "healthy.json"),
                "--suspect",
                str(suspect),
                "--json",
            ]
        )
        == 4
    )
    assert json.loads(capsys.readouterr().out)["verdict"] == "inconclusive"


def test_candidate_introduces_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suspect = tmp_path / "suspect.json"
    capture = json.loads((FIXTURE / "healthy.json").read_text())
    suspect.write_text(json.dumps({**capture, "build": "reviewed-build"}))
    assert (
        main(
            [
                "reproduce",
                "--event",
                "purchase",
                "--healthy",
                str(FIXTURE / "healthy.json"),
                "--suspect",
                str(suspect),
                "--candidate",
                str(FIXTURE / "suspect.json"),
                "--json",
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out)["verdict"] == "still_failing"


@pytest.mark.parametrize("stable", [True, False])
def test_first_difference_only_on_stable_shared_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stable: bool
) -> None:
    base = {"fixture": "order-1", "journey": "checkout"}
    for role, runs, names in [
        ("healthy", [["debug", "open", "purchase"]] * 2, ["debug", "open", "purchase"]),
        ("suspect", [["open"], ["open"] if stable else []], ["open", "purchase"]),
    ]:
        (tmp_path / role).write_text(json.dumps(dict(base, build=role, allowlist=names, runs=runs)))
    assert (
        main(
            [
                "reproduce",
                "--event",
                "purchase",
                "--healthy",
                str(tmp_path / "healthy"),
                "--suspect",
                str(tmp_path / "suspect"),
                "--json",
            ]
        )
        == 3
    )
    report = json.loads(capsys.readouterr().out)
    if stable:
        assert report["first_difference"] == {"position": 2, "healthy": "purchase", "suspect": None}
    else:
        assert "first_difference" not in report


def test_it_reads_no_connection_settings_so_a_malformed_one_does_not_stop_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRIPL_BASE_URL", "tripl.example.com")  # no scheme
    args = [
        "reproduce",
        "--config",
        str(tmp_path / "missing.toml"),
        "--event",
        "purchase",
        "--healthy",
        str(FIXTURE / "healthy.json"),
        "--suspect",
        str(FIXTURE / "suspect.json"),
        "--json",
    ]
    assert main(args) == 3
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["verdict"] == "reproduced"
