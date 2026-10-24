"""The gate: which findings are new in a change (``--new-from-rev``, ``--new-from-patch``).

A finding fails a gated run only when the change brought it in. That is
decided per ITEM, not per line: an item is new or changed when the tree at the
base, scanned and resolved by the same CLI with the same config, has no item
that sends the same thing by the same callee, in any file (``item_key``). So a
call that was only moved, to another file too, reformatted or renamed with its
file stays unchanged, while an
edit that changes what a call sends is caught wherever it was made, a
constant in another file or an ``enums:`` file included.

The base tree is never checked out. The diff's hunks are undone on each
changed file (``changes.reverse_apply``) and the result handed to the scan's
own passes (``scan.base_items``). When the diff touches no file the scan
reads, nothing is scanned twice: every item is unchanged.

What the gate cannot rebuild fails CLOSED, never open, because a gate that
goes green on a change it did not understand is the worst answer it can give:

- a file the diff does not fit (or a binary one, or one a patch deletes that
  is still there) is absent at the base, so every item in it is changed;
- a file the diff deletes whose base cannot be rebuilt may have defined
  anything, so every item is changed;
- a submodule the diff moves is absent at the base as a whole: the diff shows
  its commit, not its files;
- a path the diff names that this checkout does not have (a patch of another
  tree, or with paths relative to somewhere else) makes EVERY item changed,
  since there is no telling which file it meant.

A patch's paths are relative to the repository root when the check root is
in one, else to the working directory, as ``git apply`` reads them. A file
changed several times (a patch series) is undone change by change, and a
scanned symlink counts as the file it points at.

A run against a plan branch takes one more look: the branch's own edits can
fail an unchanged call (an event it archives). Those items are validated
against main as well (``candidates``, in the caller's one ``run_gate``), and
a finding main does not have, or has less severe, is new: the branch caused it
(``plan_branch_causes``).

``apply_gate`` writes the answer onto the results as ``in_change`` and
``cause``, on copies; the rest is ``CheckReport.fails``.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from tripl_cli.check.changes import FileChange, reverse_apply
from tripl_cli.check.config import CheckConfig
from tripl_cli.check.model import (
    CAUSE_PLAN_BRANCH,
    CODE_NO_VERDICT,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    CheckItem,
    CheckReport,
    CheckResult,
    finding_fails,
)
from tripl_cli.check.scan import Snapshot, base_items, discover, enum_files, scanned
from tripl_cli.model import JsonDict

_RANK = {SEVERITY_ERROR: 2, SEVERITY_WARNING: 1}


@dataclass(frozen=True)
class Marking:
    """What the gate learned from the diff: one ``changed`` flag per scanned item.

    ``files`` counts the files the diff changes; ``paths`` are the ones the
    scan reads, relative to the check root, at their new path;
    ``unreconstructable`` the ones of those the diff did not fit, and each
    submodule it moves (ending in ``/``); ``unmapped`` the paths it names that
    the checkout does not have, which make every item changed.
    """

    changed: tuple[bool, ...]
    files: int
    paths: frozenset[str] = frozenset()
    unreconstructable: tuple[str, ...] = ()
    unmapped: tuple[str, ...] = ()
    # Scanned files git does not track (generated, not added yet): new.
    untracked: tuple[str, ...] = ()


def item_key(item: CheckItem) -> str:
    """What an item is for the gate: the call it is sent by, what it sends, what it waives.

    Never its line or its file: a call moved to another file, or a function
    pulled out into a new one, sends what it sent. The call is its bare
    function name or Objective-C selector (``_called``), not the receiver as
    written: ``self.`` dropped, a receiver renamed, or a qualified name
    imported calls the same thing. A placeholder's name in an interpolated
    value (``promo_${sheetId}_opened``) is blanked as the validator reads it
    (``_holes``), so renaming the variable changes nothing, while its text
    and the number and place of its holes do. What is a set in what is
    sent (the targets only known at runtime, the notes) is compared sorted,
    so reordering named arguments or object keys changes nothing. The waived
    part is the targets of the valid markers on the call, not their reasons:
    removing a marker, dropping its reason (so it waives nothing) or
    changing a code it lists changes the call, and so does pasting an
    unmarked copy of a marked call, which cannot take the marked one's place
    at the base; rewording a reason does not.
    """
    notes = sorted((note.code, note.field or "") for note in item.notes)
    waived = sorted(
        {
            (code, field or "")
            for suppression in item.suppressions
            for code, field in suppression.targets
        }
    )
    return json.dumps(
        [
            _called(item.origin.snippet),
            _holes(item.wire()),
            item.sendable,
            sorted(item.dynamic),
            notes,
            waived,
        ],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def _called(snippet: str) -> str:
    """The callee's last name (``Analytics.shared.log`` is ``log``); a selector as it is."""
    return snippet.rsplit(".", 1)[-1]


