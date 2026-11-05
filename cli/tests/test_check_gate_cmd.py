"""``tripl check --new-from-patch`` / ``--new-from-rev`` end to end: only what is new fails.

The synthetic validator is ``test_check_cmd``'s. Patches are made here from
the files before and after each edit, in git's format, so the gate is tested
without git; the last block runs the same through a real temporary repository
(skipped without git), whose user is set locally and which reads no global or
system git config.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from tripl_cli.check import changes as changes_mod
from tripl_cli.cli import main
from tripl_cli.commands import check as check_cmd
from tripl_cli.install.shell import Command, CommandResult

from .conftest import FakeInstance, make_branch
from .test_check_cmd import CONFIG, VALIDATE_URL, _respond, _verdict, _write

LEGACY_CALL = 'Analytics.shared.log(category: "map", action: "open", label: "x")'


@pytest.fixture
def validator(tripl_api: FakeInstance) -> Any:
    return tripl_api.handler(VALIDATE_URL, _respond, method="POST")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``test_check_cmd``'s checkout, plus a legacy error and a call through a constant."""
    root = tmp_path / "repo"
    _write(
        root,
        {
            ".tripl/check.yml": CONFIG,
            "Tracking/Events.swift": """
                enum Category: String { case home, profile }
                enum Action: String { case open }
            """,
            "Sources/Feed.swift": """
                func show(sheetId: String) {
                    Analytics.shared.log(category: .home,
                                         action: .open,
                                         label: "card")
                    Analytics.shared.logEvent("promo_sheet_\\(sheetId)_shown")
                }
            """,
            "Sources/Legacy.swift": f"""
                // An old screen.
                func legacy() {{
                    {LEGACY_CALL}
                }}
            """,
            "Sources/Screens.swift": """
                enum Screens {
                    static let home = "home"
                }
            """,
            "Sources/Uses.swift": """
                func uses() {
                    Analytics.shared.log(category: Screens.home, action: "open", label: "card")
                }
            """,
        },
    )
    (root / ".git").mkdir()
    monkeypatch.chdir(root / "Sources")
    return root


def change(root: Path, edits: dict[str, str | None]) -> str:
    """Apply ``edits`` (None deletes); the patch from before to after, as git prints it."""
    parts: list[str] = []
    for relative, text in edits.items():
        path = root / relative
        before = path.read_text(encoding="utf-8") if path.exists() else None
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        old = [] if before is None else before.splitlines(keepends=True)
        new = [] if text is None else text.splitlines(keepends=True)
        header = f"diff --git a/{relative} b/{relative}\n"
        if before is None:
            header += "new file mode 100644\n"
        elif text is None:
            header += "deleted file mode 100644\n"
        body = list(
            difflib.unified_diff(
                old,
                new,
                DEV_NULL if before is None else f"a/{relative}",
                DEV_NULL if text is None else f"b/{relative}",
                n=0,
            )
        )
        parts.append(header + "".join(body))
    return "".join(parts)


DEV_NULL = "/dev/null"


def save(root: Path, patch: str) -> str:
    path = root / "change.diff"
    path.write_text(patch, encoding="utf-8")
    return str(path)


def gate(root: Path, edits: dict[str, str | None], *extra: str) -> int:
    return main(["check", "--new-from-patch", save(root, change(root, edits)), *extra])


