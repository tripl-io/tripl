"""``tripl check`` — validate tracking calls in source code, or captured events, against the plan.

One word, by the grammar in ``commands/__init__.py``: it acts on a checkout (or
a capture file) as a whole, not on a class of objects an operator browses.

Three modes, one scan and one validator between them:

* **static** (default) — scan the files ``.tripl/check.yml`` names for the
  tracking calls it describes, per event type: the project's own wrapper
  functions first, SDK presets (Segment, Amplitude, Snowplow) as shorthands.
  Every call becomes an item with whatever is knowable at scan time; a value
  only known at runtime is sent as ``null``, which is never an error.
* **payloads** (``--payloads FILE``) — validate captured events, NDJSON or JSON.
  These are what the app really sent, so a missing required field IS an error.
* **list** (``--list``) — the static scan alone, for writing the config: each
  call it matches with what was read and why a value was not, the functions
  that forward their own parameter into a tracking call, and the calls that
  look like tracking but match no call spec. It needs no URL and no key, opens
  no connection and sends nothing; with no check config found it scans the
  whole repository it runs in (discovery).

The first two batch their items to ``POST /projects/{slug}/plan/validate`` — a
READ that happens to be a POST, so any project member's key works, ``tk_r_``
included, and there is no ``--dry-run`` because nothing is written.
``--print-preset [NAME]`` prints a preset's call specs as YAML to start a
custom one from, and exits the moment it is parsed, the way ``--version`` does.

One run, one set of verdicts, as many reports as a pipeline wants: ``--format``
picks what goes to stdout, and each ``--report FORMAT=PATH`` writes the same
result to a file — GitLab Code Quality and JUnit for a merge request, SARIF for
code scanning, the JSON document for scripts. A failed run writes none of them
and removes what an earlier run left at those paths, so a stale report can
never pass for this run's. Only a command line the parser rejects outright (an
unknown flag, or ``--list`` with ``--payloads``, say) leaves them, because then
no command runs at all; so does ``--print-preset``, which runs none.

``--new-from-rev REF`` or ``--new-from-patch FILE`` gate the run on a change:
only findings the change brought in fail it (``check/gate.py``). The change
is read, and the base tree scanned, before anything is sent, so a bad REF or
an unreadable patch is exit 2 with no request made; a run against a plan
branch then validates the unchanged calls it would fail against main as well,
in the same ``run_gate``. The gate changes the exit code, the listing and the
gate fields of JSON and SARIF; Code Quality and JUnit read the same with it
and without.

Exit codes: 0 clean; 1 when any item has an error (or, with ``--strict``, a
warning — ``--strict`` also reports values only known at runtime) that no
``tripl-check-ignore`` marker suppresses (``check/suppress.py``) and that, under
a gate, is new in the change; 2 for a bad config, a bad payload file, a bad
invocation, a change that cannot be read (no git, an unknown REF, an
unreadable patch), or a config that matches no call
at all (``--allow-empty`` accepts that); 4 when the instance was unavailable —
unreachable, timed out, 429 or 5xx — so nothing was decided. An instance that
refuses the key is exit 1, with the reason on stderr. ``--list`` exits 0
whenever the scan ran, a reader that closed the pipe early included, and 2 for
a bad config or a flag it refuses (``--report``, ``--branch``, ``--strict``,
``--allow-empty``, and a ``--format`` other than text or JSON).

``--check-config`` rather than ``--config``: ``--config`` is already the global
flag naming the CLI's own connection settings file, on every command.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import httpx

from tripl_cli.check import changes as changes_mod
from tripl_cli.check import gate as gate_mod
from tripl_cli.check import payloads as payloads_mod
from tripl_cli.check import scan as scan_mod
from tripl_cli.check.config import DEFAULT_EXCLUDES, DEFAULT_SOURCES, CheckConfig, repository_root
from tripl_cli.check.inventory import inventory
from tripl_cli.check.listing import render_listing
from tripl_cli.check.model import (
    GATE_PATCH,
    GATE_REV,
    MODE_PAYLOADS,
    MODE_STATIC,
    CheckItem,
    CheckReport,
    CheckResult,
    GateInfo,
    Marker,
    PlanBranchComparison,
    SkippedFile,
)
from tripl_cli.check.presets import PRESETS, preset_yaml
from tripl_cli.check.render import nothing_matched, render_text, sarif_document
from tripl_cli.check.reports import codequality_document, junit_document, repo_paths
from tripl_cli.check.suppress import marker_states
from tripl_cli.check.validate import validate
from tripl_cli.commands import add_json, add_timeout
from tripl_cli.commands._check_config import (
    add_check_config,
    add_project_flag,
    check_config_of,
    project_flag,
    project_of,
)
from tripl_cli.commands._plan import Branch, add_branch, begin, resolve_branch
from tripl_cli.config import Config
from tripl_cli.diagnostics.collect import instance_of
from tripl_cli.errors import TriplConfigError, TriplError
from tripl_cli.install.shell import Runner, subprocess_runner
from tripl_cli.model import Run, to_rfc3339
from tripl_cli.render import render_header
from tripl_cli.report import check_document, check_list_document
from tripl_cli.runner import run_gate

FORMAT_TEXT = "text"
FORMAT_JSON = "json"
FORMAT_SARIF = "sarif"
FORMAT_CODEQUALITY = "codequality"
FORMAT_JUNIT = "junit"
FORMATS = (FORMAT_TEXT, FORMAT_JSON, FORMAT_SARIF, FORMAT_CODEQUALITY, FORMAT_JUNIT)
# What `--list` with no check config leaves out beyond the default excludes:
# every hidden directory (see `_discovery_config`).
DISCOVERY_EXCLUDE = "**/.*/**"


class _PrintPreset(argparse.Action):
    """``--print-preset [NAME]``: print call specs as YAML and exit, the way ``--version`` does.

    It acts the moment argparse reaches it: a bad value before it is still
    exit 2, and nothing after it is read. So no command runs: like ``--help``
    it prints its YAML even with ``--json``, and leaves ``--report`` files
    alone. An unknown NAME is argparse's own invalid choice, which lists them.
    It prints the way ``--list`` does, so a reader that left early
    (``| true``) ends the output, and the exit is still 0.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        names = list(PRESETS) if values is None else [str(values)]
        # sys.stdout looked up now rather than bound at import, as a capture swaps it.
        _emit("\n\n".join(preset_yaml(name) for name in names), sys.stdout)
        parser.exit()


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    parent: argparse.ArgumentParser,
) -> None:
    parser = subparsers.add_parser(
        "check",
        parents=[parent],
        help="validate tracking calls in code, or captured events, against the plan",
        description=(
            "Scan source files for the tracking calls .tripl/check.yml describes and "
            "validate each against the tracking plan: unknown event types and events, "
            "unknown fields, values outside allowed values. With --payloads, validate "
            "captured events instead, where a missing required field is an error too. "
            "With --list, only scan, and list what was read from each call and why a "
            "value was not; nothing is sent. "
            "A finding can be suppressed by a comment at its call, "
            "tripl-check-ignore[CODE] -- REASON. "
            "With --new-from-rev or --new-from-patch, only findings new in that change "
            "fail the run. "
            "Exits 1 on any error (or warning, with --strict) that is not suppressed (and, "
            "with a gate, is new in the change), 4 when the instance was unavailable."
        ),
    )
    add_check_config(parser)
    # One mode per run: argparse refuses the pair before any command runs.
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--list",
        dest="list",
        action="store_true",
        help=(
            "only scan: list what the scan reads from each call the check config matches, "
            "why a value is not read, and the calls that look like tracking; needs no "
            "key and sends nothing"
        ),
    )
    mode.add_argument(
        "--payloads",
        dest="payloads",
        metavar="FILE",
        help="validate captured events (NDJSON, or a JSON array; - for stdin) instead of source",
    )
    # Outside the group: it acts as soon as argparse reaches it, so with the
    # pair in one group `--list --print-preset x` would be refused and
    # `--print-preset x --list` would print.
    parser.add_argument(
        "--print-preset",
        action=_PrintPreset,
        nargs="?",
        choices=tuple(PRESETS),
        metavar="NAME",
        default=argparse.SUPPRESS,
        help=(
            "print the call specs of preset NAME (every preset without NAME) as YAML to "
            f"paste under an event type's calls:, and exit; the presets are {', '.join(PRESETS)}"
        ),
    )
    # One change per run; neither flag reads a CI variable.
    gate = parser.add_mutually_exclusive_group()
    gate.add_argument(
        "--new-from-rev",
        dest="new_from_rev",
        metavar="REF",
        help=(
            "fail only on findings new in the change from commit REF to the working tree "
            "(needs git and the REF commit in the clone; an empty REF gates nothing)"
        ),
    )
    gate.add_argument(
        "--new-from-patch",
        dest="new_from_patch",
        metavar="FILE",
        help=(
            "fail only on findings new in the change FILE holds, a unified diff from the base "
            "to the working tree (git diff format; - for stdin); needs no git"
        ),
    )
    add_project_flag(parser)
    add_branch(parser)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 on warnings too, and report values that are only known at runtime",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=FORMATS,
        default=None,
        help=(
            "output format on stdout (default: text): json, sarif (SARIF 2.1.0, for code "
            "scanning), codequality (GitLab Code Quality) or junit (JUnit XML)"
        ),
    )
    parser.add_argument(
        "--report",
        dest="reports",
        metavar="FORMAT=PATH",
        action="append",
        help=(
            "also write the result as FORMAT to the file PATH; repeat for more reports "
            "from the same run, e.g. --report codequality=gl-code-quality-report.json"
        ),
    )
    parser.add_argument(
        "--allow-empty",
        dest="allow_empty",
        action="store_true",
        help=(
            "exit 0 when there is nothing to check: no call matched the check config, "
            "or the --payloads file holds no events"
        ),
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run, abandon=abandon)