# A ``${…}`` placeholder, as the backend's name templates read one.
_HOLE = re.compile(r"\$\{[^}]*\}")


def _holes(value: Any) -> Any:
    """``value`` with every placeholder's name blanked (``${id}`` is ``${}``), nested."""
    if isinstance(value, str):
        return _HOLE.sub("${}", value)
    if isinstance(value, Mapping):
        return {_holes(key): _holes(entry) for key, entry in value.items()}
    if isinstance(value, list | tuple):
        return [_holes(entry) for entry in value]
    return value


@dataclass(frozen=True)
class _Net:
    """One file's whole change, a patch series folded into one: where it was and is, and its base.

    ``base_path`` and ``final`` are the file's path (relative to the diff's
    root) at the base and today, None where it does not exist; ``text`` its
    base text, None when the base did not have it; ``broken`` when that text
    cannot be rebuilt; ``lines`` the lines of today's file the diff touched.
    """

    base_path: str | None
    final: str | None
    text: str | None
    broken: bool = False
    lines: frozenset[int] = frozenset()
    # Broken because a hunk does not fit the file the diff names: proof the
    # diff is not of this tree, unlike a binary or unreadable file.
    mismatch: bool = False


def mark(
    config: CheckConfig,
    items: Sequence[CheckItem],
    changes: Sequence[FileChange],
    root: Path,
    *,
    trusted: bool = False,
    untracked: Sequence[str] = (),
    unread: Collection[str] = (),
) -> Marking:
    """Which of ``items`` (this tree's scan) the change made new or changed.

    ``changes`` are the diff's, their paths relative to ``root``: git's top
    level for ``--new-from-rev``; for ``--new-from-patch``, the repository
    root, or the working directory outside a repository. ``trusted`` says the
    diff is this very tree's (git's own, for ``--new-from-rev``), so a file it
    deletes that is still on disk (untracked by the change) is compared with
    its base like any other. ``untracked`` are the files git does not track
    (``changes.GitChange``): one the scan reads has no base the diff can give,
    so it is absent there, and new. ``unread`` are the files today's scan was
    configured to read and did not (``scan.ScanOutcome.skipped``: too large,
    unreadable): their base calls are not compared, so a copy of one pasted
    elsewhere cannot take its place, and is new.
    """
    every = (True,) * len(items)
    nets, submodules, missing = _fold(changes, root)
    if missing:
        return Marking(every, len(nets) + len(submodules), unmapped=tuple(missing))
    files = len(nets) + len(submodules)
    prefix = _prefix(config.root, root)
    texts: dict[Path, str | None] = {}
    deleted: list[tuple[Path, str]] = []
    absent: list[Path] = []
    touched: dict[str, frozenset[int]] = {}
    paths: set[str] = set()
    broken: list[str] = []
    blind = False
    for moved in submodules:
        if _under(moved, prefix) is None and _within(prefix, moved):
            # The check root is in the submodule: all of it moved.
            return Marking(every, files, unreconstructable=(f"{moved}/",))
    walked = _walked(config) if nets or untracked or submodules else []
    links = _links(config, walked)
    for moved in submodules:
        inside = _under(moved, prefix)
        if inside is not None:
            absent.append(config.root / inside)
            broken.append(f"{inside}/")
        # A scanned link into the submodule reads what moved with it.
        for link in _into(links, root / moved, directory=True):
            texts[config.root / link] = None
            paths.add(link)
            broken.append(link)
    fresh: list[str] = []
    names = {relative for _, relative in walked}
    for entry in untracked:
        normal = _normal(entry.rstrip("/"))
        if normal is None:
            continue
        # A scanned link to a file git does not track (one generated into
        # build/, wherever that is) reads it: as new as the file.
        for link in _into(links, root / normal, directory=entry.endswith("/")):
            texts[config.root / link] = None
            paths.add(link)
            fresh.append(link)
        inside = _under(normal, prefix)
        if inside is None:
            if entry.endswith("/") and _within(prefix, normal):
                # The check root itself is untracked: nothing has a base.
                return Marking(every, files, untracked=(entry,))
            continue
        under = (
            [name for name in names if name.startswith(f"{inside}/")]
            if entry.endswith("/")
            else [inside]
            if inside in names
            else []
        )
        for name in under:
            texts[config.root / name] = None
            paths.add(name)
            fresh.append(name)
    for net in nets:
        if (
            net.final is None
            and net.base_path is not None
            and os.path.lexists(root / net.base_path)
            and not _behind_link(root, net.base_path)
        ):
            # Deleted by the diff and still on disk: untracked by the change
            # (git's own diff), compared with its base like a modified file;
            # else not this tree's diff, so every call in it is changed. One
            # reached through a directory the change made a link is another
            # file, read through the link: this one is deleted.
            net = replace(net, final=net.base_path, broken=net.broken or not trusted)
        old = _under(net.base_path, prefix)
        new = _under(net.final, prefix)
        old = old if old is not None and scanned(config, old) else None
        new = new if new is not None and scanned(config, new) else None
        linked = _linked(links, root, net, new)
        if old is None and new is None and not linked:
            if net.mismatch and not trusted:
                # A patch whose hunks do not fit a file it names, even one the
                # scan never reads, is not a patch of this tree.
                blind = True
                broken.append(net.final or net.base_path or "")
            continue
        for link in linked:
            # A scanned link to the file the diff changed: what the link reads
            # changed with it, wherever the file itself sits.
            texts[config.root / link] = None if net.broken else net.text
            paths.add(link)
            if net.broken:
                broken.append(link)
        if new is not None:
            paths.add(new)
            touched[new] = net.lines
            if net.broken:
                broken.append(new)
            # Absent at the base: added, or brought under the scan's config.
            texts[config.root / new] = None if net.broken or old is None else net.text
            continue
        if old is None:
            continue
        # Deleted, or moved out of the scan's config: the base has it at its
        # old path, where it can still define constants.
        if net.broken:
            # What it defined is unknown, so nothing that may read it is known.
            blind = True
            broken.append(old)
        elif net.text is not None:
            texts[config.root / old] = net.text
            deleted.append((config.root / old, old))
    # An untracked file the diff deletes (no longer tracked by the change)
    # has its base from the diff, and is compared with it.
    fresh = sorted({name for name in fresh if texts.get(config.root / name, "") is None})
    if links and any(change.symlink for change in changes):
        # The change moves a link: what any scanned link resolves through at
        # the base is not known from today's tree, so each counts as changed.
        for linked in links.values():
            for link in linked:
                texts[config.root / link] = None
                paths.add(link)
                broken.append(link)
    if blind:
        return Marking(
            every, files, frozenset(paths), tuple(sorted(set(broken))), untracked=tuple(fresh)
        )
    if not paths and not deleted and not absent:
        return Marking((False,) * len(items), files)
    found = [
        item
        for item in base_items(config, Snapshot(texts, tuple(deleted), tuple(absent)))
        if item.origin.path not in unread
    ]
    unreconstructable = tuple(sorted(set(broken)))
    return Marking(
        changed_items(
            items, found, touched, files=frozenset(paths), forced=(*unreconstructable, *fresh)
        ),
        files,
        frozenset(paths),
        unreconstructable,
        untracked=tuple(fresh),
    )


