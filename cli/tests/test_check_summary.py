"""``tripl check --format markdown``: what a change does to the analytics, as a note."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tripl_cli.check import summary
from tripl_cli.check.model import CheckItem, CheckResult, Origin
from tripl_cli.cli import main

from .test_check_gate_cmd import change
from .test_check_gate_cmd import repo as repo  # noqa: PLC0414 - the fixture
from .test_check_gate_cmd import validator as validator  # noqa: PLC0414 - the fixture
from .test_check_list_doc_samples import GUIDE_PATH, _fenced_block

FEED = """\
func show(sheetId: String) {
    Analytics.shared.log(category: .home,
                         action: .open,
                         label: "card",
                         value: "1")
    Analytics.shared.logEvent("promo_sheet_\\(sheetId)_shown")
}
"""

NEW = """\
func start() {
    Analytics.shared.logEvent("trial_started")
    Analytics.shared.logEvent("brand_new")
}
"""


def _patch(repo: Path, edits: dict[str, str | None]) -> str:
    (repo / "change.patch").write_text(change(repo, edits), encoding="utf-8")
    return str(repo / "change.patch")


def test_the_summary_lists_the_events_a_change_adds_changes_and_removes(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    patch = _patch(
        repo, {"Sources/New.swift": NEW, "Sources/Feed.swift": FEED, "Sources/Legacy.swift": None}
    )
    assert main(["check", "--new-from-patch", patch, "--format", "markdown"]) == 1
    assert capsys.readouterr().out == (
        "### tripl check: analytics changes\n"
        "\n"
        "Project `demo`.\n"
        "\n"
        f"Compared with the base of the patch `{patch}`.\n"
        "\n"
        "**2 events added, 1 changed, 1 removed.**\n"
        "\n"
        "| Change | Event type | Event | In the plan | Details |\n"
        "|--------|------------|-------|-------------|---------|\n"
        "| Added | `legacy` | `brand_new` | **no** | `Sources/New.swift:3` |\n"
        "| Added | `legacy` | `trial_started` | yes | `Sources/New.swift:2` |\n"
        "| Changed | `se` | `home:open:card` | yes | now sends `value` |\n"
        "| Removed | `se` | `map:open:x` | **no** | was at `Sources/Legacy.swift:3` |\n"
        "\n"
        "**Check failed:** 5 call sites, 4 ok, 0 warnings, 1 error, 1 call failing the run.\n"
        "- `Sources/New.swift:3` error `unknown_event`: no such event\n"
    )


def test_a_removed_event_is_asked_about_once_and_nothing_else_twice(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The base's verdicts come from this run's where the call is still there."""
    patch = _patch(repo, {"Sources/Legacy.swift": None})
    assert main(["check", "--new-from-patch", patch, "--format", "markdown"]) == 0
    assert "| Removed | `se` | `map:open:x` | **no** |" in capsys.readouterr().out
    head, base = (len(json.loads(call.request.content)["items"]) for call in validator.calls)
    assert (head, base) == (2, 1)  # two distinct calls today; the one the change removed