def _format(args: argparse.Namespace) -> str:
    chosen: str | None = args.output_format
    if args.as_json:
        if chosen not in (None, FORMAT_JSON):
            raise TriplConfigError(f"--json and --format {chosen} ask for two outputs; pick one.")
        return FORMAT_JSON
    return chosen or FORMAT_TEXT


def _report_targets(args: argparse.Namespace) -> list[tuple[str, Path]]:
    """Each ``--report FORMAT=PATH``, checked before anything is read or sent.

    A bad one is a refusal like any other failed run: the files the other
    ``--report`` flags name are removed first, so what an earlier run left
    there cannot pass for this one's.
    """
    targets: list[tuple[str, Path]] = []
    problem: str | None = None
    seen: set[Path] = set()
    for spec in args.reports or ():
        report_format, separator, raw = spec.partition("=")
        refusal = _refusal(spec, report_format, separator, raw, seen)
        if refusal is not None:
            problem = problem or refusal
            continue
        path = Path(raw)
        seen.add(path.resolve())
        targets.append((report_format, path))
    if problem is not None:
        abandon(args)
        raise TriplConfigError(problem)
    return targets


def _refusal(
    spec: str, report_format: str, separator: str, raw: str, seen: set[Path]
) -> str | None:
    if not separator or not report_format or not raw:
        return (
            f"--report {spec!r}: write it as FORMAT=PATH, "
            "for example codequality=gl-code-quality-report.json."
        )
    if report_format not in FORMATS:
        return (
            f"--report {spec!r}: {report_format!r} is not a format; "
            f"use one of {', '.join(FORMATS)}."
        )
    if raw == "-":
        return f"--report {spec!r}: stdout is --format's; give --report a file path."
    if not _names_a_file(raw):
        return f"--report {spec!r}: {raw} names a directory; give --report a file path."
    path = Path(raw)
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
        # A report replaces the file at PATH, so a link would be replaced
        # rather than written through, and /dev/stdout swapped for a file.
        return (
            f"--report {spec!r}: {raw} is a directory, a link or a special file; "
            "give --report the path of a regular file."
        )
    if path.resolve() in seen:
        return f"--report names {raw} twice; each report needs its own file."
    return None