def _fold(changes: Sequence[FileChange], root: Path) -> tuple[list[_Net], list[str], list[str]]:
    """``(each file's net change, the submodules moved, the paths that map onto nothing)``.

    A diff may change one file several times (a ``git format-patch`` series,
    a pull request's ``.patch``): its changes are undone last to first, each
    on the text the one after it left, starting from today's file, renames
    followed. One step that does not fit breaks the whole file. A path that
    escapes ``root``, or that the diff leaves in the tree and the tree does
    not have, maps onto nothing.
    """
    state: dict[str, _Net] = {}
    submodules: list[str] = []
    missing: list[str] = []
    for change in reversed(changes):
        old, new = _normal(change.old_path), _normal(change.new_path)
        for raw, normal in ((change.old_path, old), (change.new_path, new)):
            if raw is not None and normal is None:
                missing.append(raw)
        if (change.old_path is not None and old is None) or (
            change.new_path is not None and new is None
        ):
            continue
        if change.submodule:
            if new is not None and not os.path.lexists(root / new):
                missing.append(new)
            submodules.append(new or old or "")
            continue
        if new is not None:
            after = state.pop(new, None)
            if after is None:
                after = _today(root, new, change)
                if after is None:
                    missing.append(new)
                    continue
            current = after.text
        else:
            # Gone after this change: a later change may have added it back.
            after = state.pop(old or "", None) or _Net(old, None, None)
            if after.text is not None:
                after = replace(after, broken=True)
            current = ""
        if old is None:
            # Added by this change: not there before it.
            state[new or ""] = replace(after, base_path=None, text=None)
            continue
        before = None
        tried = not (after.broken or change.binary) and current is not None
        if current is not None and tried:
            before = reverse_apply(current, change)
        state[old] = replace(
            after,
            base_path=old,
            text=before,
            broken=after.broken or before is None,
            mismatch=after.mismatch or (tried and before is None and not _lfs(change)),
        )
    # The base text as its bytes were, line ends included: the scan reads it
    # (and counts its size) as it reads a file on disk (``Snapshot.read``).
    nets = [net for net in state.values() if net.final is not None or net.base_path is not None]
    return nets, submodules, missing