# --- what is new ------------------------------------------------------------------------
def test_only_a_new_call_fails_and_only_it_is_listed(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check"]) == 1  # the legacy error, with no gate
    capsys.readouterr()
    new = 'func pay() { Analytics.shared.log(category: "checkout", action: "pay", label: "x") }\n'
    assert gate(repo, {"Sources/New.swift": new}) == 1
    out = capsys.readouterr().out
    assert "Sources/New.swift:1:14  error  se  checkout:pay:x" in out
    assert "Legacy.swift" not in out
    assert (
        "(1 file, 1 call site new or changed). 1 call site fails the run; "
        "1 error is not new in this change." in out
    )


def test_a_legacy_finding_at_a_call_the_change_left_alone_does_not_fail(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # The plan-edit case: the call errors today, and the change is elsewhere.
    feed = (repo / "Sources/Feed.swift").read_text(encoding="utf-8")
    assert gate(repo, {"Sources/Feed.swift": "// A note.\n" + feed}) == 0
    assert "0 call sites fail the run; 1 error is not new" in capsys.readouterr().out


def test_moving_or_reformatting_a_call_keeps_it_unchanged(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    moved = (
        "// An old screen,\n// moved down the file.\n\n\nfunc legacy() {\n"
        '    Analytics.shared.log(\n        category: "map",\n        action: "open",\n'
        '        label: "x"\n    )\n}\n'
    )
    assert gate(repo, {"Sources/Legacy.swift": moved}) == 0
    out = capsys.readouterr().out
    assert "(1 file, 0 call sites new or changed)" in out


def test_editing_a_call_makes_it_new(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    edited = f"func legacy() {{\n    {LEGACY_CALL.replace('x', 'y')}\n}}\n"
    assert gate(repo, {"Sources/Legacy.swift": edited}) == 1
    assert "Sources/Legacy.swift:2:5  error  se  map:open:y" in capsys.readouterr().out


def test_a_constant_edited_in_another_file_fails_the_unchanged_call(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    screens = 'enum Screens {\n    static let home = "map"\n}\n'
    assert gate(repo, {"Sources/Screens.swift": screens}) == 1
    out = capsys.readouterr().out
    assert "Sources/Uses.swift:2:5  error  se  map:open:card" in out
    assert "Legacy.swift" not in out


def test_an_enums_file_edit_fails_the_calls_it_changes(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    events = (
        'enum Category: String { case home = "house", profile }\n'
        "enum Action: String { case open }\n"
    )
    assert gate(repo, {"Tracking/Events.swift": events}) == 1
    out = capsys.readouterr().out
    assert "Sources/Feed.swift:2:5  error  se  house:open:card" in out
    assert "(1 file, 1 call site new or changed)" in out


def test_a_renamed_file_keeps_its_calls_unchanged(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "Sources/Legacy.swift").rename(repo / "Sources/Renamed.swift")
    patch = (
        "diff --git a/Sources/Legacy.swift b/Sources/Renamed.swift\n"
        "similarity index 100%\n"
        "rename from Sources/Legacy.swift\n"
        "rename to Sources/Renamed.swift\n"
    )
    assert main(["check", "--new-from-patch", save(repo, patch)]) == 0
    assert "1 error is not new" in capsys.readouterr().out


def test_a_check_config_edit_never_makes_a_legacy_finding_new(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    config = (repo / ".tripl/check.yml").read_text(encoding="utf-8") + "# tuned\n"
    assert gate(repo, {".tripl/check.yml": config}, "--json") == 0
    document = json.loads(capsys.readouterr().out)
    assert document["gate"]["changed_items"] == 0
    assert document["gate"]["changed_files"] == 1
    assert document["exit_code"] == 0


def test_a_patch_that_does_not_fit_the_tree_counts_the_file_as_changed(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    patch = (
        "diff --git a/Sources/Legacy.swift b/Sources/Legacy.swift\n"
        "--- a/Sources/Legacy.swift\n"
        "+++ b/Sources/Legacy.swift\n"
        "@@ -1 +1 @@\n"
        "-foo\n"
        "+bar\n"
    )
    assert main(["check", "--new-from-patch", save(repo, patch)]) == 1
    out = capsys.readouterr().out
    assert "Sources/Legacy.swift:3:5  error" in out
    assert (
        "Gate: the diff cannot rebuild Sources/Legacy.swift as it was, so every call there "
        "counts as changed." in out
    )


def test_an_empty_patch_is_a_gate_with_nothing_new(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--new-from-patch", save(repo, "")]) == 0
    assert "Gate: the change in" in capsys.readouterr().out


def test_a_patch_on_stdin(
    repo: Path,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    new = 'func pay() { Analytics.shared.log(category: "checkout", action: "pay", label: "x") }\n'
    patch = change(repo, {"Sources/New.swift": new})
    monkeypatch.setattr("sys.stdin", io.StringIO(patch))
    assert main(["check", "--new-from-patch", "-"]) == 1
    assert "Gate: the change in the patch on stdin" in capsys.readouterr().out


NEW_CALL = 'func pay() { Analytics.shared.log(category: "checkout", action: "pay", label: "x") }\n'


def test_a_patch_with_mnemonic_prefixes_maps_onto_the_tree(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    patch = change(repo, {"Sources/New.swift": NEW_CALL})
    patch = patch.replace(" a/Sources", " c/Sources").replace("b/Sources", "w/Sources")
    assert "+++ w/Sources/New.swift" in patch
    assert main(["check", "--new-from-patch", save(repo, patch)]) == 1
    out = capsys.readouterr().out
    assert "Sources/New.swift:1:14  error" in out
    assert "(1 file, 1 call site new or changed)" in out


def test_a_patch_naming_files_the_tree_does_not_have_fails_closed(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    patch = (
        "diff --git a/elsewhere/Sources/New.swift b/elsewhere/Sources/New.swift\n"
        "--- a/elsewhere/Sources/New.swift\n"
        "+++ b/elsewhere/Sources/New.swift\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    assert main(["check", "--new-from-patch", save(repo, patch), "--json"]) == 1
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["gate"]["unmapped"] == ["elsewhere/Sources/New.swift"]
    # Every item is changed: the legacy error fails, as with no gate.
    assert document["gate"]["changed_items"] == len(document["results"])
    assert "Gate: the diff names elsewhere/Sources/New.swift, which this checkout" in (captured.err)


def test_a_submodule_the_change_moves_counts_every_call_in_it_as_changed(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, {"Sources/vendor/Track.swift": f"func v() {{ {LEGACY_CALL} }}\n"})
    patch = (
        "diff --git a/Sources/vendor b/Sources/vendor\n"
        "index 1111111..2222222 160000\n"
        "--- a/Sources/vendor\n"
        "+++ b/Sources/vendor\n"
        "@@ -1 +1 @@\n"
        "-Subproject commit 1111111111111111111111111111111111111111\n"
        "+Subproject commit 2222222222222222222222222222222222222222\n"
    )
    assert main(["check", "--new-from-patch", save(repo, patch)]) == 1
    out = capsys.readouterr().out
    assert "Sources/vendor/Track.swift:1:12  error" in out
    assert "Legacy.swift" not in out
    assert "Gate: the diff cannot rebuild Sources/vendor/ as it was" in out


def test_a_patch_that_only_moves_a_submodule_outside_the_scan_is_a_gate(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "third_party/lib").mkdir(parents=True)
    patch = (
        "diff --git a/third_party/lib b/third_party/lib\n"
        "index 1111111..2222222 160000\n"
        "--- a/third_party/lib\n"
        "+++ b/third_party/lib\n"
        "@@ -1 +1 @@\n"
        "-Subproject commit 1111111\n"
        "+Subproject commit 2222222\n"
    )
    assert main(["check", "--new-from-patch", save(repo, patch)]) == 0
    assert "(1 file, 0 call sites new or changed)" in capsys.readouterr().out


def test_deleting_an_enums_file_compares_the_calls_it_resolved(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # The base still has the enum: Feed.swift's `.open` read "open" there and
    # reads nothing now, so the call changed (a dynamic value, under --strict).
    assert gate(repo, {"Tracking/Events.swift": None}, "--strict") == 1
    out = capsys.readouterr().out
    assert "Sources/Feed.swift:2:5  warning  se  home:*:card" in out
    assert "dynamic_value [action]" in out


def test_a_check_root_below_the_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "mono"
    _write(
        root,
        {
            "ios/.tripl/check.yml": CONFIG,
            "ios/Sources/Legacy.swift": f"func legacy() {{\n    {LEGACY_CALL}\n}}\n",
        },
    )
    (root / ".git").mkdir()
    monkeypatch.chdir(root / "ios")
    patch = change(root, {"ios/Sources/New.swift": NEW_CALL})
    assert "+++ b/ios/Sources/New.swift" in patch
    assert main(["check", "--new-from-patch", save(root, patch)]) == 1
    out = capsys.readouterr().out
    assert "Sources/New.swift:1:14  error" in out
    assert "Legacy.swift" not in out
    # A change outside the check root is none of its business.
    patch = change(root, {"android/App.kt": "fun x() {}\n"})
    assert main(["check", "--new-from-patch", save(root, patch)]) == 0


def test_a_patch_from_another_vcs_with_no_git_is_relative_to_the_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "hg-checkout"
    _write(
        root,
        {
            "ios/.tripl/check.yml": CONFIG,
            "ios/Sources/Legacy.swift": f"func legacy() {{\n    {LEGACY_CALL}\n}}\n",
        },
    )
    patch = save(root, change(root, {"ios/Sources/New.swift": NEW_CALL}))
    config = ["--check-config", "ios/.tripl/check.yml"]
    monkeypatch.chdir(root)  # where the patch's paths start, as for git apply
    assert main(["check", *config, "--new-from-patch", patch]) == 1
    out = capsys.readouterr().out
    assert "Sources/New.swift:1:14  error" in out
    assert "Legacy.swift" not in out
    # From the check root the same paths name nothing: fail closed.
    monkeypatch.chdir(root / "ios")
    assert main(["check", "--new-from-patch", patch]) == 1
    captured = capsys.readouterr()
    assert "Sources/Legacy.swift:2:5  error" in captured.out
    assert "Gate: the diff names ios/Sources/New.swift, which this checkout" in captured.out


def test_a_patch_whose_hunks_do_not_fit_a_file_outside_the_scan_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A patch made relative to the check root, whose path also names another file above it."""
    screens = 'enum Screens {\n    static let home = "home"\n}\n'
    root = tmp_path / "mono"
    _write(
        root,
        {
            "app/.tripl/check.yml": CONFIG,
            "app/Sources/Screens.swift": screens,
            "app/Sources/Uses.swift": (
                "func uses() {\n"
                '    Analytics.shared.log(category: Screens.home, action: "open", label: "card")\n'
                "}\n"
            ),
            "Sources/Screens.swift": screens,
        },
    )
    (root / ".git").mkdir()
    monkeypatch.chdir(root / "app")
    patch = change(root / "app", {"Sources/Screens.swift": screens.replace('"home"', '"map"')})
    assert main(["check", "--new-from-patch", save(root, patch)]) == 1
    out = capsys.readouterr().out
    assert "Sources/Uses.swift:2:5  error  se  map:open:card" in out
    assert "Gate: the diff cannot rebuild Sources/Screens.swift as it was" in out


def test_a_deleted_file_whose_base_cannot_be_rebuilt_makes_every_call_changed(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # An enums file marked -diff: its deletion is a binary change, so what it
    # defined is unknown, and so is every call that may have read it.
    (repo / "Tracking/Events.swift").unlink()
    patch = (
        "diff --git a/Tracking/Events.swift b/Tracking/Events.swift\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "Binary files a/Tracking/Events.swift and /dev/null differ\n"
    )
    assert main(["check", "--new-from-patch", save(repo, patch), "--json"]) == 1
    gate = json.loads(capsys.readouterr().out)["gate"]
    assert gate["unreconstructable"] == ["Tracking/Events.swift"]
    assert gate["changed_items"] == gate["failing"] + 3  # every row: three of them pass


# --- symlinks -----------------------------------------------------------------------------
def test_a_scanned_link_changes_with_its_target_outside_the_sources(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo, {"Shared/Track.swift": 'func s() { Analytics.shared.logEvent("trial_started") }\n'}
    )
    (repo / "Sources/Track.swift").symlink_to("../Shared/Track.swift")
    edited = f"func s() {{ {LEGACY_CALL.replace('x', 'linked')} }}\n"
    assert gate(repo, {"Shared/Track.swift": edited}) == 1
    out = capsys.readouterr().out
    assert "Sources/Track.swift:1:12  error  se  map:open:linked" in out
    assert "Legacy.swift" not in out


def test_a_scanned_link_changes_with_its_excluded_target(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    config = (repo / ".tripl/check.yml").read_text(encoding="utf-8")
    config = config.replace(
        'sources: ["Sources/**"]', 'sources: ["Sources/**"]\nexclude: ["Sources/Vendor/**"]'
    )
    _write(
        repo,
        {
            ".tripl/check.yml": config,
            "Sources/Vendor/Real.swift": (
                'func r() { Analytics.shared.logEvent("trial_started") }\n'
            ),
        },
    )
    (repo / "Sources/Track.swift").symlink_to("Vendor/Real.swift")
    edited = f"func r() {{ {LEGACY_CALL.replace('x', 'vendored')} }}\n"
    assert gate(repo, {"Sources/Vendor/Real.swift": edited}) == 1
    assert "Sources/Track.swift:1:12  error  se  map:open:vendored" in capsys.readouterr().out


def test_an_enums_link_changes_with_its_target(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    events = repo / "Tracking/Events.swift"
    _write(repo, {"Shared/Events.swift": events.read_text(encoding="utf-8")})
    events.unlink()
    events.symlink_to("../Shared/Events.swift")
    renamed = (
        'enum Category: String { case home = "house", profile }\n'
        "enum Action: String { case open }\n"
    )
    assert gate(repo, {"Shared/Events.swift": renamed}) == 1
    assert "Sources/Feed.swift:2:5  error  se  house:open:card" in capsys.readouterr().out


def test_sarif_marker_results_say_whether_they_are_in_the_change(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    marked = "// tripl-check-ignore[unknown_event] -- not used\n" + NEW_CALL.replace(
        '"checkout"', '"home"'
    ).replace('"pay"', '"open"').replace('"x"', '"card"')
    _write(
        repo, {"Sources/Old.swift": "// tripl-check-ignore[unknown_field] -- stale\nfunc o() {}\n"}
    )
    patch = save(repo, change(repo, {"Sources/New.swift": marked}))
    assert main(["check", "--new-from-patch", patch, "--format", "sarif"]) == 0
    run = json.loads(capsys.readouterr().out)["runs"][0]
    markers = {
        result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]: result["properties"][
            "inChange"
        ]
        for result in run["results"]
        if result["ruleId"] == "unused_suppression"
    }
    assert markers == {"Sources/New.swift": True, "Sources/Old.swift": False}


# --- the plan branch --------------------------------------------------------------------------
def _branchy(archived: set[str], *, main_status: int = 200) -> Any:
    """The synthetic validator, where the plan branch archives ``archived`` events."""

    def respond(request: httpx.Request) -> httpx.Response:
        on_branch = "branch" in request.url.params
        if not on_branch and main_status != 200:
            return httpx.Response(main_status, json={"detail": "down"})
        body = json.loads(request.content)
        verdicts = []
        for item in body["items"]:
            verdict = _verdict(item)
            if on_branch and item.get("name") in archived:
                verdict["status"] = "error"
                verdict["findings"].append(
                    {
                        "code": "deprecated_event",
                        "severity": "error",
                        "field": None,
                        "message": "archived on the branch",
                    }
                )
            verdicts.append(verdict)
        return httpx.Response(200, json={"items": verdicts, "summary": {}})

    return respond


def test_a_finding_the_plan_branch_brought_in_fails_an_unchanged_call(
    repo: Path, tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.branches("demo", [make_branch()])
    route = tripl_api.handler(VALIDATE_URL, _branchy({"trial_started"}), method="POST")
    _write(
        repo, {"Sources/Trial.swift": 'func t() { Analytics.shared.logEvent("trial_started") }\n'}
    )
    argv = ["check", "--branch", "checkout-redesign", "--json"]
    assert main([*argv, "--new-from-patch", save(repo, "")]) == 1
    document = json.loads(capsys.readouterr().out)
    # One more batch than without the gate: the unchanged failing items, against main.
    assert route.call_count == 2
    first, second = (json.loads(call.request.content)["items"] for call in route.calls)
    assert "branch" not in route.calls[1].request.url.params
    assert sorted(item["name"] or "" for item in second) == ["", "trial_started"]
    assert len(first) == 4  # Uses.swift sends what Feed.swift's first call does
    assert document["gate"]["plan_branch"] == {"compared": 2, "caused": 1}
    [trial] = [r for r in document["results"] if r["name"] == "trial_started"]
    [finding] = trial["findings"]
    assert (finding["cause"], finding["in_change"], finding["fails"]) == ("plan_branch", True, True)
    assert (trial["in_change"], trial["fails"]) == (False, True)
    [legacy] = [r for r in document["results"] if r["location"]["path"] == "Sources/Legacy.swift"]
    assert legacy["fails"] is False
    assert all(f["cause"] is None for f in legacy["findings"])


def test_an_outage_during_the_main_comparison_is_exit_4(
    repo: Path, tripl_api: FakeInstance, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    tripl_api.branches("demo", [make_branch()])
    tripl_api.handler(VALIDATE_URL, _branchy(set(), main_status=503), method="POST")
    argv = ["check", "--branch", "checkout-redesign", "--new-from-patch", save(repo, "")]
    assert main(argv) == 4


# --- no gate, refusals ---------------------------------------------------------------------
def test_an_empty_rev_gates_nothing_and_says_so(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--new-from-rev", ""]) == 1
    captured = capsys.readouterr()
    assert "--new-from-rev is empty: every finding counts." in captured.err
    assert "Gate:" not in captured.out
    assert "Sources/Legacy.swift" in captured.out


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["check", "--list", "--new-from-rev", "HEAD"],
            "--list checks nothing; drop --new-from-rev",
        ),
        (["check", "--list", "--new-from-patch", "x"], "drop --new-from-patch"),
        (
            ["check", "--payloads", "p.ndjson", "--new-from-rev", "HEAD"],
            "a captured event has no source to compare",
        ),
        (["check", "--new-from-rev=--output=x"], "a revision cannot start with '-'"),
        (["check", "--new-from-patch", "missing.diff"], "cannot read missing.diff"),
        (["check", "--new-from-patch", "../notes.txt"], "holds no diff"),
        (
            ["check", "--new-from-patch", "../plain.diff"],
            "reads diffs in git's format only",
        ),
    ],
)
def test_a_gate_that_cannot_run_is_exit_2_before_any_request(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    (repo / "notes.txt").write_text("not a diff\n", encoding="utf-8")
    (repo / "plain.diff").write_text(
        "--- Sources/Feed.swift\t2026-10-24\n+++ Sources/Feed.swift\t2026-10-24\n"
        "@@ -1 +1 @@\n-a\n+b\n",
        encoding="utf-8",
    )
    stale = repo / "Sources/stale.json"
    stale.write_text("{}", encoding="utf-8")
    report = [] if "--list" in argv else ["--report", "json=stale.json"]
    assert main([*argv, *report]) == 2
    assert message in capsys.readouterr().err
    assert len(tripl_api.router.calls) == 0
    if report:
        assert not stale.exists()


def test_both_gate_flags_at_once_are_refused_by_the_parser(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--new-from-rev", "HEAD", "--new-from-patch", "x"])
    assert excinfo.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert not validator.called


def test_a_revision_git_does_not_know_is_exit_2_with_no_request(
    repo: Path,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Command] = []

    def runner(command: Command, capture: bool) -> CommandResult:
        seen.append(command)
        if "--show-toplevel" in command.argv:
            return CommandResult(command.argv, 0, stdout=f"{repo}\n")
        return CommandResult(command.argv, 1)

    monkeypatch.setattr(changes_mod, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(check_cmd, "default_runner", lambda: runner)
    stale = repo / "Sources/out.sarif"
    stale.write_text("{}", encoding="utf-8")
    sha = "1a2b3c4d" * 5
    assert main(["check", "--new-from-rev", sha, "--report", "sarif=out.sarif"]) == 2
    err = capsys.readouterr().err
    assert f"the commit {sha} is not in this clone" in err
    assert f"git fetch --no-tags --depth=1 origin {sha}" in err
    assert len(seen) == 2
    assert len(tripl_api.router.calls) == 0
    assert not stale.exists()


# --- what each format shows ---------------------------------------------------------------------
def _untimed(text: str) -> str:
    return re.sub(r'time="[^"]*"', 'time=""', text)


def test_code_quality_and_junit_are_the_same_with_and_without_a_gate(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    reports = ["--report", "codequality=cq.json", "--report", "junit=junit.xml"]
    new = 'func pay() { Analytics.shared.log(category: "checkout", action: "pay", label: "x") }\n'
    patch = save(repo, change(repo, {"Sources/New.swift": new}))
    assert main(["check", *reports]) == 1
    plain = ((repo / "Sources/cq.json").read_text(), (repo / "Sources/junit.xml").read_text())
    assert main(["check", "--new-from-patch", patch, *reports]) == 1
    gated = ((repo / "Sources/cq.json").read_text(), (repo / "Sources/junit.xml").read_text())
    assert gated[0] == plain[0]
    assert _untimed(gated[1]) == _untimed(plain[1])
    # JUnit fails the legacy case too: more failures than the exit code counts.
    assert 'failures="2"' in gated[1]


def test_without_a_gate_flag_the_documents_have_no_gate_keys(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert "gate" not in document
    assert "failing" not in document["summary"]
    assert all("in_change" not in result for result in document["results"])
    assert main(["check", "--format", "sarif"]) == 1
    sarif = json.loads(capsys.readouterr().out)
    run = sarif["runs"][0]
    assert "gate" not in run["properties"]
    assert all("inChange" not in result["properties"] for result in run["results"])


def test_json_and_sarif_carry_the_gate(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    new = 'func pay() { Analytics.shared.log(category: "checkout", action: "pay", label: "x") }\n'
    patch = save(repo, change(repo, {"Sources/New.swift": new}))
    assert main(["check", "--new-from-patch", patch, "--json"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["gate"] == {
        "source": "patch",
        "rev": None,
        "base": None,
        "patch": patch,
        "changed_files": 1,
        "changed_items": 1,
        "unreconstructable": [],
        "unmapped": [],
        "untracked": [],
        "plan_branch": None,
        "failing": 1,
        "not_new": 1,
    }
    assert document["summary"]["failing"] == 1
    by_path = {r["location"]["path"]: r for r in document["results"]}
    assert (by_path["Sources/New.swift"]["in_change"], by_path["Sources/New.swift"]["fails"]) == (
        True,
        True,
    )
    legacy = by_path["Sources/Legacy.swift"]
    assert (legacy["status"], legacy["in_change"], legacy["fails"]) == ("error", False, False)
    assert {f["in_change"] for f in legacy["findings"]} == {False}
    assert main(["check", "--new-from-patch", patch, "--format", "sarif"]) == 1
    run = json.loads(capsys.readouterr().out)["runs"][0]
    assert run["properties"]["gate"]["changed_items"] == 1
    in_change = {
        result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]: result["properties"][
            "inChange"
        ]
        for result in run["results"]
    }
    assert in_change == {"Sources/New.swift": True, "Sources/Legacy.swift": False}
    assert all("baselineState" not in result for result in run["results"])


# --- for real, through git -----------------------------------------------------------------------
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(cwd: Path, *argv: str) -> str:
    done = subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


@needs_git
def test_new_from_rev_through_a_real_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tripl_api: FakeInstance,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "real"
    _write(
        root,
        {
            ".tripl/check.yml": CONFIG,
            "Tracking/Events.swift": "enum Category: String { case home, profile }\n",
            "Sources/Legacy.swift": f"func legacy() {{\n    {LEGACY_CALL}\n}}\n",
        },
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "tripl test")
    git(root, "config", "user.email", "test@tripl.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    monkeypatch.chdir(root)
    # The legacy error alone: nothing new.
    (root / "Sources/Legacy.swift").write_text(
        f"// moved\nfunc legacy() {{\n    {LEGACY_CALL}\n}}\n", encoding="utf-8"
    )
    assert main(["check", "--new-from-rev", "HEAD"]) == 0
    assert f"Gate: changes since {base[:12]}" in capsys.readouterr().out
    # A new error in a file git does not track yet: new, whether added or not.
    _write(root, {"Sources/Untracked.swift": f"func u() {{ {LEGACY_CALL.replace('x', 'u')} }}\n"})
    assert main(["check", "--new-from-rev", "HEAD"]) == 1
    out = capsys.readouterr().out
    assert "Sources/Untracked.swift:1:12  error" in out
    assert "Gate: git does not track Sources/Untracked.swift, so every call there" in out
    git(root, "add", "-N", "Sources/Untracked.swift")
    assert main(["check", "--new-from-rev", "HEAD"]) == 1
    assert "Sources/Untracked.swift:1:12  error" in capsys.readouterr().out
    calls = len(tripl_api.router.calls)
    assert main(["check", "--new-from-rev", "no-such-branch"]) == 2
    assert "the commit no-such-branch is not in this clone" in capsys.readouterr().err
    assert len(tripl_api.router.calls) == calls


def _real_repo(root: Path, files: dict[str, str], *config: tuple[str, str]) -> str:
    """A committed temporary repository with ``files``; its own user, and ``config`` set first."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "tripl test"),
        ("user.email", "test@tripl.invalid"),
        ("commit.gpgsign", "false"),
        *config,
    ):
        git(root, "config", key, value)
    _write(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@needs_git
def test_a_submodule_bump_that_changes_a_call_fails_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vendor = tmp_path / "vendor"
    _real_repo(vendor, {"Track.swift": 'func v() { Analytics.shared.logEvent("trial_started") }\n'})
    root = tmp_path / "app"
    _real_repo(root, {".tripl/check.yml": CONFIG, "Sources/Main.swift": "func m() {}\n"})
    git(
        root,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(vendor),
        "Sources/vendor",
    )
    git(root, "commit", "-q", "-m", "vendor")
    monkeypatch.chdir(root)
    assert main(["check", "--new-from-rev", "HEAD"]) == 0
    capsys.readouterr()
    inner = root / "Sources/vendor"
    # An untracked file in the submodule is not part of the change.
    (inner / "report.txt").write_text("left by a CI step\n", encoding="utf-8")
    assert main(["check", "--new-from-rev", "HEAD"]) == 0
    assert "Sources/vendor/" not in capsys.readouterr().out
    for key, value in (
        ("user.name", "t"),
        ("user.email", "t@tripl.invalid"),
        ("commit.gpgsign", "false"),
    ):
        git(inner, "config", key, value)
    (inner / "Track.swift").write_text(f"func v() {{ {LEGACY_CALL} }}\n", encoding="utf-8")
    git(inner, "commit", "-q", "-am", "bump")
    assert main(["check", "--new-from-rev", "HEAD"]) == 1
    out = capsys.readouterr().out
    assert "Sources/vendor/Track.swift:1:12  error" in out
    assert "Gate: the diff cannot rebuild Sources/vendor/ as it was" in out


@needs_git
def test_new_from_rev_with_a_check_root_below_the_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "mono"
    _real_repo(
        root,
        {
            "ios/.tripl/check.yml": CONFIG,
            "ios/Sources/Legacy.swift": f"func legacy() {{\n    {LEGACY_CALL}\n}}\n",
        },
    )
    monkeypatch.chdir(root / "ios")
    assert main(["check", "--new-from-rev", "HEAD"]) == 0
    capsys.readouterr()
    _write(root, {"ios/Sources/New.swift": NEW_CALL})
    git(root, "add", "-N", "ios/Sources/New.swift")
    assert main(["check", "--new-from-rev", "HEAD"]) == 1
    out = capsys.readouterr().out
    assert "Sources/New.swift:1:14  error" in out
    assert "Legacy.swift" not in out


@needs_git
@pytest.mark.parametrize("setting", ["autocrlf", "eol"])
def test_moving_a_call_in_a_crlf_checkout_stays_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    setting: str,
) -> None:
    root = tmp_path / "win"
    files = {
        ".tripl/check.yml": CONFIG,
        "Sources/Legacy.swift": f"func legacy() {{\n    {LEGACY_CALL}\n}}\n",
    }
    if setting == "eol":
        files[".gitattributes"] = "*.swift text eol=crlf\n"
        _real_repo(root, files)
        git(root, "rm", "-q", "--cached", "-r", ".")
        (root / "Sources/Legacy.swift").unlink()
        git(root, "reset", "-q", "--hard")
    else:
        _real_repo(root, files, ("core.autocrlf", "true"))
        (root / "Sources/Legacy.swift").unlink()
        git(root, "checkout", "-q", "--", "Sources/Legacy.swift")
    legacy = root / "Sources/Legacy.swift"
    assert b"\r\n" in legacy.read_bytes()
    legacy.write_bytes(b"// moved down\r\n" + legacy.read_bytes())
    monkeypatch.chdir(root)
    assert main(["check", "--new-from-rev", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert "cannot rebuild" not in out
    assert "1 error is not new" in out


@needs_git
def test_a_patch_series_is_undone_commit_by_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """git format-patch --stdout BASE, the shape of a pull request's .patch."""
    root = tmp_path / "series"
    screens = (
        'enum Screens {{\n    static let home = "{home}"\n    static let other = "{other}"\n}}\n'
    )
    base = _real_repo(
        root,
        {
            ".tripl/check.yml": CONFIG,
            "Sources/Screens.swift": screens.format(home="home", other="a"),
            "Sources/Uses.swift": (
                "func uses() {\n"
                '    Analytics.shared.log(category: Screens.home, action: "open", label: "card")\n'
                "}\n"
            ),
        },
    )
    # The first commit brings the bad constant in; the second edits the same file.
    for home, other in (("map", "a"), ("map", "b")):
        (root / "Sources/Screens.swift").write_text(screens.format(home=home, other=other))
        git(root, "commit", "-q", "-am", f"other {other}")
    series = git(root, "format-patch", "--stdout", base)
    assert series.count("diff --git a/Sources/Screens.swift") == 2
    monkeypatch.chdir(root)
    patch = root / "series.patch"
    patch.write_text(series + "\n", encoding="utf-8")
    assert main(["check", "--new-from-patch", str(patch)]) == 1
    out = capsys.readouterr().out
    assert "Sources/Uses.swift:2:5  error  se  map:open:card" in out
    assert "cannot rebuild" not in out


@needs_git
def test_a_file_the_change_stops_tracking_is_compared_with_its_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "gen"
    _real_repo(
        root,
        {".tripl/check.yml": CONFIG, "Sources/Gen.swift": f"func g() {{ {LEGACY_CALL} }}\n"},
    )
    git(root, "rm", "-q", "--cached", "Sources/Gen.swift")
    (root / ".gitignore").write_text("Sources/Gen.swift\n", encoding="utf-8")
    git(root, "add", ".gitignore")
    monkeypatch.chdir(root)
    assert main(["check", "--new-from-rev", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert "1 error is not new" in out
    assert "cannot rebuild" not in out and "does not have" not in out


# --- round three: what the base scan reads is what the scan reads -------------------------
MARKER = "// tripl-check-ignore[unknown_event,value_not_allowed:category] -- legacy screen"


def _both_flags(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, int]:
    """The same change through --new-from-rev HEAD and --new-from-patch of git's own diff."""
    rev = main(["check", "--new-from-rev", "HEAD"])
    capsys.readouterr()
    git(root, "add", "-N", ".")
    diff = subprocess.run(
        ["git", "diff", "-M", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    patch = main(["check", "--new-from-patch", save(root.parent, diff)])
    capsys.readouterr()
    return rev, patch


@needs_git
@pytest.mark.parametrize(
    ("edit", "code"),
    [
        ("removed", 1),
        ("no reason", 1),
        ("misspelled", 1),
        ("reworded", 0),
        ("copy above", 1),
        ("copy below", 1),
    ],
)
def test_what_a_call_waives_is_part_of_the_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    edit: str,
    code: int,
) -> None:
    root = tmp_path / "marked"
    marked = f"    {LEGACY_CALL}  {MARKER}\n"
    _real_repo(
        root,
        {".tripl/check.yml": CONFIG, "Sources/Legacy.swift": f"func legacy() {{\n{marked}}}\n"},
    )
    monkeypatch.chdir(root)
    assert main(["check"]) == 0
    capsys.readouterr()
    plain = f"    {LEGACY_CALL}\n"
    reworded = marked.replace("legacy screen", "the old checkout screen")
    body = {
        "removed": plain,
        "no reason": marked.replace(" -- legacy screen", ""),
        "misspelled": marked.replace("unknown_event", "unknown_evnet"),
        "reworded": reworded,
        "copy above": plain + reworded,
        "copy below": reworded + plain,
    }[edit]
    (root / "Sources/Legacy.swift").write_text(f"func legacy() {{\n{body}}}\n")
    assert main(["check"]) == (0 if edit == "reworded" else 1)
    capsys.readouterr()
    assert _both_flags(root, capsys) == (code, code)


USES_K = (
    'func uses() {\n    Analytics.shared.log(category: K.cat, action: "open", label: "card")\n}\n'
)
K_MAP = 'enum K {\n    static let cat = "map"\n}\n'
PADDING = "// " + "x" * 100 + "\n"


@needs_git
@pytest.mark.parametrize(
    ("base", "change_to", "config"),
    [
        pytest.param(
            {"Sources/Vendor/Lib/K.swift": K_MAP, "Sources/Uses.swift": USES_K},
            {"Sources/Vendor/Lib/K.swift": None, "Sources/K.swift": K_MAP},
            CONFIG.replace(
                'sources: ["Sources/**"]', 'sources: ["Sources/**"]\nexclude: ["Sources/Vendor/*"]'
            ),
            id="deleted-in-a-pruned-directory",
        ),
        pytest.param(
            {
                "Sources/Big.swift": PADDING * 20_000 + f"func b() {{ {LEGACY_CALL} }}\n",
                "Sources/Main.swift": USES_K.replace("K.cat", '"home"'),
            },
            {"Sources/Big.swift": f"func b() {{ {LEGACY_CALL} }}\n"},
            CONFIG,
            id="shrunk-under-the-size-cap",
        ),
        pytest.param(
            {"Sources/K.swift": PADDING * 20_000 + K_MAP, "Sources/Uses.swift": USES_K},
            {"Sources/K.swift": None, "Sources/K2.swift": K_MAP},
            CONFIG,
            id="oversized-constants-deleted-and-moved",
        ),
    ],
)
def test_the_base_scan_reads_only_what_the_scan_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    base: dict[str, str],
    change_to: dict[str, str | None],
    config: str,
) -> None:
    root = tmp_path / "sized"
    _real_repo(root, {".tripl/check.yml": config, **base})
    monkeypatch.chdir(root)
    assert main(["check"]) == 0  # the base passes: it never read that call or constant
    capsys.readouterr()
    for relative, text in change_to.items():
        if text is None:
            (root / relative).unlink()
        else:
            _write(root, {relative: text})
    assert main(["check"]) == 1
    capsys.readouterr()
    assert _both_flags(root, capsys) == (1, 1)


@needs_git
def test_a_link_chain_the_change_retargets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "chain"
    good = 'func s() { Analytics.shared.logEvent("trial_started") }\n'
    _write(root, {"Shared/v1.swift": good, "Shared/v2.swift": f"func s() {{ {LEGACY_CALL} }}\n"})
    (root / "Shared/current.swift").symlink_to("v1.swift")
    (root / "Sources").mkdir()
    (root / "Sources/Track.swift").symlink_to("../Shared/current.swift")
    _real_repo(root, {".tripl/check.yml": CONFIG})
    monkeypatch.chdir(root)
    assert main(["check"]) == 0
    capsys.readouterr()
    (root / "Shared/current.swift").unlink()
    (root / "Shared/current.swift").symlink_to("v2.swift")
    assert main(["check"]) == 1
    capsys.readouterr()
    assert _both_flags(root, capsys) == (1, 1)


@needs_git
def test_a_generated_untracked_source_counts_as_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Generated in CI from a tracked schema the change edits; git ignores the output."""
    root = tmp_path / "codegen"
    _real_repo(
        root,
        {
            ".gitignore": "Sources/Generated/\n",
            "schema/events.json": '{"cat": "home"}\n',
            "Sources/Uses.swift": USES_K,
        },
    )
    _write(root, {".tripl/check.yml": CONFIG})
    git(root, "add", ".tripl/check.yml")
    git(root, "commit", "-q", "-m", "config")
    _write(root, {"Sources/Generated/Gen.swift": K_MAP.replace("map", "home")})
    monkeypatch.chdir(root)
    assert main(["check", "--new-from-rev", "HEAD"]) == 0
    capsys.readouterr()
    _write(root, {"schema/events.json": '{"cat": "map"}\n', "Sources/Generated/Gen.swift": K_MAP})
    assert main(["check", "--new-from-rev", "HEAD"]) == 1
    out = capsys.readouterr().out
    assert "Sources/Uses.swift:2:5  error  se  map:open:card" in out
    assert "Gate: git does not track Sources/Generated/Gen.swift" in out


# --- round four: what a call sends, in whatever file, and what the base can see ------------
FILLER = "".join(f"func helper{index}() {{ let n = {index} }}\n" for index in range(40))
OTHER_EVENT = LEGACY_CALL.replace('"open"', '"close"')
GOOD_CALL = 'func m() { Analytics.shared.logEvent("trial_started") }\n'


def _legacy_repo(root: Path) -> None:
    _real_repo(
        root,
        {
            ".tripl/check.yml": CONFIG,
            "Sources/Legacy.swift": f"// An old screen.\nfunc legacy() {{\n    {LEGACY_CALL}\n}}\n",
            "Sources/Main.swift": "func m() {}\n",
        },
    )


@needs_git
@pytest.mark.parametrize(
    ("edits", "code"),
    [
        pytest.param(
            {
                "Sources/Legacy.swift": None,
                "Sources/Moved.swift": f"{FILLER}func moved() {{\n    {LEGACY_CALL}\n}}\n",
            },
            0,
            id="moved-to-a-new-file",
        ),
        pytest.param(
            {
                "Sources/Legacy.swift": None,
                "Sources/Moved.swift": f"{FILLER}func moved() {{\n    {OTHER_EVENT}\n}}\n",
            },
            1,
            id="moved-and-its-event-changed",
        ),
        pytest.param(
            {
                "Sources/Legacy.swift": "// An old screen.\nfunc legacy() {}\n",
                "Sources/Main.swift": f"func m() {{\n    {LEGACY_CALL}\n}}\n",
            },
            0,
            id="deleted-in-one-file-the-same-call-added-in-another",
        ),
        pytest.param(
            {"Sources/Copy.swift": f"func copy() {{\n    {LEGACY_CALL}\n}}\n"},
            1,
            id="a-copy-in-a-new-file-is-new",
        ),
    ],
)
def test_a_call_is_what_it_sends_in_whatever_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    edits: dict[str, str | None],
    code: int,
) -> None:
    root = tmp_path / "moves"
    _legacy_repo(root)
    monkeypatch.chdir(root)
    for relative, text in edits.items():
        if text is None:
            (root / relative).unlink()
        else:
            (root / relative).write_text(text, encoding="utf-8")
    git(root, "add", "-N", ".")
    assert main(["check"]) == 1
    capsys.readouterr()
    assert _both_flags(root, capsys) == (code, code)


@needs_git
def test_a_rename_with_edits_below_gits_similarity_keeps_its_legacy_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "rename"
    _legacy_repo(root)
    monkeypatch.chdir(root)
    git(root, "mv", "Sources/Legacy.swift", "Sources/Screens.swift")
    (root / "Sources/Screens.swift").write_text(
        f"{FILLER}func legacy() {{\n    {LEGACY_CALL}\n}}\n{FILLER}", encoding="utf-8"
    )
    diff = git(root, "diff", "-M", "HEAD")
    assert "rename from" not in diff  # git sees a delete and an add
    assert _both_flags(root, capsys) == (0, 0)


def test_reordered_named_arguments_with_runtime_values_are_the_same_call(
    repo: Path, validator: Any, configured_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    before = (
        "fun a(x: String, y: String) {\n"
        '    Analytics.shared.log(category = "map", action = x, label = y)\n}\n'
    )
    after = (
        "fun a(x: String, y: String) {\n"
        '    Analytics.shared.log(label = y, category = "map", action = x)\n}\n'
    )
    _write(repo, {"Sources/A.kt": before})
    assert gate(repo, {"Sources/A.kt": after}) == 0
    capsys.readouterr()
    assert gate(repo, {"Sources/A.kt": after.replace("label = y", 'label = "z"')}) == 1


def _submodule(tmp_path: Path, files: dict[str, str], at: str) -> tuple[Path, Path]:
    vendor = tmp_path / "vendor"
    _real_repo(vendor, files)
    root = tmp_path / "app"
    _real_repo(root, {".tripl/check.yml": CONFIG, "Sources/Main.swift": "func m() {}\n"})
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(vendor), at)
    git(root, "commit", "-q", "-m", "vendor")
    inner = root / at
    for key, value in (
        ("user.name", "t"),
        ("user.email", "t@tripl.invalid"),
        ("commit.gpgsign", "false"),
    ):
        git(inner, "config", key, value)
    return root, inner


@needs_git
@pytest.mark.parametrize("ignored", [False, True])
def test_an_untracked_source_in_a_submodule_counts_as_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    ignored: bool,
) -> None:
    root, inner = _submodule(tmp_path, {"Track.swift": "func v() {}\n"}, "Sources/vendor")
    if ignored:
        (inner / ".gitignore").write_text("Gen/\n", encoding="utf-8")
        git(inner, "add", ".gitignore")
        git(inner, "commit", "-q", "-m", "ignore")
        git(root, "add", "Sources/vendor")
        git(root, "commit", "-q", "-m", "bump")
    (inner / "Gen").mkdir()
    (inner / "Gen/New.swift").write_text(f"func n() {{ {LEGACY_CALL} }}\n", encoding="utf-8")
    monkeypatch.chdir(root)
    assert main(["check", "--new-from-rev", "HEAD"]) == 1
    out = capsys.readouterr().out
    assert "Sources/vendor/Gen/New.swift:1:12  error" in out
    assert "Gate: git does not track Sources/vendor/Gen/New.swift" in out


@needs_git
def test_a_link_to_an_untracked_generated_file_counts_as_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "linkgen"
    _real_repo(
        root,
        {
            ".tripl/check.yml": CONFIG,
            ".gitignore": "build/\n",
            "Sources/Main.swift": "func m() {}\n",
        },
    )
    # A tracked link, dangling at the base, to a file a build generates.
    (root / "Sources/Gen.swift").symlink_to("../build/Gen.swift")
    git(root, "add", "Sources/Gen.swift")
    git(root, "commit", "-q", "-m", "link")
    (root / "build").mkdir()
    (root / "build/Gen.swift").write_text(f"func g() {{ {LEGACY_CALL} }}\n", encoding="utf-8")
    monkeypatch.chdir(root)
    assert main(["check", "--new-from-rev", "HEAD"]) == 1
    out = capsys.readouterr().out
    assert "Sources/Gen.swift:1:12  error" in out
    assert "Gate: git does not track Sources/Gen.swift" in out


@needs_git
def test_a_link_into_a_dirty_or_bumped_submodule_counts_as_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, inner = _submodule(tmp_path, {"Track.swift": GOOD_CALL}, "vendor")
    (root / "Sources/Track.swift").symlink_to("../vendor/Track.swift")
    git(root, "add", "Sources/Track.swift")
    git(root, "commit", "-q", "-m", "link")
    monkeypatch.chdir(root)
    assert main(["check", "--new-from-rev", "HEAD"]) == 0
    capsys.readouterr()
    (inner / "Track.swift").write_text(f"func v() {{ {LEGACY_CALL} }}\n", encoding="utf-8")
    for state in ("dirty", "bumped"):
        if state == "bumped":
            git(inner, "commit", "-q", "-am", "bump")
        assert main(["check", "--new-from-rev", "HEAD"]) == 1
        out = capsys.readouterr().out
        assert "Sources/Track.swift:1:12  error" in out
        assert "cannot rebuild Sources/Track.swift" in out


@needs_git
def test_a_directory_replaced_by_a_link_keeps_its_deleted_files_at_the_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "dirlink"
    home = K_MAP.replace("map", "home")
    _real_repo(
        root,
        {
            ".tripl/check.yml": CONFIG,
            "Sources/Uses.swift": USES_K,
            # Two definitions at the base: K.cat is ambiguous, so the call is clean.
            "Sources/Consts/K.swift": home,
            "Sources/Other.swift": K_MAP,
            "Shared/Consts/K.swift": home,
        },
    )
    monkeypatch.chdir(root)
    assert main(["check", "--new-from-rev", "HEAD"]) == 0
    capsys.readouterr()
    shutil.rmtree(root / "Sources/Consts")
    (root / "Sources/Consts").symlink_to("../Shared/Consts")
    assert main(["check"]) == 1  # the walk does not enter the link: only "map" is left
    capsys.readouterr()
    assert main(["check", "--new-from-rev", "HEAD"]) == 1
    assert "Sources/Uses.swift:2:5  error  se  map:open:card" in capsys.readouterr().out


@needs_git
def test_the_base_size_cap_counts_the_bases_own_line_ends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "crlf"
    _real_repo(
        root,
        {
            ".tripl/check.yml": CONFIG,
            ".gitattributes": "* -text\n",
            "Sources/Main.swift": GOOD_CALL,
        },
    )
    line = "// " + "p" * 96 + "\r\n"  # 101 bytes, 100 with its CR read away
    call = f"func legacy() {{\r\n    {LEGACY_CALL}\r\n}}\r\n"
    base = (call + line * 19_850).encode()
    assert len(base) > 2_000_000 > len(base.replace(b"\r\n", b"\n"))
    (root / "Sources/Big.swift").write_bytes(base)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "big")
    monkeypatch.chdir(root)
    assert main(["check"]) == 0  # too large: never read
    capsys.readouterr()
    (root / "Sources/Big.swift").write_bytes((call + line * 19_700).encode())
    assert main(["check"]) == 1
    capsys.readouterr()
    assert _both_flags(root, capsys) == (1, 1)


# --- round five: the call as the scan matched it, and what today's scan reads ----------------
CALLS_CONFIG = """
project: demo
sources: ["Sources/**", "src/**"]
event_types:
  se:
    calls:
      - function: "log"
  legacy:
    calls: [{function: "logEvent", name_arg: 0, properties_arg: 1}]
  other:
    calls:
      - function: "send"
"""
SWIFT_SELF = (
    "class A {{\n    let analytics = Analytics()\n    func f() {{\n"
    '        {r}log(category: "map", action: "open", label: "x")\n    }}\n}}\n'
)
KOTLIN_THIS = (
    "class A(private val analytics: Analytics) {{\n    fun f() {{\n"
    '        {r}analytics.log(category = "map", action = "open", label = "x")\n    }}\n}}\n'
)
TS_RECEIVER = 'export function f({v}: Analytics) {{\n  {v}.logEvent("promo_opened");\n}}\n'
JAVA_FQN = (
    'class A {\n    void f() {\n        com.acme.Analytics.logEvent("promo_opened");\n    }\n}\n'
)
JAVA_IMPORTED = (
    "import com.acme.Analytics;\n\nclass A {\n    void f() {\n"
    '        Analytics.logEvent("promo_opened");\n    }\n}\n'
)
SWIFT_HOLE = 'func f({v}: String) {{\n    Analytics.shared.logEvent("promo_{name}_opened")\n}}\n'
TS_HOLE = "export function f({v}: string) {{\n  analytics.logEvent(`promo_{name}_opened`);\n}}\n"
KOTLIN_MAP = (
    'fun f() {\n    analytics.logEvent("promo_opened", '
    'mapOf("screen" to "map", "source" to "push"))\n}\n'
)
KOTLIN_MAP_TRAILING = (
    'fun f() {\n    analytics.logEvent(\n        "promo_opened",\n        mapOf(\n'
    '            "screen" to "map",\n            "source" to "push",\n        ),\n    )\n}\n'
)


@needs_git
@pytest.mark.parametrize(
    ("path", "base", "head", "code"),
    [
        pytest.param(
            "Sources/A.swift",
            SWIFT_SELF.format(r="self.analytics."),
            SWIFT_SELF.format(r="analytics."),
            0,
            id="swift-redundant-self-removed",
        ),
        pytest.param(
            "src/A.kt",
            KOTLIN_THIS.format(r="this."),
            KOTLIN_THIS.format(r=""),
            0,
            id="kotlin-this-dropped",
        ),
        pytest.param(
            "src/a.ts",
            TS_RECEIVER.format(v="tracker"),
            TS_RECEIVER.format(v="analytics"),
            0,
            id="ts-receiver-renamed",
        ),
        pytest.param("src/A.java", JAVA_FQN, JAVA_IMPORTED, 0, id="java-qualified-to-imported"),
        pytest.param(
            "Sources/A.swift",
            SWIFT_SELF.format(r="self.analytics."),
            SWIFT_SELF.format(r="self.analytics.").replace(".log(", ".send("),
            1,
            id="another-event-types-function",
        ),
        pytest.param(
            "Sources/A.swift",
            SWIFT_HOLE.format(v="sheetId", name="\\(sheetId)"),
            SWIFT_HOLE.format(v="id", name="\\(id)"),
            0,
            id="swift-interpolated-variable-renamed",
        ),
        pytest.param(
            "src/a.ts",
            TS_HOLE.format(v="sheetId", name="${sheetId}"),
            TS_HOLE.format(v="id", name="${id}"),
            0,
            id="ts-template-variable-renamed",
        ),
        pytest.param(
            "Sources/A.swift",
            SWIFT_HOLE.format(v="id", name="\\(id)"),
            SWIFT_HOLE.format(v="id", name="\\(id)").replace("_opened", "_closed"),
            1,
            id="interpolated-literal-text-changed",
        ),
        pytest.param(
            "Sources/A.swift",
            SWIFT_HOLE.format(v="id", name="\\(id)"),
            SWIFT_HOLE.format(v="id", name="\\(id)_\\(id)"),
            1,
            id="a-hole-added",
        ),
        pytest.param(
            "Sources/A.swift",
            SWIFT_HOLE.format(v="id", name="x_\\(id)"),
            SWIFT_HOLE.format(v="id", name="\\(id)_x"),
            1,
            id="a-hole-moved",
        ),
        pytest.param(
            "src/A.kt", KOTLIN_MAP, KOTLIN_MAP_TRAILING, 0, id="kotlin-map-trailing-comma"
        ),
    ],
)
def test_an_edit_that_calls_the_same_thing_keeps_it_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
    path: str,
    base: str,
    head: str,
    code: int,
) -> None:
    root = tmp_path / "calls"
    _real_repo(root, {".tripl/check.yml": CALLS_CONFIG, path: base})
    monkeypatch.chdir(root)
    assert main(["check"]) == 1  # a legacy error at the base
    capsys.readouterr()
    (root / path).write_text(head, encoding="utf-8")
    assert main(["check"]) == 1
    capsys.readouterr()
    assert _both_flags(root, capsys) == (code, code)


@needs_git
def test_a_call_in_a_file_the_scan_stops_reading_lends_no_base_to_a_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_git: None,
    validator: Any,
    configured_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "grown"
    _legacy_repo(root)
    (root / "Sources/Main.swift").write_text(GOOD_CALL, encoding="utf-8")
    git(root, "commit", "-q", "-am", "a call that passes")
    monkeypatch.chdir(root)
    legacy = root / "Sources/Legacy.swift"
    # The legacy file gains a generated table past the size cap: today's scan
    # skips it, so its call is no longer there to compare a copy with.
    legacy.write_text(legacy.read_text() + PADDING * 20_000, encoding="utf-8")
    assert main(["check"]) == 0
    capsys.readouterr()
    assert _both_flags(root, capsys) == (0, 0)
    (root / "Sources/New.swift").write_text(
        f"func added() {{\n    {LEGACY_CALL}\n}}\n", encoding="utf-8"
    )
    assert main(["check"]) == 1
    capsys.readouterr()
    assert _both_flags(root, capsys) == (1, 1)