def _names_a_file(raw: str) -> bool:
    # As typed, not through Path(), which drops a trailing separator and "."
    # parts: "reports/" would otherwise become a FILE named reports.
    return raw != "-" and os.path.basename(raw) not in ("", ".", "..")


def abandon(args: argparse.Namespace) -> None:
    """Remove what an earlier run left at every ``--report`` path, for a run that has none.

    Called on every run that cannot finish: by ``run`` when the check fails,
    and by ``cli.main`` when the connection settings do not resolve, before
    ``run`` starts. A spec without ``FORMAT=`` names no path, so a mistyped
    ``--report src/app.ts`` never touches a source file.
    """
    named = (spec.partition("=")[2] for spec in args.reports or ())
    _discard(Path(raw) for raw in named if raw and _names_a_file(raw))


def run(args: argparse.Namespace, config: Config) -> int:
    targets = _report_targets(args)
    try:
        if args.list:
            return _list(args)
        return _check(args, config, targets)
    except BaseException:
        # Any run that did not finish, a crash or Ctrl-C included, leaves no
        # report behind: not this run's half, not an earlier run's whole.
        # `--list` refusing a flag is one of those too.
        abandon(args)
        raise


def _list(args: argparse.Namespace) -> int:
    """``--list``: the static scan alone, each matched call shown in full. 0 once it ran.

    Never ``begin(config)``: no URL, no key, no client, nothing sent, so it
    works with no connection settings at all. ``--timeout`` always has a value
    and cannot be told from its default, so it is ignored rather than refused.
    """
    started = time.monotonic()
    generated_at = datetime.now(UTC)
    refusal = _list_refusal(args)
    if refusal is not None:
        raise TriplConfigError(refusal)
    output = _format(args)
    project = project_flag(args, "check")
    check_config = check_config_of(args)
    discovery = check_config is None
    if check_config is None:
        check_config = _discovery_config()
    elif project is None:
        project = check_config.project
    found = inventory(check_config, discovery=discovery)
    config_path = None if check_config.path is None else str(check_config.path)
    # Each stream on its own: a reader that left stderr early, or a stderr that
    # cannot be written at all, still lets the JSON through.
    human = sys.stdout if output == FORMAT_TEXT else sys.stderr
    _emit(render_listing(found, config=config_path), human, primary=output == FORMAT_TEXT)
    if output == FORMAT_JSON:
        document = check_list_document(
            found,
            generated_at=to_rfc3339(generated_at),
            duration_ms=int((time.monotonic() - started) * 1000),
            project=project,
            config=config_path,
        )
        _emit(json.dumps(document), sys.stdout)
    return 0