def _lfs(change: FileChange) -> bool:
    """A change to a git-lfs pointer: the work tree holds the object, not the pointer."""
    return any(
        line[1:].startswith("version https://git-lfs.github.com/spec/")
        for hunk in change.hunks
        for line in hunk.lines
    )


def _today(root: Path, path: str, change: FileChange) -> _Net | None:
    """The file at ``path`` as it is now, read as the scan reads it; None when there is none."""
    lines = frozenset(line for hunk in change.hunks for line in hunk.added())
    target = root / path
    if not os.path.lexists(target):
        return None
    try:
        text = target.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return _Net(path, path, None, broken=True, lines=lines)
    return _Net(path, path, text, lines=lines)


def _normal(path: str | None) -> str | None:
    """``path`` without ``./`` or ``x/..`` parts; None when it leaves the root or is absolute."""
    if path is None:
        return None
    normal = posixpath.normpath(path)
    if posixpath.isabs(normal) or normal == ".." or normal.startswith("../"):
        return None
    return normal


def _walked(config: CheckConfig) -> list[tuple[Path, str]]:
    """``(path, relative)`` of every file the scan reads today, sources and enum files."""
    walked = [(source.path, source.relative) for source in discover(config)]
    return walked + [(path, relative) for path, relative, _ in enum_files(config)]


def _links(config: CheckConfig, walked: Sequence[tuple[Path, str]]) -> dict[str, list[str]]:
    """Each scanned file reached through a symlink, by the real path it reads.

    The walk lists a link to a file as a file, and the scan reads through it,
    so a diff of the target changes what the link sends.
    """
    resolved = config.root.resolve()
    found: dict[str, list[str]] = defaultdict(list)
    for path, relative in walked:
        real = os.path.realpath(path)
        if real != str(resolved / relative) and relative not in found[real]:
            found[real].append(relative)
    return dict(found)


