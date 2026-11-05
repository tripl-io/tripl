"""What a change did to the tree: a unified diff, parsed, and each file's base text rebuilt from it.

``tripl check --new-from-rev REF`` asks git for the diff between ``REF`` and the
working tree; ``--new-from-patch FILE`` reads one (``-`` for stdin), and needs
no git at all. Either way the gate (``gate.py``) gets the same thing: each
changed file with its hunks, from which ``reverse_apply`` rebuilds the file as
it was at the base, so the base tree can be scanned without checking it out.

Git runs through the ``Runner`` seam (``install/shell.py``), with a fixed argv,
never a shell string, and settings of its own that no repository or user
config can change: no prompt, no lazy fetch of a partial clone's objects, no
optional locks, no external diff or text conversion, ``a/``/``b/`` prefixes and
quoted paths. Every way it can fail is a ``TriplConfigError`` (exit 2), and
all of them happen before anything is sent.

Text is decoded as UTF-8 with replacement, the way the scan reads files
(``scan._read``), and is never newline-translated here. ``reverse_apply``
compares lines with CRLF and LF read alike, because git diffs the clean
(LF) content of a file that ``core.autocrlf`` or ``eol=crlf`` checks out with
CRLF; a missing newline at the end of the file still counts.
"""

from __future__ import annotations

import os
import posixpath
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from shutil import which

from tripl_cli.errors import TriplConfigError
from tripl_cli.install.shell import Command, CommandResult, Runner, subprocess_runner

GIT = "git"
# Each git call; a diff of a huge checkout on a slow disk is the long one.
GIT_TIMEOUT = 60.0
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
}
# What a repository's or a user's config could change about the diff, pinned.
GIT_CONFIG = (
    "-c",
    "core.quotePath=true",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
)
DIFF_FLAGS = (
    "diff",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-relative",
    # Every submodule change, a tracked edit inside one included, in one
    # format: the scan reads a submodule's files, so the gate must see it
    # move. An untracked file in one is no more part of the change than an
    # untracked file anywhere else.
    "--ignore-submodules=untracked",
    "--submodule=short",
    "-U0",
    "-M",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
# Every file git does not track, ignored ones included (no --exclude-standard):
# a directory of them once, paths quoted the way the diff quotes them.
LS_OTHERS = ("ls-files", "--others", "--directory", "--no-empty-directory", "--full-name")
# Every tracked path with its mode: a submodule is a gitlink, mode 160000.
LS_STAGE = ("ls-files", "--stage")
GITLINK = "160000"
DEV_NULL = "/dev/null"
_SHA = re.compile(r"[0-9a-fA-F]{7,64}")
_MODE_LINE = re.compile(r"(?:new file mode|deleted file mode|old mode|new mode|index) ")
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
STDIN = "-"

_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# git's prefix pairs: a/ b/, and with diff.mnemonicPrefix the (c)ommit, the
# (i)ndex, the (w)ork tree, an (o)bject, or the two sides of --no-index.
_OLD_PREFIXES = ("a/", "c/", "i/", "o/", "1/")
_NEW_PREFIXES = ("b/", "w/", "i/", "2/")
# git diff --submodule=log/diff: "Submodule PATH abc123..def456:", and the
# lines for a submodule with uncommitted or untracked changes.
_SUBMODULE_LINE = re.compile(
    r"Submodule (.+?) (?:[0-9a-f]{7,}\.\.\.?[0-9a-f]{7,}|contains (?:modified|untracked) content)"
)
_OCTAL = re.compile(r"[0-7]{3}")
_ESCAPES = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}


@dataclass(frozen=True)
class Hunk:
    """One ``@@`` hunk. Each line keeps its prefix (`` ``, ``-``, ``+``) and its newline.

    A line the diff marks ``\\ No newline at end of file`` has none.
    """

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]

    @property
    def old(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " -"]

    @property
    def new(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " +"]

    def added(self) -> list[int]:
        """The new side's lines this hunk adds; the lines around it for one that only deletes."""
        if self.new_count == 0:
            return [self.new_start, self.new_start + 1]
        numbers: list[int] = []
        line = self.new_start
        for entry in self.lines:
            if entry[0] == "+":
                numbers.append(line)
            if entry[0] in " +":
                line += 1
        return numbers