def _list_refusal(args: argparse.Namespace) -> str | None:
    """Why a flag ``--list`` has no use for is refused rather than quietly ignored.

    A flag that quietly did nothing would be read as one that did something
    (the rule ``add_branch`` follows too). ``--report`` is asked about last, so
    a run that also names a report path still hears about its other flags.
    """
    if args.branch is not None:
        return (
            "--branch names the plan branch to check against, and --list sends nothing; "
            "drop --branch."
        )
    if args.strict:
        return (
            "--strict changes what fails a check, and --list checks nothing (it already "
            "lists every value the scan could not read); drop --strict."
        )
    if args.allow_empty:
        return "--list never fails because nothing matched; drop --allow-empty."
    gate = _gate_flag(args)
    if gate is not None:
        return (
            f"{gate} decides which findings fail a check, and --list checks nothing; drop {gate}."
        )
    if args.output_format not in (None, FORMAT_TEXT, FORMAT_JSON):
        chosen = args.output_format
        return f"--list prints text or JSON, not {chosen}; drop --format {chosen}."
    if args.reports:
        return (
            "--report writes a check's verdicts, and --list checks nothing; drop --report, "
            "or run tripl check without --list."
        )
    return None


def _discovery_config() -> CheckConfig:
    """No check config found: the whole repository, every supported file, no event types.

    Nothing can match, so what is left to show is the calls that look like
    tracking. Only from inside a repository: outside one the walk would cover a
    home directory or ``/``, and the first pass keeps every file it reads.
    Hidden directories are left out as well as the default excludes: what is in
    them (``.venv``, ``.gradle``, ``.next``, an editor's or agent's worktrees)
    is installed, generated or a copy, never the app's own tracking code.
    """
    cwd = Path.cwd()
    root = repository_root(cwd)
    if root is None:
        raise TriplConfigError(
            f"no check config found, and {cwd} is not in a git repository: run tripl check "
            "--list from a checkout, or pass --check-config PATH."
        )
    exclude = (*DEFAULT_EXCLUDES, DISCOVERY_EXCLUDE)
    return CheckConfig(path=None, root=root, sources=DEFAULT_SOURCES, exclude=exclude)


