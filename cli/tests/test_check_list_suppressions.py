"""``tripl check --list`` shows the ``tripl-check-ignore`` markers: the audit view.

Under each matched call, the valid markers covering it; after the summary, how
many markers there are and the ones that suppress nothing, because they are
malformed or sit on no matched call. ``--list`` sends nothing, so it never
says a marker is unused: that takes verdicts.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest

from tripl_cli.check import yamlish
from tripl_cli.check.config import parse
from tripl_cli.check.inventory import inventory, summarize
from tripl_cli.check.scan import scan
from tripl_cli.cli import main

CONFIG = """
project: demo
sources: ["src/**"]
event_types:
  legacy:
    calls: [{function: "track", name_arg: 0}]
"""

MARKED = """
// tripl-check-ignore[unknown_event] -- staged rollout
track("one")
track("two") // tripl-check-ignore[deprecated_event,unknown_field:plan] -- gone   in 5.0
setup()
// tripl-check-ignore[unknown_event]
track("three")
// tripl-check-ignore -- no code
// tripl-check-ignore[no_verdict] -- cannot be waived
// tripl-check-ignore[unknown_event] -- the call below is not tracking
setup()
track(name)
"""


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any request fails the test: --list must never open a connection."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("tripl check --list sent a request")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)


def _checkout(root: Path, files: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Path:
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    (root / ".git").mkdir()
    monkeypatch.chdir(root)
    return root.resolve()


def test_the_text_names_the_markers_on_each_call_and_the_ones_that_suppress_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _checkout(tmp_path, {".tripl/check.yml": CONFIG, "src/a.ts": MARKED}, monkeypatch)
    assert main(["check", "--list"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        f"tripl check --list - {root / '.tripl' / 'check.yml'}, root {root} (nothing is sent)\n"
        "\n"
        "src/a.ts:2:1  legacy  track  name=one\n"
        # A fully read call shows its markers too.
        "    ignore: tripl-check-ignore[unknown_event] -- staged rollout (line 1)\n"
        "src/a.ts:3:1  legacy  track  name=two\n"
        "    ignore: tripl-check-ignore[deprecated_event,unknown_field:plan] -- gone in 5.0 "
        "(line 3)\n"
        # The marker above it has no reason, so nothing covers this call.
        "src/a.ts:6:1  legacy  track  name=three\n"
        "src/a.ts:11:1  legacy  track  name=?\n"
        "    name: variable name\n"
        "\n"
        "4 call sites in 1 file: 3 fully read; 1 not sent (no name or field read); "
        "0 sent with a value not read.\n"
        "Values not read: name 1.\n"
        "Read per event type: legacy: name 4.\n"
        "\n"
        "Why (call sites, per target):\n"
        "  name  variable  1  a variable the scan does not follow (a property, a lambda "
        "parameter, a captured variable), not a constant\n"
        "\n"
        "Suppression markers: 6 read, covering 2 call sites.\n"
        'Suppression ignored: src/a.ts:5 tripl-check-ignore needs "-- <reason>".\n'
        "Suppression ignored: src/a.ts:7 tripl-check-ignore needs the codes it waives, "
        "as tripl-check-ignore[<code>].\n"
        "Suppression ignored: src/a.ts:8 tripl-check-ignore cannot waive no_verdict.\n"
        "Suppression on no matched call: src/a.ts:9 tripl-check-ignore[unknown_event].\n"
        "\n"
        "Calls that look like tracking but match no call spec: none.\n"
    )


def test_the_json_document_carries_the_markers_per_call_and_as_a_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _checkout(tmp_path, {".tripl/check.yml": CONFIG, "src/a.ts": MARKED}, monkeypatch)
    assert main(["check", "--list", "--json"]) == 0
    captured = capsys.readouterr()
    # The document alone is on stdout; the listing, marker lines included, on stderr.
    document = json.loads(captured.out)
    assert "Suppression markers: 6 read, covering 2 call sites.\n" in captured.err
    assert "    ignore: tripl-check-ignore[unknown_event] -- staged rollout (line 1)\n" in (
        captured.err
    )

    assert document["summary"]["suppressions"] == {
        "markers": 6,
        "invalid": 3,
        "no_call": 1,
        "calls_covered": 2,
    }
    assert [call["suppressions"] for call in document["calls"]] == [
        [
            {
                "location": {"path": "src/a.ts", "line": 1, "column": 4},
                "targets": [{"code": "unknown_event", "field": None}],
                "reason": "staged rollout",
            }
        ],
        [
            {
                "location": {"path": "src/a.ts", "line": 3, "column": 17},
                "targets": [
                    {"code": "deprecated_event", "field": None},
                    {"code": "unknown_field", "field": "plan"},
                ],
                "reason": "gone   in 5.0",
            }
        ],
        [],
        [],
    ]
    assert document["suppressions"][0] == {
        "location": {"path": "src/a.ts", "line": 1, "column": 4},
        "text": "tripl-check-ignore[unknown_event] -- staged rollout",
        "targets": [{"code": "unknown_event", "field": None}],
        "reason": "staged rollout",
        "problem": None,
        "calls": 1,
    }
    assert [
        (entry["location"]["line"], entry["problem"], entry["reason"], entry["calls"])
        for entry in document["suppressions"]
    ] == [
        (1, None, "staged rollout", 1),
        (3, None, "gone   in 5.0", 1),
        (5, "no_reason", None, 0),
        (7, "no_codes", "no code", 0),
        (8, "not_suppressible", "cannot be waived", 0),
        (9, None, "the call below is not tracking", 0),
    ]
    # The keys of a document entry are in the documented order.
    assert list(document)[-4:] == ["calls", "suppressions", "forwarders", "unmatched"]


def test_one_marker_covering_two_calls_counts_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = """
        track("one"); track("two") // tripl-check-ignore[unknown_event] -- both are staged
        builder("three")
            // tripl-check-ignore[unknown_field] -- set by the proxy
            .with("plan", "pro")
            .send()
    """
    config = """
        project: demo
        sources: ["src/**"]
        event_types:
          legacy:
            calls:
              - {function: "track", name_arg: 0}
              - {function: "builder", name_arg: 0, chain: {with: field}}
    """
    _checkout(tmp_path, {".tripl/check.yml": config, "src/a.ts": source}, monkeypatch)
    assert main(["check", "--list", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert [entry["calls"] for entry in document["suppressions"]] == [2, 1]
    assert document["summary"]["suppressions"] == {
        "markers": 2,
        "invalid": 0,
        "no_call": 0,
        "calls_covered": 3,
    }
    # The marker inside the builder chain is on the call the chain belongs to.
    assert [
        [s["location"]["line"] for s in call["suppressions"]] for call in document["calls"]
    ] == [
        [1],
        [1],
        [3],
    ]


def test_a_repository_with_no_marker_prints_no_marker_line_and_empty_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {".tripl/check.yml": CONFIG, "src/a.ts": 'track("one")\n'}
    _checkout(tmp_path, files, monkeypatch)
    assert main(["check", "--list"]) == 0
    text = capsys.readouterr().out
    assert "Suppression" not in text and "ignore" not in text
    assert main(["check", "--list", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["suppressions"] == []
    assert document["calls"][0]["suppressions"] == []
    assert document["summary"]["suppressions"] == {
        "markers": 0,
        "invalid": 0,
        "no_call": 0,
        "calls_covered": 0,
    }


def test_with_no_check_config_valid_markers_are_counted_not_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing matches in discovery mode, so "on no matched call" would name every marker."""
    _checkout(tmp_path, {"src/a.ts": MARKED}, monkeypatch)
    assert main(["check", "--list"]) == 0
    text = capsys.readouterr().out
    assert "Suppression markers: 6 read.\n" in text
    assert text.count("Suppression ignored: ") == 3
    assert "Suppression on no matched call" not in text
    assert "ignore: " not in text
    assert main(["check", "--list", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["calls"] == []
    assert [entry["calls"] for entry in document["suppressions"]] == [0] * 6


def test_the_listed_items_carry_the_suppressions_the_scan_attaches(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text(textwrap.dedent(MARKED).lstrip("\n"), encoding="utf-8")
    config = parse(yamlish.loads(textwrap.dedent(CONFIG)), path=None, base=tmp_path)

    found = inventory(config)
    outcome = scan(config)

    listed = [item for call in found.calls for item in call.items]
    assert [item.suppressions for item in listed] == [item.suppressions for item in outcome.items]
    assert [call.suppressions for call in found.calls] == [item.suppressions for item in listed]
    assert [entry.marker for entry in found.markers] == list(outcome.markers)
    assert [entry.calls for entry in found.markers] == [1, 1, 0, 0, 0, 0]
    summary = summarize(found)
    assert (summary.markers, summary.markers_invalid, summary.markers_no_call) == (6, 3, 1)
    assert summary.calls_covered == 2