@dataclass(frozen=True)
class FileChange:
    """One file in a diff. ``old_path`` is None for an added file, ``new_path`` for a deleted one.

    Paths are as the diff names them, relative to the repository root, without
    their prefixes. ``binary`` is a change the diff gives no lines for;
    ``submodule`` one to a submodule, whose files the diff does not show.
    """

    old_path: str | None
    new_path: str | None
    hunks: tuple[Hunk, ...] = ()
    binary: bool = False
    submodule: bool = False
    # Named by ``rename``/``copy`` lines, so two different paths are meant.
    renamed: bool = False
    # A symlink on either side (mode 120000): its "text" is where it points.
    symlink: bool = False


# --- parsing ------------------------------------------------------------------------
def split_lines(text: str) -> list[str]:
    """``text`` cut after each ``\\n`` only (git's rule, and ``LineIndex``'s), ends kept."""
    lines = text.split("\n")
    out = [line + "\n" for line in lines[:-1]]
    if lines[-1]:
        out.append(lines[-1])
    return out


GIT_FORMAT_ONLY = (
    "--new-from-patch reads diffs in git's format only, a 'diff --git' header on "
    "every file: write the change with git diff, git diff --no-index BASE_DIR "
    "CHECKOUT_DIR, hg diff --git, svn diff --git or git format-patch."
)


def parse_unified_diff(text: str) -> tuple[FileChange, ...]:
    """Every file ``text`` changes, in diff order. Text that holds no diff header gives ``()``.

    Reads git's format only: ``diff --git`` blocks with their extended headers
    (renames, copies, binary files, mode-only changes), any of git's prefix
    pairs (``a/``/``b/``, the mnemonic ``c/``/``w/``, ``i/``, ``o/``,
    ``1/``/``2/``, or none), hunks with or without context and
    ``\\ No newline at end of file``; whatever comes between the blocks (a
    ``git format-patch`` mail, its commit message) is skipped. A submodule
    change is kept, as ``submodule``: the scan reads the files in it. A plain
    ``---``/``+++`` header outside a ``diff --git`` block (``diff -u``, a
    Subversion diff) raises ``TriplConfigError``: its paths and its missing
    sides are not reliably readable, and the gate must not guess.
    ``git diff --no-index`` pairs (each side's path under its own directory)
    are read as one file, by the paths the two sides share.
    """
    lines = split_lines(text)
    changes: list[FileChange] = []
    index = 0
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        if line.startswith("diff --git "):
            change, index = _git_block(lines, index)
            changes.append(change)
            continue
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            raise TriplConfigError(GIT_FORMAT_ONLY)
        named = _SUBMODULE_LINE.match(line)
        if named is not None:
            # git diff --submodule=log or =diff: a line of its own, no header.
            path = _path(named.group(1))
            changes.append(FileChange(path, path, submodule=True))
        index += 1
    return _no_index(tuple(changes))


def _no_index(changes: tuple[FileChange, ...]) -> tuple[FileChange, ...]:
    """``git diff --no-index BASE_DIR CHECKOUT_DIR``'s paths, each side moved onto the checkout's.

    Such a diff names a changed file twice, ``BASE_DIR/p`` and
    ``CHECKOUT_DIR/p``, with no rename line; a git repository's own diff
    never does. The two directories are what such pairs do not share; a
    deleted file's base path moves with them. Pairs that disagree are left as
    they are, so they read as renames from outside the tree: fail closed.
    """
    pairs = {
        _split_tails(change.old_path, change.new_path)
        for change in changes
        if change.old_path is not None
        and change.new_path is not None
        and not change.renamed
        and not change.submodule
        and posixpath.normpath(change.old_path) != posixpath.normpath(change.new_path)
    }
    if len(pairs) != 1:
        return changes
    [(base_dir, checkout_dir)] = pairs

    def moved(path: str | None) -> str | None:
        if path is None:
            return None
        normal = posixpath.normpath(path)
        if base_dir in ("", "."):
            return posixpath.normpath(posixpath.join(checkout_dir, normal))
        if normal == base_dir or normal.startswith(f"{base_dir}/"):
            rest = normal[len(base_dir) :].lstrip("/")
            return posixpath.normpath(posixpath.join(checkout_dir, rest))
        return path

    return tuple(
        change if change.renamed else replace(change, old_path=moved(change.old_path))
        for change in changes
    )