def _emit(text: str, stream: TextIO | None, *, primary: bool = True) -> None:
    """``_say`` and flush; a reader that went away ends that stream's output, not the run.

    ``tripl check --list | head`` closes the pipe early. Unhandled, that is
    exit 1 with a traceback (``cli.main`` catches only ``TriplError``), and the
    interpreter's own flush at exit fails again (exit 120). So the stream's
    descriptor is pointed at the null device, where anything more written to
    it, that last flush included, lands harmlessly. A descriptor closed before
    the run (``>&-``, ``2>&-``) leaves no stream at all (None): nothing to write.

    The human lines beside the JSON document (``primary`` false: stderr under
    ``--json``) end the same way on any failure to write them, a full disk
    included, so the document still reaches stdout. On the primary output any
    other failure is one.
    """
    if stream is None:
        return
    try:
        _say(text, stream)
        stream.flush()
    except BrokenPipeError:
        _silence(stream)
    except OSError:
        if primary:
            raise
        _silence(stream)


def _silence(stream: TextIO) -> None:
    try:
        descriptor = stream.fileno()
    except (OSError, ValueError):
        return  # not backed by a file (a test's stream), so no flush at exit reaches one
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, descriptor)
    finally:
        os.close(devnull)


def _gate_flag(args: argparse.Namespace) -> str | None:
    if args.new_from_rev is not None:
        return "--new-from-rev"
    if args.new_from_patch is not None:
        return "--new-from-patch"
    return None


def _gate_refusal(args: argparse.Namespace) -> str | None:
    """Why a gate flag cannot do what it says in this run; None when it can (or is not given)."""
    gate = _gate_flag(args)
    if gate is None:
        return None
    if args.payloads is not None:
        return (
            f"{gate} compares source with the change's base, and a captured event has no "
            f"source to compare; drop {gate}."
        )
    if args.new_from_rev is not None and args.new_from_rev.startswith("-"):
        return (
            f"--new-from-rev {args.new_from_rev!r}: a revision cannot start with '-'; "
            "name a commit, a branch or a tag."
        )
    return None


def _change(
    args: argparse.Namespace,
    check_config: CheckConfig,
    items: Sequence[CheckItem],
    skipped: Sequence[SkippedFile] = (),
) -> tuple[GateInfo, tuple[bool, ...]] | None:
    """The gate: what the change is, and which items it made new or changed. None for no gate.

    Everything is read here, before any request: git, the patch, the base scan.
    """
    if args.new_from_rev is not None:
        ref = args.new_from_rev.strip()
        if not ref:
            # One job line for merge requests and the default branch alike,
            # where the base variable is empty: no gate, and say so.
            _emit("--new-from-rev is empty: every finding counts.", sys.stderr, primary=False)
            return None
        untracked: tuple[str, ...]
        git = changes_mod.from_git(ref, check_config.root, runner=default_runner())
        root, sha, text, untracked = git.root, git.sha, git.diff, git.untracked
        info = GateInfo(source=GATE_REV, files=0, rev=ref, base=sha)
    else:
        spec = args.new_from_patch
        text = changes_mod.read_patch(spec)
        # Relative to the repository root, else to the working directory,
        # the way git apply reads a patch.
        root = repository_root(check_config.root) or Path.cwd()
        # A patch cannot say what git does not track: it must cover the change.
        untracked = ()
        info = GateInfo(source=GATE_PATCH, files=0, patch=spec)
    found = changes_mod.parse_unified_diff(text)
    if info.source == GATE_PATCH and text.strip() and not found:
        raise TriplConfigError(
            f"{info.patch} holds no diff ({changes_mod.GIT_FORMAT_ONLY} An empty file "
            "is a change with nothing in it.)"
        )
    marking = gate_mod.mark(
        check_config,
        items,
        found,
        root,
        trusted=info.source == GATE_REV,
        untracked=untracked,
        # What today's scan did not read: its calls at the base are not compared.
        unread=frozenset(entry.path for entry in skipped),
    )
    info = replace(
        info,
        files=marking.files,
        paths=marking.paths,
        unreconstructable=marking.unreconstructable,
        unmapped=marking.unmapped,
        untracked=marking.untracked,
    )
    return info, marking.changed


