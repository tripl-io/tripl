"""The change a gate reads: a unified diff parsed, each file's base rebuilt, and git asked for it.

The parser and ``reverse_apply`` are tested on hand-written diffs in git's
format and on randomized edits; git through a recording ``Runner`` for every
way it refuses, and through a real temporary repository (skipped without git)
for what it actually prints. The real repositories set their own user and
read no global or system git config, so nothing here touches the machine's.
"""

from __future__ import annotations

import difflib
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tripl_cli.check import changes
from tripl_cli.check.changes import (
    FileChange,
    GitChange,
    from_git,
    parse_unified_diff,
    read_patch,
    reverse_apply,
)
from tripl_cli.errors import TriplConfigError
from tripl_cli.install.shell import Command, CommandResult

from .conftest import FakeRunner

HAS_GIT = shutil.which("git") is not None


def _one(text: str) -> FileChange:
    [change] = parse_unified_diff(text)
    return change


# --- the parser -------------------------------------------------------------------
def test_an_added_and_a_deleted_file() -> None:
    added, deleted = parse_unified_diff(
        "diff --git a/src/New.kt b/src/New.kt\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/src/New.kt\n"
        "@@ -0,0 +1,2 @@\n"
        "+one\n"
        "+two\n"
        "diff --git a/src/Old.kt b/src/Old.kt\n"
        "deleted file mode 100644\n"
        "index 2222222..0000000\n"
        "--- a/src/Old.kt\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-gone\n"
    )
    assert (added.old_path, added.new_path) == (None, "src/New.kt")
    assert added.hunks[0].new == ["one\n", "two\n"]
    assert (deleted.old_path, deleted.new_path) == ("src/Old.kt", None)
    assert reverse_apply("", deleted) == "gone\n"
    assert reverse_apply("one\ntwo\n", added) == ""


def test_an_empty_new_file_has_no_hunks_and_still_counts() -> None:
    change = _one("diff --git a/a.ts b/a.ts\nnew file mode 100644\nindex 0000000..e69de29\n")
    assert (change.old_path, change.new_path, change.hunks) == (None, "a.ts", ())


def test_a_rename_with_and_without_hunks() -> None:
    pure, edited = parse_unified_diff(
        "diff --git a/src/A.kt b/src/B.kt\n"
        "similarity index 100%\n"
        "rename from src/A.kt\n"
        "rename to src/B.kt\n"
        "diff --git a/src/C.kt b/src/D.kt\n"
        "similarity index 90%\n"
        "rename from src/C.kt\n"
        "rename to src/D.kt\n"
        "index 1..2 100644\n"
        "--- a/src/C.kt\n"
        "+++ b/src/D.kt\n"
        "@@ -2 +2 @@\n"
        "-old\n"
        "+new\n"
    )
    assert (pure.old_path, pure.new_path, pure.hunks) == ("src/A.kt", "src/B.kt", ())
    assert (edited.old_path, edited.new_path) == ("src/C.kt", "src/D.kt")
    assert reverse_apply("x\nnew\ny\n", edited) == "x\nold\ny\n"


def test_a_copy_is_a_new_file() -> None:
    change = _one("diff --git a/a.ts b/b.ts\nsimilarity index 100%\ncopy from a.ts\ncopy to b.ts\n")
    assert (change.old_path, change.new_path) == (None, "b.ts")


def test_zero_length_hunks_and_several_hunks_in_one_file() -> None:
    change = _one(
        "diff --git a/f b/f\n"
        "--- a/f\n"
        "+++ b/f\n"
        "@@ -1,0 +2 @@\n"
        "+inserted\n"
        "@@ -3 +3,0 @@\n"
        "-dropped\n"
        "@@ -5,2 +5,2 @@\n"
        "-e\n"
        "-f\n"
        "+E\n"
        "+F\n"
    )
    assert len(change.hunks) == 3
    current = "a\ninserted\nb\nd\nE\nF\n"
    assert reverse_apply(current, change) == "a\nb\ndropped\nd\ne\nf\n"
    assert change.hunks[0].added() == [2]
    assert change.hunks[1].added() == [3, 4]  # a deletion: the lines around it