def _split_tails(old: str, new: str) -> tuple[str, str]:
    """The directories two paths of one file sit in: what is left once their shared end is cut."""
    old_parts = posixpath.normpath(old).split("/")
    new_parts = posixpath.normpath(new).split("/")
    while old_parts and new_parts and old_parts[-1] == new_parts[-1]:
        old_parts.pop()
        new_parts.pop()
    return "/".join(old_parts) or ".", "/".join(new_parts) or "."


def _git_block(lines: list[str], index: int) -> tuple[FileChange, int]:
    header = lines[index].rstrip("\r\n")
    old_raw, new_raw = _git_header_paths(header[len("diff --git ") :])
    old_prefix, new_prefix = _prefix_pair(old_raw, new_raw)
    old_path: str | None = old_raw.removeprefix(old_prefix)
    new_path: str | None = new_raw.removeprefix(new_prefix)
    added = deleted = binary = submodule = renamed = symlink = False
    index += 1
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        if line.startswith(("diff --git ", "@@ ")) or line.startswith("--- "):
            break
        symlink = symlink or (_MODE_LINE.match(line) is not None and line.endswith(" 120000"))
        if line.startswith("new file mode"):
            added = True
            submodule = submodule or line.endswith(" 160000")
        elif line.startswith("deleted file mode"):
            deleted = True
            submodule = submodule or line.endswith(" 160000")
        elif line.startswith("copy to "):
            # A copy is a new file, whatever it was copied from.
            added = renamed = True
            new_path = _path(line[len("copy to ") :])
        elif line.startswith("rename from "):
            renamed = True
            old_path = _path(line[len("rename from ") :])
        elif line.startswith("rename to "):
            renamed = True
            new_path = _path(line[len("rename to ") :])
        elif line.startswith(("Binary files ", "GIT binary patch")):
            binary = True
        elif line.startswith("index ") and line.endswith(" 160000"):
            submodule = True
        index += 1
    if index < len(lines) and lines[index].startswith("--- "):
        old_side, _ = _side(lines[index][4:])
        old_path = old_side.removeprefix(old_prefix) if old_side is not None else old_path
        if index + 1 < len(lines):
            new_side, _ = _side(lines[index + 1][4:])
            new_path = new_side.removeprefix(new_prefix) if new_side is not None else new_path
        index += 2
    hunks, index = _hunks(lines, index)
    if added:
        old_path = None
    if deleted:
        new_path = None
    submodule = submodule or any(_is_submodule(hunk) for hunk in hunks)
    change = FileChange(old_path, new_path, tuple(hunks), binary, submodule, renamed, symlink)
    return change, index


def _prefix_pair(old: str, new: str) -> tuple[str, str]:
    """The two prefixes of a ``diff --git`` header's paths, or none (``--no-prefix``)."""
    old_prefix, new_prefix = old[:2], new[:2]
    if (
        old_prefix in _OLD_PREFIXES
        and new_prefix in _NEW_PREFIXES
        and old_prefix != new_prefix
        and old[2:] != ""
    ):
        return old_prefix, new_prefix
    return "", ""