def default_runner() -> Runner:
    """The runner git goes through; a test replaces this binding, as install's does."""
    return subprocess_runner


def _check(args: argparse.Namespace, config: Config, targets: list[tuple[str, Path]]) -> int:
    output = _format(args)
    refusal = _gate_refusal(args)
    if refusal is not None:
        raise TriplConfigError(refusal)
    # Connection settings first: "no URL configured" is exit 2 before any file is read.
    context = begin(config)
    check_config = check_config_of(args)
    if args.payloads is None and check_config is None:
        # Before the project question: a missing config is the actual problem.
        raise TriplConfigError(
            "no check config found: create .tripl/check.yml (see `tripl check --help` "
            "and the CLI docs), pass --check-config PATH, or validate captured events "
            "with --payloads FILE."
        )
    project = project_of(args, check_config, "check")
    selector: str | None = args.branch
    if selector is None and check_config is not None:
        selector = check_config.branch
    files = 0
    skipped: tuple[SkippedFile, ...] = ()
    markers: tuple[Marker, ...] = ()
    items: tuple[CheckItem, ...]
    if args.payloads is not None:
        mode = MODE_PAYLOADS
        items = tuple(payloads_mod.load(args.payloads, allow_empty=args.allow_empty))
    else:
        assert check_config is not None  # raised above when missing
        mode = MODE_STATIC
        outcome = scan_mod.scan(check_config)
        items, files, skipped = outcome.items, outcome.files, outcome.skipped
        markers = outcome.markers
        if not items and not args.allow_empty:
            # A gate that checked nothing must not read as one that passed: a
            # renamed wrapper or a moved directory would otherwise turn CI green.
            raise TriplConfigError(_nothing_matched(check_config, files))
    gated = None
    if check_config is not None and _gate_flag(args) is not None:
        gated = _change(args, check_config, items, skipped)

    async def body(client: httpx.AsyncClient) -> tuple[Branch, tuple[CheckResult, ...], int]:
        nonlocal gated
        reader = context.reader(client)
        branch = await resolve_branch(reader, project, selector)
        results: tuple[CheckResult, ...] = ()
        if items:
            results = await validate(
                reader, project, items, branch_id=branch.id, strict=args.strict
            )
        if gated is not None:
            info, changed = gated
            causes: dict[int, tuple[bool, ...]] = {}
            if branch.id is not None:
                # A plan branch can fail a call the change left alone: ask main
                # about those, in this same run_gate, so an outage is still exit 4.
                picked = gate_mod.candidates(results, changed, args.strict)
                if picked:
                    on_main = await validate(
                        reader,
                        project,
                        [items[index] for index in picked],
                        branch_id=None,
                        strict=args.strict,
                    )
                    found = gate_mod.plan_branch_causes(
                        [results[index] for index in picked], on_main
                    )
                    causes = dict(zip(picked, found, strict=True))
                info = replace(
                    info,
                    plan_branch=PlanBranchComparison(
                        len(picked), sum(sum(caused) for caused in causes.values())
                    ),
                )
            results = gate_mod.apply_gate(results, changed, causes)
            gated = info, changed
        return branch, results, reader.requests

    branch, results, requests = run_gate(config, body, timeout=args.timeout)
    scan_root = check_config.root if check_config is not None else None
    cwd = Path.cwd()
    repo_root = repository_root(scan_root if mode == MODE_STATIC and scan_root else cwd)
    report = CheckReport(
        run=Run(
            instance=instance_of(config, context.base_url, "unknown"),
            generated_at=context.generated_at,
            duration_ms=int((time.monotonic() - context.started) * 1000),
            requests=requests,
        ),
        project=project,
        mode=mode,
        strict=args.strict,
        results=results,
        branch_id=branch.id,
        branch_name=branch.name,
        config_path=(
            str(check_config.path) if check_config is not None and check_config.path else None
        ),
        files_scanned=files,
        repo_paths=repo_paths(
            [
                *(result.item.origin.path for result in results),
                *(marker.path for marker in markers),
            ],
            mode=mode,
            scan_root=scan_root,
            repo_root=repo_root,
            cwd=cwd,
        ),
        skipped=skipped,
        markers=marker_states(markers, results),
        gate=None if gated is None else gated[0],
    )
    # Every file before anything is printed: a report that cannot be written
    # fails the run, and must not do so after stdout already told a success.
    for report_format, path in targets:
        _write(path, _render(report_format, report, scan_root))
    human = sys.stdout if output == FORMAT_TEXT else sys.stderr
    source = config.sources.get("base_url", "unknown")
    lines = [render_header("check", context.base_url, source), ""]
    if mode == MODE_STATIC and not items:
        lines.append(
            "No tracking calls matched the check config; check `sources:` and the call specs."
        )
    lines.append(render_text(report))
    _say("\n".join(lines), human)
    if output != FORMAT_TEXT:
        sys.stdout.write(_render(output, report, scan_root))
    return report.exit_code