def _into(links: Mapping[str, list[str]], target: Path, *, directory: bool) -> list[str]:
    """The scanned links that read ``target``, or (``directory``) a file under it."""
    if not links:
        return []
    real = os.path.realpath(target)
    return [
        link
        for at, linked in links.items()
        if at == real or (directory and at.startswith(f"{real.rstrip(os.sep)}{os.sep}"))
        for link in linked
    ]


def _behind_link(root: Path, path: str) -> bool:
    """Whether a directory on the way from ``root`` to ``path`` is a symlink now."""
    parts = PurePosixPath(path).parts[:-1]
    return any(os.path.islink(root.joinpath(*parts[: depth + 1])) for depth in range(len(parts)))


def _linked(links: Mapping[str, list[str]], root: Path, net: _Net, own: str | None) -> list[str]:
    """The scanned links whose target is the file ``net`` changes (not the link itself)."""
    if not links:
        return []
    at = net.final or net.base_path
    if at is None or os.path.islink(root / at):
        return []  # a changed link is its own change, not its target's
    real = os.path.realpath(root / at)
    return [link for link in links.get(real, ()) if link != own]


def _within(prefix: PurePosixPath | None, path: str) -> bool:
    """Whether the check root (``prefix``, from the diff's root) lies inside ``path``."""
    if prefix is None:
        return False
    return str(prefix) == path or str(prefix).startswith(f"{path.rstrip('/')}/")


def _prefix(root: Path, repo_root: Path) -> PurePosixPath | None:
    """The check root relative to the repository root; None when it lies outside it."""
    try:
        return PurePosixPath(root.resolve().relative_to(repo_root.resolve()).as_posix())
    except (ValueError, OSError):
        return None


def _under(path: str | None, prefix: PurePosixPath | None) -> str | None:
    """A repository-relative diff path, relative to the check root; None outside it."""
    if path is None or prefix is None:
        return None
    if str(prefix) == ".":
        return path
    try:
        return PurePosixPath(path).relative_to(prefix).as_posix()
    except ValueError:
        return None


def changed_items(
    items: Sequence[CheckItem],
    base: Sequence[CheckItem],
    touched: Mapping[str, frozenset[int]],
    *,
    files: frozenset[str] = frozenset(),
    forced: Sequence[str] = (),
) -> tuple[bool, ...]:
    """Per item: True when the base has fewer items like it (``item_key``) than the tree.

    Compared as multisets over the whole scan, whatever file each is in:
    three identical calls where the base had two give one changed item, and
    a call deleted in one file and added in another is the same call. Which
    one is changed is the diff's say: the items in files the change left
    alone take up the base's count first, then those in the ``files`` it
    changed on lines it did not touch, then the rest. An item in a
    ``forced`` file (one with no base: untracked, unreconstructable, under a
    moved submodule's ``dir/``) is changed whatever the base has, and takes
    up none of it.
    """
    keys = [item_key(item) for item in items]
    left = Counter(item_key(item) for item in base)
    changed = [False] * len(items)
    exact = {path for path in forced if not path.endswith("/")}
    dirs = tuple(path for path in forced if path.endswith("/"))

    def rank(index: int) -> int:
        item = items[index]
        if item.origin.path not in files:
            return 0
        return 2 if _touches(item, touched) else 1

    for index in sorted(range(len(items)), key=rank):
        path = items[index].origin.path
        if path in exact or path.startswith(dirs):
            changed[index] = True
        elif left[keys[index]] > 0:
            left[keys[index]] -= 1
        else:
            changed[index] = True
    return tuple(changed)


def _touches(item: CheckItem, touched: Mapping[str, frozenset[int]]) -> bool:
    lines = touched.get(item.origin.path)
    if not lines:
        return False
    last = item.origin.end_line if item.origin.end_line is not None else item.origin.line
    return any(item.origin.line <= line <= last for line in lines)