def _hunks(lines: list[str], index: int) -> tuple[list[Hunk], int]:
    """The hunks from ``index`` on, each read by its counts, so a ``-- x`` line is content."""
    hunks: list[Hunk] = []
    while index < len(lines):
        match = _HUNK.match(lines[index])
        if match is None:
            break
        old_start, new_start = int(match.group(1)), int(match.group(3))
        old_count = 1 if match.group(2) is None else int(match.group(2))
        new_count = 1 if match.group(4) is None else int(match.group(4))
        index += 1
        body: list[str] = []
        old_left, new_left = old_count, new_count
        while index < len(lines) and (old_left > 0 or new_left > 0):
            line = lines[index]
            kind = line[0]
            if kind == "\\":
                _no_newline(body)
            elif kind in " -+":
                if kind in " -":
                    old_left -= 1
                if kind in " +":
                    new_left -= 1
                body.append(line)
            elif line in ("\n", "\r\n"):
                # A context line that lost its leading space on the way (an
                # editor, a mail client): an empty line on both sides.
                old_left -= 1
                new_left -= 1
                body.append(" " + line)
            else:
                break
            index += 1
        if index < len(lines) and lines[index].startswith("\\"):
            _no_newline(body)
            index += 1
        hunks.append(Hunk(old_start, old_count, new_start, new_count, tuple(body)))
    return hunks, index


def _no_newline(body: list[str]) -> None:
    """The last line read has no newline: drop it, and a CR a CRLF patch file put before it."""
    if body and body[-1].endswith("\n"):
        body[-1] = body[-1][:-2] if body[-1].endswith("\r\n") else body[-1][:-1]


def _is_submodule(hunk: Hunk) -> bool:
    return bool(hunk.lines) and all(
        line[1:].startswith("Subproject commit ") for line in hunk.lines
    )


def _side(raw: str) -> tuple[str | None, str]:
    """A ``---``/``+++`` line: ``(path, what follows a tab)``, the path None for ``/dev/null``.

    git ends the line with a tab when the path holds a space; that ends the
    path, after the closing quote of a quoted one.
    """
    raw = raw.rstrip("\r\n")
    if raw.startswith('"'):
        end = _quoted_end(raw)
        name, stamp = raw[: end + 1], raw[end + 1 :]
    else:
        name, _, stamp = raw.partition("\t")
    path = _path(name)
    return (None if path == DEV_NULL else path), stamp.strip()


def _git_header_paths(rest: str) -> tuple[str, str]:
    """The two paths of a ``diff --git`` line, prefixes kept, for a change with no other names.

    Unquoted, the split is ambiguous when a path holds a space, so it is taken
    where the two halves agree (a mode change or a binary file names one path
    twice), else before the new side's prefix.
    """
    if rest.startswith('"'):
        end = _quoted_end(rest)
        return _path(rest[: end + 1]), _path(rest[end + 1 :])
    half = (len(rest) - 1) // 2
    old, new = rest[:half], rest[half + 1 :]
    if rest[half : half + 1] == " " and (old == new or old[2:] == new[2:]):
        return old, new
    for prefix in _NEW_PREFIXES:
        before, found, after = rest.partition(" " + prefix)
        if found:
            return before, _path(prefix + after)
    return old, new


def _quoted_end(text: str) -> int:
    index = 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index
        index += 1
    return len(text) - 1