def _nothing_matched(config: CheckConfig, files: int) -> str:
    return (
        f"{nothing_matched(config.root, files)}, so nothing was checked. Fix `sources:`, "
        "`exclude:` or the call specs (a renamed wrapper, a moved directory), or pass "
        "--allow-empty if there is no tracking code to check yet."
    )


def _render(report_format: str, report: CheckReport, root: Path | None) -> str:
    """One format of the finished run, as the text that goes to stdout or a file."""
    if report_format == FORMAT_TEXT:
        return render_text(report) + "\n"
    if report_format == FORMAT_JSON:
        return json.dumps(check_document(report)) + "\n"
    if report_format == FORMAT_SARIF:
        return json.dumps(sarif_document(report, root=root), indent=2) + "\n"
    if report_format == FORMAT_CODEQUALITY:
        return json.dumps(codequality_document(report), indent=2) + "\n"
    return junit_document(report)


def _say(text: str, stream: TextIO) -> None:
    """Print ``text``, escaping what ``stream`` cannot encode instead of failing on it.

    A Windows agent's pipe is cp1252, where a Cyrillic event name would crash
    the run after its reports were written, and a file name that is not UTF-8
    arrives from os.walk as lone surrogates, which no stream encodes. Both
    print as backslash escapes, the way stderr already prints them.
    """
    encoding = stream.encoding or "utf-8"
    print(text.encode(encoding, "backslashreplace").decode(encoding), file=stream)


def _write(path: Path, text: str) -> None:
    """Write through a temporary file, so a reader never sees half a report.

    The temporary file is always created afresh (``O_EXCL``), never opened
    through whatever already sits at its name, a link included, so what
    replaces PATH is a new regular file.
    """
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()  # a leftover, or a link: the link goes, not its target
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        # backslashreplace: a file name that is not UTF-8 reaches the text
        # report as lone surrogates; the machine formats are ASCII already.
        with open(
            os.open(temporary, flags, 0o666), "w", encoding="utf-8", errors="backslashreplace"
        ) as handle:
            handle.write(text)
        os.replace(temporary, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise TriplError(f"cannot write the report {path}: {exc}") from exc


def _discard(paths: Iterable[Path]) -> None:
    for path in paths:
        # A regular file only: never a link (what it points at is not this
        # run's), a directory or a device. Anything else, or no permission,
        # is left as it is; the error that ended the run is the one to report.
        with contextlib.suppress(OSError):
            if stat.S_ISREG(path.lstat().st_mode):
                path.unlink()