def candidates(results: Sequence[CheckResult], changed: Sequence[bool], strict: bool) -> list[int]:
    """The unchanged results a plan branch could have failed: each with a finding that fails.

    ``no_verdict`` aside, which fails whatever main says.
    """
    return [
        index
        for index, (result, new) in enumerate(zip(results, changed, strict=True))
        if not new
        and any(
            finding.code != CODE_NO_VERDICT and finding_fails(finding, strict)
            for finding in result.findings
        )
    ]


def plan_branch_causes(
    branch: Sequence[CheckResult], main: Sequence[CheckResult]
) -> tuple[tuple[bool, ...], ...]:
    """Per result, per finding: whether the plan branch caused it.

    A finding is the branch's when main's verdict on the same item has no
    finding with the same code and field at the same severity or a higher one,
    counted as multisets: an event the branch archives turns main's warning
    into the branch's error, and that error is new.
    """
    causes: list[tuple[bool, ...]] = []
    for on_branch, on_main in zip(branch, main, strict=True):
        pool: dict[tuple[str, str | None], list[int]] = defaultdict(list)
        for finding in on_main.findings:
            pool[(finding.code, finding.field)].append(_RANK.get(finding.severity, 0))
        for ranks in pool.values():
            ranks.sort(reverse=True)
        caused = [False] * len(on_branch.findings)
        order = sorted(
            range(len(on_branch.findings)),
            key=lambda index: -_RANK.get(on_branch.findings[index].severity, 0),
        )
        for index in order:
            finding = on_branch.findings[index]
            ranks = pool[(finding.code, finding.field)]
            # The most severe of main's left: it matches this one if any does,
            # and the branch's findings after this one are no more severe.
            if ranks and ranks[0] >= _RANK.get(finding.severity, 0):
                ranks.pop(0)
            else:
                caused[index] = True
        causes.append(tuple(caused))
    return tuple(causes)


def apply_gate(
    results: Sequence[CheckResult],
    changed: Sequence[bool],
    causes: Mapping[int, tuple[bool, ...]],
) -> tuple[CheckResult, ...]:
    """``results`` with ``in_change`` (and ``cause``) on each result and finding, as copies.

    ``causes`` maps a result's index to ``plan_branch_causes``'s answer for it.
    """
    gated: list[CheckResult] = []
    for index, (result, new) in enumerate(zip(results, changed, strict=True)):
        caused = causes.get(index, ())
        findings = tuple(
            replace(finding, in_change=True)
            if new
            else replace(finding, in_change=True, cause=CAUSE_PLAN_BRANCH)
            if position < len(caused) and caused[position]
            else replace(finding, in_change=False)
            for position, finding in enumerate(result.findings)
        )
        gated.append(replace(result, findings=findings, in_change=new))
    return tuple(gated)


def gate_document(report: CheckReport) -> JsonDict | None:
    """The gate as ``--json`` and SARIF carry it; None for a run with no gate.

    ``changed_items`` counts the results the change made new or changed,
    ``failing`` the ones that fail the run, ``not_new`` the ones that would
    fail it without the gate and do not with it.
    """
    gate = report.gate
    if gate is None:
        return None
    branch = gate.plan_branch
    return {
        "source": gate.source,
        "rev": gate.rev,
        "base": gate.base,
        "patch": gate.patch,
        "changed_files": gate.files,
        "changed_items": sum(1 for result in report.results if result.in_change),
        "unreconstructable": list(gate.unreconstructable),
        "unmapped": list(gate.unmapped),
        "untracked": list(gate.untracked),
        "plan_branch": (
            None if branch is None else {"compared": branch.compared, "caused": branch.caused}
        ),
        "failing": sum(1 for result in report.results if report.fails(result)),
        "not_new": sum(
            1
            for result in report.results
            if report.verdict_fails(result) and not report.fails(result)
        ),
    }