def _path(raw: str) -> str:
    """A diff path, C-unquoted when git quoted it, decoded the way ``os.walk`` names files."""
    raw = raw.strip()
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw
    body = raw[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            following = body[index + 1]
            octal = _OCTAL.match(body, index + 1)
            if octal is not None:
                out.append(int(octal.group(0), 8) & 0xFF)
                index += 4
                continue
            if following in _ESCAPES:
                out.append(_ESCAPES[following])
                index += 2
                continue
        out.extend(char.encode("utf-8", "surrogateescape"))
        index += 1
    return os.fsdecode(bytes(out))


# --- the base text --------------------------------------------------------------------
def reverse_apply(current: str, change: FileChange) -> str | None:
    """``current`` (the file now) with ``change`` undone: the file at the base.

    None when the change does not fit: a hunk whose new side is not what the
    file holds at that place (the diff is of another tree), hunks out of order,
    or a binary change.
    """
    if change.binary:
        return None
    lines = split_lines(current)
    base: list[str] = []
    position = 0
    for hunk in change.hunks:
        start = hunk.new_start - 1 if hunk.new_count > 0 else hunk.new_start
        new = hunk.new
        found = lines[start : start + len(new)]
        if (
            start < position
            or len(found) != len(new)
            or any(
                _line_end(have) != _line_end(want) for have, want in zip(found, new, strict=True)
            )
        ):
            return None
        base.extend(lines[position:start])
        base.extend(hunk.old)
        position = start + len(new)
    base.extend(lines[position:])
    return "".join(base)


def _line_end(line: str) -> str:
    """``line`` with a CRLF end read as LF; one with no end at all stays without."""
    return line[:-2] + "\n" if line.endswith("\r\n") else line


# --- where the diff comes from -------------------------------------------------------------
def read_patch(spec: str) -> str:
    """The patch ``--new-from-patch`` names (``-``: stdin), as text. Unreadable is exit 2."""
    try:
        if spec == STDIN:
            stream = sys.stdin
            if stream is None:
                raise TriplConfigError("--new-from-patch -: there is no standard input to read.")
            raw = stream.buffer.read() if hasattr(stream, "buffer") else stream.read().encode()
        else:
            raw = Path(spec).read_bytes()
    except OSError as exc:
        raise TriplConfigError(
            f"--new-from-patch: cannot read {spec}: {exc.strerror or exc}."
        ) from None
    return raw.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class GitChange:
    """What ``--new-from-rev REF`` read from git.

    ``root`` is the repository's top level, ``sha`` the commit ``REF`` names,
    ``diff`` the diff from it to the working tree (tracked files), and
    ``untracked`` every file git does not track, ignored ones included, each
    relative to ``root``; a directory none of whose files git tracks is
    listed once, ending in ``/``.
    """

    root: Path
    sha: str
    diff: str
    untracked: tuple[str, ...] = ()


def from_git(ref: str, start: Path, runner: Runner = subprocess_runner) -> GitChange:
    """The change for ``--new-from-rev REF``: the diff from ``REF`` to the working tree, and more.

    The diff holds tracked files only, so the files git does not track are
    listed too, in every checked-out submodule the check root reaches as
    well: one the scan reads (generated code, a file not added yet) has no
    base the diff can give, and counts as new. ``which`` is imported
    into this module and called unqualified, so a test can rebind it, as
    ``install/docker.py`` does.
    """
    if which(GIT) is None:
        raise TriplConfigError(
            "--new-from-rev needs git, and git was not found on PATH: install it, run "
            "the ghcr.io/tripl-io/tripl-cli image (which ships it), or pass the change "
            "as a unified diff with --new-from-patch FILE."
        )
    top = _git(runner, "rev-parse", ("rev-parse", "--show-toplevel"), start)
    toplevel = Path(top.stdout.rstrip("\r\n"))
    found = _git(
        runner,
        "rev-parse",
        ("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"),
        toplevel,
        ref=ref,
    )
    sha = found.stdout.strip()
    diff = _git(runner, "diff", (*GIT_CONFIG, *DIFF_FLAGS, sha, "--"), toplevel)
    untracked = tuple(_untracked(runner, toplevel, _scope(start, toplevel)))
    return GitChange(toplevel, sha, diff.stdout, untracked)


def _scope(start: Path, top: Path) -> str | None:
    """The check root relative to the repository's top level; None when it lies outside."""
    try:
        return start.resolve().relative_to(top.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _untracked(runner: Runner, top: Path, scope: str | None) -> list[str]:
    """What git does not track under ``top`` and in the checked-out submodules ``scope`` reaches.

    A submodule is its own repository, so the top level's listing stops at
    it; its untracked files are listed by its own git, prefixed with its
    path, and so on down. ``scope`` is the check root relative to ``top``
    ("." for all of it): a submodule outside it is not the scan's.
    """
    others = _git(runner, "ls-files", (*GIT_CONFIG, *LS_OTHERS), top)
    found = [_path(line) for line in others.stdout.splitlines() if line.strip()]
    if scope is None:
        return found
    staged = _git(runner, "ls-files", (*GIT_CONFIG, *LS_STAGE), top)
    seen: set[str] = set()
    for line in staged.stdout.splitlines():
        meta, tab, raw = line.partition("\t")
        if not tab or meta.split(" ", 1)[0] != GITLINK:
            continue
        sub = _path(raw).rstrip("/")
        if sub in seen or not sub:
            continue
        seen.add(sub)
        if scope == "." or scope == sub or scope.startswith(f"{sub}/"):
            inner = scope[len(sub) + 1 :] if scope.startswith(f"{sub}/") else "."
        elif sub.startswith(f"{scope}/"):
            inner = "."
        else:
            continue
        if not os.path.exists(top / sub / ".git"):
            continue  # not checked out: nothing of it is on disk to scan
        found.extend(f"{sub}/{entry}" for entry in _untracked(runner, top / sub, inner))
    return found


def _git(
    runner: Runner, what: str, argv: Sequence[str], cwd: Path, *, ref: str | None = None
) -> CommandResult:
    """Run ``git ARGV`` in ``cwd``, captured; every failure is exit 2 with what to do about it."""
    command = Command((GIT, *argv), cwd=cwd, env=GIT_ENV, timeout=GIT_TIMEOUT, encoding="utf-8")
    result = runner(command, True)
    if result.timed_out:
        raise TriplConfigError(
            f"git {what} took longer than {GIT_TIMEOUT:.0f} s in {cwd}, so the change "
            "is unknown; run again, or pass the change as a diff with --new-from-patch FILE."
        )
    if result.ok and (ref is None or result.stdout.strip()):
        return result
    stderr = result.stderr.strip()
    if "dubious ownership" in stderr:
        at = re.search(r"repository at '([^']*)'", stderr)
        where = at.group(1) if at is not None else str(cwd)
        raise TriplConfigError(
            f"git refuses the repository at {where}: it belongs to another user "
            '("detected dubious ownership"). If you trust this checkout, mark it safe '
            f'and run again: git config --global --add safe.directory "{where}"'
        )
    if ref is not None:
        raise TriplConfigError(
            f"--new-from-rev: the commit {ref} is not in this clone; {_fetch(ref)}"
        )
    if what == "rev-parse":
        raise TriplConfigError(
            f"--new-from-rev needs a git checkout, and {cwd} is not in one"
            f"{_detail(stderr)}; or pass the change as a diff with --new-from-patch FILE."
        )
    raise TriplConfigError(f"git {what} failed in {cwd}{_detail(stderr)}.")


def _fetch(ref: str) -> str:
    """How to bring ``ref`` into a shallow clone, by what kind of name it is."""
    if _FULL_SHA.fullmatch(ref):
        return f"fetch it: git fetch --no-tags --depth=1 origin {ref}"
    if _SHA.fullmatch(ref):
        # A remote answers a fetch by full SHA or by name, never by a prefix.
        return (
            "an abbreviated SHA cannot be fetched: pass the full SHA, or fetch the history "
            "that holds it (git fetch --no-tags --unshallow origin, or a full clone)"
        )
    if any(mark in ref for mark in ("^", "~", "@{", ":")):
        # A commit relative to another (HEAD^1): only more history brings it.
        return (
            "fetch the history it needs (git fetch --no-tags --deepen=1 origin, or check "
            "out with a depth of 2 or more), or name the base commit by its SHA"
        )
    branch = ref.removeprefix("origin/")
    return (
        f"fetch it and name the remote branch: git fetch --no-tags --depth=1 origin "
        f"{branch}, then --new-from-rev origin/{branch}"
    )


def _detail(stderr: str) -> str:
    first = stderr.splitlines()[0].strip() if stderr else ""
    return f" ({first})" if first else ""