def test_context_hunks_and_a_line_that_looks_like_a_header() -> None:
    change = _one(
        "diff --git a/f.ts b/f.ts\n"
        "--- a/f.ts\n"
        "+++ b/f.ts\n"
        "@@ -1,3 +1,3 @@\n"
        " keep\n"
        "--- not a header\n"
        "+++ not one either\n"
        " keep\n"
    )
    assert change.new_path == "f.ts"
    assert reverse_apply("keep\n++ not one either\nkeep\n", change) == (
        "keep\n-- not a header\nkeep\n"
    )


def test_no_newline_at_end_of_file_on_either_side() -> None:
    change = _one(
        "diff --git a/f b/f\n"
        "--- a/f\n"
        "+++ b/f\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
    )
    assert reverse_apply("new\n", change) == "old"
    back = _one(
        "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n"
        "\\ No newline at end of file\n"
    )
    assert reverse_apply("new", back) == "old\n"
    assert reverse_apply("new\n", back) is None


def test_quoted_and_spaced_paths_decode_as_the_walk_names_them() -> None:
    quoted, spaced = parse_unified_diff(
        'diff --git "a/src/caf\\303\\251.ts" "b/src/caf\\303\\251.ts"\n'
        "index 1..2 100644\n"
        '--- "a/src/caf\\303\\251.ts"\n'
        '+++ "b/src/caf\\303\\251.ts"\n'
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "diff --git a/my dir/x.ts b/my dir/x.ts\n"
        "index 1..2 100644\n"
        "--- a/my dir/x.ts\t\n"
        "+++ b/my dir/x.ts\t\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    assert quoted.new_path == "src/café.ts"
    assert spaced.old_path == spaced.new_path == "my dir/x.ts"


def test_a_mode_change_names_its_path_from_the_header() -> None:
    change = _one("diff --git a/my dir/x.sh b/my dir/x.sh\nold mode 100644\nnew mode 100755\n")
    assert change.old_path == change.new_path == "my dir/x.sh"


def test_binary_files_and_submodules() -> None:
    binary, submodule, logged = parse_unified_diff(
        "diff --git a/logo.png b/logo.png\n"
        "index 1..2 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
        "diff --git a/vendor/lib b/vendor/lib\n"
        "index 1111111..2222222 160000\n"
        "--- a/vendor/lib\n"
        "+++ b/vendor/lib\n"
        "@@ -1 +1 @@\n"
        "-Subproject commit 1111111\n"
        "+Subproject commit 2222222\n"
        "Submodule other/sdk 1111111..2222222:\n"
        "  > Bump the SDK\n"
    )
    assert binary.binary and binary.new_path == "logo.png"
    assert reverse_apply("anything", binary) is None
    # Kept, not dropped: the scan reads a submodule's files.
    assert (submodule.submodule, submodule.new_path) == (True, "vendor/lib")
    assert (logged.submodule, logged.new_path) == (True, "other/sdk")


def test_git_prefix_pairs_mnemonic_and_none() -> None:
    mnemonic, index, bare = parse_unified_diff(
        "diff --git c/src/a.ts w/src/a.ts\n"
        "--- c/src/a.ts\n"
        "+++ w/src/a.ts\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "diff --git i/src/new.ts w/src/new.ts\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ w/src/new.ts\n"
        "@@ -0,0 +1 @@\n"
        "+x\n"
        "diff --git src/b.ts src/b.ts\n"
        "--- src/b.ts\n"
        "+++ src/b.ts\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    assert (mnemonic.old_path, mnemonic.new_path) == ("src/a.ts", "src/a.ts")
    assert (index.old_path, index.new_path) == (None, "src/new.ts")
    assert (bare.old_path, bare.new_path) == ("src/b.ts", "src/b.ts")


def test_reverse_apply_reads_crlf_and_lf_alike() -> None:
    """git diffs the clean (LF) text of a file autocrlf or eol=crlf checks out as CRLF."""
    change = _one("diff --git a/w.ts b/w.ts\n--- a/w.ts\n+++ b/w.ts\n@@ -2 +2 @@\n-old\n+new\n")
    assert reverse_apply("a\r\nnew\r\nz\r\n", change) == "a\r\nold\nz\r\n"
    # A missing last newline still counts.
    last = _one("diff --git a/w.ts b/w.ts\n--- a/w.ts\n+++ b/w.ts\n@@ -1 +1 @@\n-old\n+new\n")
    assert reverse_apply("new", last) is None


def test_crlf_content_keeps_its_carriage_returns() -> None:
    change = _one(
        "diff --git a/w.ts b/w.ts\r\n--- a/w.ts\r\n+++ b/w.ts\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n"
    )
    assert change.new_path == "w.ts"
    assert reverse_apply("new\r\nnext\r\n", change) == "old\r\nnext\r\n"


def test_a_crlf_patch_file_with_no_newline_at_the_end() -> None:
    patch = (
        "diff --git a/t.swift b/t.swift\n--- a/t.swift\n+++ b/t.swift\n@@ -2 +2 @@\n"
        "-let z = 1\n\\ No newline at end of file\n+let z = 2\n\\ No newline at end of file\n"
    ).replace("\n", "\r\n")
    change = _one(patch)
    assert reverse_apply("let a = 0\nlet z = 2", change) == "let a = 0\nlet z = 1"


@pytest.mark.parametrize(
    "patch",
    [
        "--- a/f.ts\n+++ b/f.ts\n@@ -1 +1 @@\n-a\n+b\n",
        "--- f.ts\t2026-10-24 13:42:17 +0200\n+++ f.ts\t2026-10-24 13:42:18 +0200\n"
        "@@ -1 +1 @@\n-a\n+b\n",
        # Subversion's own form, a deleted file's new side "(nonexistent)".
        "Index: gone.ts\n===\n--- gone.ts\t(revision 12)\n+++ gone.ts\t(nonexistent)\n"
        "@@ -1 +0,0 @@\n-y\n",
    ],
)
def test_a_diff_not_in_gits_format_is_refused(patch: str) -> None:
    with pytest.raises(TriplConfigError, match="git's format only"):
        parse_unified_diff(patch)


@pytest.mark.skipif(shutil.which("diff") is None, reason="diff is not installed")
def test_real_diff_ruN_output_is_refused(tmp_path: Path) -> None:
    for side, text in (("old", "a\n"), ("new", "b\n")):
        (tmp_path / side).mkdir()
        (tmp_path / side / "f.ts").write_text(text, encoding="utf-8")
    done = subprocess.run(
        ["diff", "-ruN", "old", "new"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    with pytest.raises(TriplConfigError, match="git diff --no-index"):
        parse_unified_diff(done.stdout)


def test_a_format_patch_mail_between_the_blocks_is_skipped() -> None:
    first, second = parse_unified_diff(
        "From 1111 Mon Sep 17 00:00:00 2001\nSubject: [PATCH 1/2] one\n\n---\n"
        " f.ts | 2 +-\n\n"
        "diff --git a/f.ts b/f.ts\n--- a/f.ts\n+++ b/f.ts\n@@ -1 +1 @@\n-a\n+b\n"
        "-- \n2.47.3\n\n"
        "From 2222 Mon Sep 17 00:00:00 2001\nSubject: [PATCH 2/2] two\n\n"
        "diff --git a/f.ts b/f.ts\n--- a/f.ts\n+++ b/f.ts\n@@ -1 +1 @@\n-b\n+c\n"
    )
    assert first.new_path == second.new_path == "f.ts"


def test_text_with_no_diff_header_holds_no_change() -> None:
    assert parse_unified_diff("") == ()
    assert parse_unified_diff("just some notes\n+ and a plus\n") == ()


def test_reverse_apply_refuses_a_diff_of_another_tree() -> None:
    change = _one("diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n")
    assert reverse_apply("something else\n", change) is None
    assert reverse_apply("", change) is None


@pytest.mark.parametrize("context", [0, 3])
def test_reverse_apply_round_trips_randomized_edits(context: int) -> None:
    generator = random.Random(468 + context)
    for _ in range(300):
        before = [f"line {generator.randrange(8)}\n" for _ in range(generator.randrange(0, 30))]
        after = list(before)
        for _ in range(generator.randrange(1, 6)):
            where = generator.randrange(len(after) + 1)
            action = generator.choice(("insert", "delete", "replace"))
            if action == "insert" or not after:
                after.insert(where, f"new {generator.randrange(100)}\n")
            elif action == "delete":
                del after[min(where, len(after) - 1)]
            else:
                after[min(where, len(after) - 1)] = f"changed {generator.randrange(100)}\n"
        body = "".join(difflib.unified_diff(before, after, "a/f", "b/f", n=context))
        diff = f"diff --git a/f b/f\n{body}" if body else ""
        found = parse_unified_diff(diff)
        if before == after:
            assert found == ()
            continue
        [change] = found
        assert reverse_apply("".join(after), change) == "".join(before)


# --- reading a patch ------------------------------------------------------------------
def test_read_patch_from_a_file_and_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "change.diff"
    path.write_bytes(b"--- a/f\n+++ b/f\n@@ -1 +1 @@\n-\xff\n+x\n")
    assert read_patch(str(path)).endswith("-�\n+x\n")
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert read_patch("-") == "from stdin"
    with pytest.raises(TriplConfigError, match="cannot read"):
        read_patch(str(tmp_path / "missing.diff"))


# --- git, through a recording runner --------------------------------------------------
class GitRunner(FakeRunner):
    """``FakeRunner`` keyed by git subcommand: toplevel, verify, diff, others, stage."""

    @staticmethod
    def key(command: Command) -> str:
        argv = command.argv
        if "--show-toplevel" in argv:
            return "toplevel"
        if "--verify" in argv:
            return "verify"
        if "--stage" in argv:
            return "stage"
        if "ls-files" in argv:
            return "others"
        return "diff" if "diff" in argv else " ".join(argv)


@pytest.fixture
def git_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "which", lambda name: f"/usr/bin/{name}")


def test_git_runs_with_a_fixed_argv_and_its_own_settings(git_found: None) -> None:
    runner = GitRunner()
    runner.stdout = {
        "toplevel": "/repo\n",
        "verify": "abc123\n",
        "diff": "the diff",
        "others": 'build/\nSources/Gen.swift\n"Sources/caf\\303\\251.swift"\n',
    }
    assert from_git("origin/main", Path("/repo/app"), runner) == GitChange(
        Path("/repo"),
        "abc123",
        "the diff",
        ("build/", "Sources/Gen.swift", "Sources/café.swift"),
    )
    toplevel, verify, diff, others, stage = runner.calls
    assert stage.argv[-2:] == ("ls-files", "--stage")
    assert stage.cwd == Path("/repo")
    # Every file git does not track, ignored ones too, a directory of them once.
    assert others.argv[-5:] == (
        "ls-files",
        "--others",
        "--directory",
        "--no-empty-directory",
        "--full-name",
    )
    assert "--exclude-standard" not in others.argv
    assert others.cwd == Path("/repo")
    assert toplevel.argv == ("git", "rev-parse", "--show-toplevel")
    assert toplevel.cwd == Path("/repo/app")
    assert verify.argv == ("git", "rev-parse", "--verify", "--quiet", "origin/main^{commit}")
    assert diff.cwd == Path("/repo")
    assert diff.argv[-2:] == ("abc123", "--")
    for flag in (
        "-U0",
        "-M",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--ignore-submodules=untracked",
        "--submodule=short",
    ):
        assert flag in diff.argv
    for command in runner.calls:
        assert command.env["GIT_TERMINAL_PROMPT"] == "0"
        assert command.env["GIT_NO_LAZY_FETCH"] == "1"
        assert command.env["LC_ALL"] == "C"
        assert command.timeout == changes.GIT_TIMEOUT
        assert command.encoding == "utf-8"
    assert all(runner.captured)


def test_untracked_files_of_a_checked_out_submodule_are_listed_under_its_path(
    tmp_path: Path, git_found: None
) -> None:
    # The top level lists a submodule as a gitlink and stops there: each one
    # the check root reaches, and checked out, is asked by its own git.
    (tmp_path / "Vendor" / "Kit" / "Deep" / ".git").mkdir(parents=True)
    (tmp_path / "Vendor" / "Kit" / ".git").write_text("gitdir: ../../.git/modules/Kit\n")
    (tmp_path / "Other" / ".git").mkdir(parents=True)
    (tmp_path / "Vendor" / "Absent").mkdir(parents=True)
    listings = {
        (tmp_path / "Vendor", "toplevel"): f"{tmp_path}\n",
        (tmp_path, "verify"): "abc123\n",
        (tmp_path, "others"): "build/\n",
        (tmp_path, "stage"): (
            "100644 1111111111111111111111111111111111111111 0\tSources/A.swift\n"
            "160000 2222222222222222222222222222222222222222 0\tVendor/Kit\n"
            "160000 3333333333333333333333333333333333333333 0\tVendor/Absent\n"
            "160000 4444444444444444444444444444444444444444 0\tOther\n"
        ),
        (tmp_path / "Vendor" / "Kit", "others"): "Gen.swift\n",
        (tmp_path / "Vendor" / "Kit", "stage"): (
            "160000 5555555555555555555555555555555555555555 0\tDeep\n"
        ),
        (tmp_path / "Vendor" / "Kit" / "Deep", "others"): "out/\n",
    }

    class Nested(GitRunner):
        def __call__(self, command: Command, capture: bool) -> CommandResult:
            self.calls.append(command)
            self.captured.append(capture)
            out = listings.get((command.cwd, self.key(command)), "")
            return CommandResult(command.argv, 0, out, "")

    runner = Nested()
    git = from_git("HEAD", tmp_path / "Vendor", runner)
    assert git.untracked == ("build/", "Vendor/Kit/Gen.swift", "Vendor/Kit/Deep/out/")
    # Other is outside the check root, Absent is not checked out: neither is asked.
    asked = {command.cwd for command in runner.calls}
    assert tmp_path / "Other" not in asked
    assert tmp_path / "Vendor" / "Absent" not in asked


def test_a_failing_git_in_a_submodule_is_exit_2(tmp_path: Path, git_found: None) -> None:
    (tmp_path / "Kit" / ".git").mkdir(parents=True)

    class Failing(GitRunner):
        def __call__(self, command: Command, capture: bool) -> CommandResult:
            self.calls.append(command)
            if command.cwd == tmp_path / "Kit":
                return CommandResult(command.argv, 128, "", "fatal: not a git repository")
            if self.key(command) == "toplevel":
                return CommandResult(command.argv, 0, f"{tmp_path}\n", "")
            if self.key(command) == "verify":
                return CommandResult(command.argv, 0, "abc123\n", "")
            if self.key(command) == "stage":
                return CommandResult(command.argv, 0, "160000 " + "6" * 40 + " 0\tKit\n", "")
            return CommandResult(command.argv, 0, "", "")

    with pytest.raises(TriplConfigError, match="git ls-files failed in .*Kit"):
        from_git("HEAD", tmp_path, Failing())


def test_git_missing_names_the_ways_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(changes, "which", lambda name: None)
    runner = GitRunner()
    with pytest.raises(TriplConfigError, match="--new-from-patch") as caught:
        from_git("HEAD^1", Path("/repo"), runner)
    assert "tripl-cli image" in str(caught.value)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("ref", "hint"),
    [
        ("main", "git fetch --no-tags --depth=1 origin main, then --new-from-rev origin/main"),
        (
            "origin/main",
            "git fetch --no-tags --depth=1 origin main, then --new-from-rev origin/main",
        ),
        ("HEAD^1", "git fetch --no-tags --deepen=1 origin"),
        # Bitbucket's destination commit: a 12-digit abbreviation no remote answers.
        ("2505a282b89e", "an abbreviated SHA cannot be fetched: pass the full SHA"),
    ],
)
def test_the_hint_fits_what_kind_of_revision_is_missing(
    git_found: None, ref: str, hint: str
) -> None:
    runner = GitRunner(verify=1)
    runner.stdout = {"toplevel": "/repo\n"}
    with pytest.raises(TriplConfigError, match=re.escape(hint)):
        from_git(ref, Path("/repo"), runner)


def test_an_unknown_revision_gives_the_fetch_hint(git_found: None) -> None:
    runner = GitRunner(verify=1)
    runner.stdout = {"toplevel": "/repo\n"}
    sha = "1a2b3c4d" * 5
    with pytest.raises(TriplConfigError) as caught:
        from_git(sha, Path("/repo"), runner)
    assert str(caught.value) == (
        f"--new-from-rev: the commit {sha} is not in this clone; fetch it: "
        f"git fetch --no-tags --depth=1 origin {sha}"
    )


def test_not_a_repository(git_found: None) -> None:
    runner = GitRunner(toplevel=128)
    runner.stderr = {"toplevel": "fatal: not a git repository (or any of the parent directories)"}
    with pytest.raises(TriplConfigError, match="is not in one .fatal: not a git repository"):
        from_git("HEAD", Path("/tmp/x"), runner)


def test_dubious_ownership_is_reported_with_the_safe_directory_command(git_found: None) -> None:
    runner = GitRunner(toplevel=128)
    runner.stderr = {
        "toplevel": "fatal: detected dubious ownership in repository at '/src'\n"
        "To add an exception for this directory, call:\n\n"
        "\tgit config --global --add safe.directory /src\n"
    }
    with pytest.raises(TriplConfigError) as caught:
        from_git("HEAD", Path("/src"), runner)
    assert 'git config --global --add safe.directory "/src"' in str(caught.value)
    # Reported, never worked around: no git call names safe.directory.
    assert all("safe.directory" not in " ".join(command.argv) for command in runner.calls)


def test_a_git_timeout_is_exit_2(git_found: None) -> None:
    def runner(command: Command, capture: bool) -> CommandResult:
        return CommandResult(command.argv, -1, timed_out=True)

    with pytest.raises(TriplConfigError, match="took longer than 60 s"):
        from_git("HEAD", Path("/repo"), runner)


# --- git, for real --------------------------------------------------------------------------
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git is not installed")


@pytest.fixture
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """No global or system config for any git this test runs, the CLI's included."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(cwd: Path, *argv: str) -> str:
    done = subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def show(root: Path, rev: str, path: str) -> str:
    """A file at ``rev`` exactly, CRLF and missing last newline included."""
    done = subprocess.run(
        ["git", "show", f"{rev}:{path}"], cwd=root, check=True, capture_output=True
    )
    return done.stdout.decode("utf-8")


def make_repo(root: Path, files: dict[str, str]) -> str:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "tripl test")
    git(root, "config", "user.email", "test@tripl.invalid")
    git(root, "config", "commit.gpgsign", "false")
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return git(root, "rev-parse", "HEAD")


@needs_git
def test_a_real_diff_of_a_commit_and_worktree_edits_round_trips(
    tmp_path: Path, isolated_git: None
) -> None:
    root = tmp_path / "repo"
    base = make_repo(
        root,
        {
            "src/a.ts": "one\ntwo\nthree\n",
            "src/b.ts": "".join(f"line {n}\n" for n in range(20)),
            "src/gone.ts": "bye\n",
            "src/crlf.ts": "a\r\nb\r\n",
        },
    )
    (root / "src/a.ts").write_text("one\n2\nthree\nfour", encoding="utf-8")
    git(root, "mv", "src/b.ts", "src/renamed.ts")
    renamed = (root / "src/renamed.ts").read_text(encoding="utf-8").replace("line 5\n", "")
    (root / "src/renamed.ts").write_text(renamed, encoding="utf-8")
    (root / "src/gone.ts").unlink()
    (root / "src/crlf.ts").write_bytes(b"a\r\nB\r\n")
    found = from_git("HEAD", root / "src")
    toplevel, sha, text = found.root, found.sha, found.diff
    assert toplevel.resolve() == root.resolve()
    assert sha == base
    found = {change.new_path or change.old_path: change for change in parse_unified_diff(text)}
    assert found["src/renamed.ts"].old_path == "src/b.ts"  # -M saw the rename
    assert found["src/gone.ts"].new_path is None
    for path, change in found.items():
        if change.new_path is None:
            assert reverse_apply("", change) == show(root, base, path)
            continue
        assert change.old_path is not None
        current = (root / path).read_bytes().decode()
        assert reverse_apply(current, change) == show(root, base, change.old_path)


@needs_git
def test_a_shallow_clone_without_the_base_names_the_fetch(
    tmp_path: Path, isolated_git: None
) -> None:
    origin = tmp_path / "origin"
    base = make_repo(origin, {"a.ts": "one\n"})
    (origin / "a.ts").write_text("two\n", encoding="utf-8")
    git(origin, "commit", "-q", "-am", "next")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth=1", f"file://{origin}", str(clone))
    with pytest.raises(TriplConfigError, match=f"git fetch --no-tags --depth=1 origin {base}"):
        from_git(base, clone)


@needs_git
@pytest.mark.parametrize("setting", ["autocrlf", "eol"])
def test_a_crlf_checkout_fits_the_lf_diff_git_prints(
    tmp_path: Path, isolated_git: None, setting: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    if setting == "autocrlf":
        git(root, "config", "core.autocrlf", "true")
    else:
        (root / ".gitattributes").write_text("*.ts text eol=crlf\n", encoding="utf-8")
    base = make_repo(root, {"src/a.ts": "one\r\ntwo\r\nthree\r\n"})
    (root / "src/a.ts").write_bytes(b"zero\r\none\r\ntwo\r\nthree\r\n")
    text = from_git("HEAD", root).diff
    [change] = [c for c in parse_unified_diff(text) if c.new_path == "src/a.ts"]
    assert "\r" not in "".join(change.hunks[0].lines)  # git printed the clean LF text
    rebuilt = reverse_apply((root / "src/a.ts").read_bytes().decode(), change)
    assert rebuilt is not None
    assert rebuilt.replace("\r\n", "\n") == show(root, base, "src/a.ts").replace("\r\n", "\n")


@needs_git
def test_git_diff_no_index_pairs_map_onto_the_checkout(tmp_path: Path, isolated_git: None) -> None:
    for relative, text in (("base/src/x.ts", "a\n"), ("base/src/gone.ts", "g\n")):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text, encoding="utf-8")
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src/x.ts").write_text("b\n", encoding="utf-8")
    (work / "src/new.ts").write_text("n\n", encoding="utf-8")
    done = subprocess.run(
        ["git", "diff", "--no-index", "../base", "."],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1
    found = {(c.old_path, c.new_path) for c in parse_unified_diff(done.stdout)}
    assert found == {("src/x.ts", "./src/x.ts"), (None, "./src/new.ts"), ("src/gone.ts", None)}