def test_a_change_that_touches_no_tracking_call_says_so(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    patch = _patch(repo, {"README.md": "hello\n"})
    assert main(["check", "--new-from-patch", patch, "--format", "markdown"]) == 0
    out = capsys.readouterr().out
    assert "**No event added, removed or changed.**" in out
    # Legacy.swift's old error is not new in the change, so the run passes.
    assert "**Check passed:** 4 call sites, 3 ok, 0 warnings, 1 error." in out
    assert len(validator.calls) == 1


def test_without_a_gate_the_summary_holds_the_findings_alone(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--format", "markdown"]) == 1
    out = capsys.readouterr().out
    assert "No change was compared: run with `--new-from-rev REF`" in out
    assert "| Change |" not in out
    assert "- `Sources/Legacy.swift:3` error `unknown_event`: no such event" in out


def test_the_summary_is_a_report_like_any_other(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    patch = _patch(repo, {"Sources/New.swift": NEW})
    note = repo / "note.md"
    assert main(["check", "--new-from-patch", patch, "--report", f"markdown={note}"]) == 1
    assert "2 events added, 0 changed, 0 removed." in note.read_text(encoding="utf-8")
    assert "Gate:" in capsys.readouterr().out  # stdout is still the text report


def test_a_base_that_cannot_be_rebuilt_is_said_not_guessed(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A patch naming a file this checkout lacks is not of this tree: no table."""
    patch = repo / "other.patch"
    patch.write_text(
        "diff --git a/Sources/Gone.swift b/Sources/Gone.swift\n"
        "--- a/Sources/Gone.swift\n+++ b/Sources/Gone.swift\n@@ -1 +1 @@\n-a\n+b\n",
        encoding="utf-8",
    )
    main(["check", "--new-from-patch", str(patch), "--format", "markdown"])
    out = capsys.readouterr().out
    assert "The base could not be rebuilt from the change" in out
    assert "| Change |" not in out


# --- the comparison, on results built by hand ----------------------------------------
def _result(
    name: str | None,
    *,
    fields: dict[str, str | None] | None = None,
    properties: dict[str, Any] | None = None,
    event_id: str | None = "e",
    line: int = 1,
) -> CheckResult:
    item = CheckItem(Origin("a.kt", line, 1), "event", name, fields or {}, properties, False)
    return CheckResult(item, "ok", event_id=event_id, identity=name)


def test_a_moved_call_is_no_change() -> None:
    found = summary.compare([_result("open", line=3)], [_result("open", line=40)])
    assert found.changes == ()


def test_property_keys_count_as_what_an_event_sends() -> None:
    found = summary.compare(
        [_result("open", properties={"step": 1})], [_result("open", properties={"coupon": "x"})]
    )
    [changed] = found.changes
    assert (changed.kind, changed.added_keys, changed.removed_keys) == (
        "changed",
        ("coupon",),
        ("step",),
    )


def test_a_call_whose_event_was_not_read_is_counted_and_never_compared() -> None:
    found = summary.compare([_result(None)], [_result(None), _result("open")])
    assert [(change.kind, change.event) for change in found.changes] == [("added", "open")]
    assert found.unread == 2


def test_a_name_cannot_break_out_of_its_table_cell() -> None:
    assert summary._code("a|b`c\nd") == "`a\\|b'c d`"


def test_the_summary_names_the_markers_that_fail_the_run(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = "// tripl-check-ignore[unknown_field] -- stale\nfunc o() {}\n"
    (repo / "Sources/Old.swift").write_text(stale, encoding="utf-8")
    (repo / "Sources/Legacy.swift").unlink()  # the one call of the fixture that fails
    assert main(["check", "--format", "markdown"]) == 0
    assert "suppression marker" not in capsys.readouterr().out
    assert main(["check", "--format", "markdown", "--strict-suppressions"]) == 1
    out = capsys.readouterr().out
    assert "**Check failed:**" in out
    assert out.endswith(
        "\n"
        "**1 suppression marker fails the run** (`--strict-suppressions`):\n"
        "- `Sources/Old.swift:1` `tripl-check-ignore[unknown_field]` matched no finding\n"
    )


def test_the_summary_shows_a_malformed_marker_as_code(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # No reason, and a field name a Markdown renderer would read as HTML.
    broken = "// tripl-check-ignore[unknown_field:<b>`x`</b>]\nfunc o() {}\n"
    (repo / "Sources/Old.swift").write_text(broken, encoding="utf-8")
    (repo / "Sources/Legacy.swift").unlink()
    assert main(["check", "--format", "markdown", "--strict-suppressions"]) == 1
    assert capsys.readouterr().out.endswith(
        "\n"
        "**1 suppression marker fails the run** (`--strict-suppressions`):\n"
        "- `Sources/Old.swift:1` `tripl-check-ignore[unknown_field:<b>'x'</b>]` gives no "
        "reason after `--`, so it suppresses nothing\n"
    )


def test_the_summary_sample_in_the_guide_is_real_output(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    documented = _fenced_block(GUIDE_PATH, "## Summarise a change {#summary}", "markdown")
    patch = _patch(
        repo, {"Sources/New.swift": NEW, "Sources/Feed.swift": FEED, "Sources/Legacy.swift": None}
    )

    assert main(["check", "--new-from-patch", patch, "--format", "markdown"]) == 1

    assert capsys.readouterr().out.replace(patch, "change.patch") == documented
