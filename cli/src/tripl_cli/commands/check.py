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
  no connection and sends nothing, and the connection settings are not read at
  all, so a malformed one does not stop it; with no check config found it
  scans the whole repository it runs in (discovery).

The first two batch their items to ``POST /projects/{slug}/plan/validate`` — a
READ that happens to be a POST, so any project member's key works, ``tk_r_``
included, and there is no ``--dry-run`` because nothing is written.
``--print-preset [NAME]`` prints a preset's call specs as YAML to start a
custom one from, and exits the moment it is parsed, the way ``--version`` does.

One run, one set of verdicts, as many reports as a pipeline wants: ``--format``
picks what goes to stdout, and each ``--report FORMAT=PATH`` writes the same
result to a file — GitLab Code Quality and JUnit for a merge request, SARIF for
code scanning, the JSON document for scripts, Markdown for a merge request
note (``check/summary.py``: under a gate, the events the change adds, removes
or changes). A failed run writes none of them
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

``--baseline FILE`` fails the run only on findings the committed baseline
file does not hold (``check/baseline.py``): the one gate for ``--payloads``
and for a run with no diff base (the default branch, a schedule). With a gate
too, a finding fails only when it is new in the change AND not in the
baseline. ``--write-baseline FILE`` writes that file from this run's
findings, and ``--prune-baseline`` rewrites the ``--baseline`` file without
what this run no longer has, never when the run could not see everything. A
baseline that cannot be read, or was written for another project, mode or
key recipe, is exit 2 before any request.

``--coverage`` adds the opposite report to a static run: the planned events
no scanned call sends (``check/coverage.py``). It reads the event types and
the events of the plan the check ran against, in the same ``run_gate``, and
never changes the exit code.

Exit codes: 0 clean; 1 when any item has an error (or, with ``--strict``, a
warning — ``--strict`` also reports values only known at runtime) that no
``tripl-check-ignore`` marker suppresses (``check/suppress.py``) and that, under
a gate, is new in the change, and, with ``--strict-suppressions``, when a
``tripl-check-ignore`` marker is malformed or waived nothing
(``CheckReport.failing_markers``); 2 for a bad config, a bad payload file, a bad
invocation, a change that cannot be read (no git, an unknown REF, an
unreadable patch), or a config that matches no call
at all (``--allow-empty`` accepts that); 4 when the instance was unavailable —
unreachable, timed out, 429 or 5xx — so nothing was decided. An instance that
refuses the key is exit 1, with the reason on stderr. ``--list`` exits 0
whenever the scan ran, a reader that closed the pipe early included, and 2 for
a bad config or a flag it refuses (``--report``, ``--branch``, ``--strict``,
``--strict-suppressions``, ``--allow-empty``, and a ``--format`` other than text or JSON).

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
import uuid
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, TextIO

import httpx

from tripl_cli.api import check_runs as check_runs_api
from tripl_cli.api import event_types as event_types_api
from tripl_cli.api import events as events_api
from tripl_cli.check import baseline as baseline_mod
from tripl_cli.check import changes as changes_mod
from tripl_cli.check import coverage as coverage_mod
from tripl_cli.check import gate as gate_mod
from tripl_cli.check import payloads as payloads_mod
from tripl_cli.check import scan as scan_mod
from tripl_cli.check import summary as summary_mod
from tripl_cli.check import upload as upload_mod
from tripl_cli.check.config import DEFAULT_EXCLUDES, DEFAULT_SOURCES, CheckConfig, repository_root
from tripl_cli.check.inventory import inventory
from tripl_cli.check.lint import lint_items
from tripl_cli.check.listing import render_listing
from tripl_cli.check.model import (
    BASELINE_CHECK,
    BASELINE_PRUNE,
    BASELINE_WRITE,
    CODE_NO_VERDICT,
    GATE_PATCH,
    GATE_REV,
    MODE_PAYLOADS,
    MODE_STATIC,
    BaselineInfo,
    CheckItem,
    CheckReport,
    CheckResult,
    GateInfo,
    Marker,
    PlanBranchComparison,
    PlanPin,
    SkippedFile,
    UploadOutcome,
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
from tripl_cli.diagnostics.collect import Reader, instance_of
from tripl_cli.errors import TriplConfigError, TriplError
from tripl_cli.install.shell import Runner, subprocess_runner
from tripl_cli.model import JsonDict, JsonList, Run, as_list, page_items, page_total, to_rfc3339
from tripl_cli.render import plural, render_header
from tripl_cli.report import check_document, check_list_document
from tripl_cli.runner import run_async, run_gate
from tripl_cli.streams import silence, write_failure

FORMAT_TEXT = "text"
FORMAT_JSON = "json"
FORMAT_SARIF = "sarif"
FORMAT_CODEQUALITY = "codequality"
FORMAT_JUNIT = "junit"
FORMAT_MARKDOWN = "markdown"
FORMATS = (
    FORMAT_TEXT,
    FORMAT_JSON,
    FORMAT_SARIF,
    FORMAT_CODEQUALITY,
    FORMAT_JUNIT,
    FORMAT_MARKDOWN,
)
# `--revision live`: the live plan, whatever the check config pins.
REVISION_LIVE = "live"
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
    (``| true``) ends the output, and the exit is still 0; a stdout that cannot
    be written at all (``> /dev/full``) is one line and exit 1, which
    ``cli.main`` reports because it parses the arguments inside its own try.
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
            "tripl-check-ignore[CODE] -- REASON (CODE@RULE for one rule of a policy). "
            "With --new-from-rev or --new-from-patch, only findings new in that change "
            "fail the run; with --baseline, only findings the baseline file does not hold "
            "(write it with --write-baseline). "
            "Exits 1 on any error (or warning, with --strict) that is not suppressed (and, "
            "with a gate, is new in the change, and with a baseline, is not in it), and, "
            "with --strict-suppressions, on a tripl-check-ignore marker that is malformed "
            "or suppresses nothing; 4 when the instance was unavailable."
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
            "why a value is not read, the tripl-check-ignore markers on it, and the calls "
            "that look like tracking; needs no key and sends nothing"
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
    # Not argparse groups: a conflict is a refusal like any other, which
    # removes the --report files an earlier run left (``_baseline_refusal``).
    parser.add_argument(
        "--baseline",
        dest="baseline",
        metavar="FILE",
        help=(
            "fail only on findings the baseline FILE does not hold (written by "
            "--write-baseline and committed, e.g. .tripl/check-baseline.json); with "
            "--new-from-rev or --new-from-patch, only on findings new in the change and "
            "not in FILE"
        ),
    )
    parser.add_argument(
        "--write-baseline",
        dest="write_baseline",
        metavar="FILE",
        help=(
            "write every finding that fails this run to the baseline FILE (overwriting it), "
            "and exit 0 unless the run could not be checked"
        ),
    )
    parser.add_argument(
        "--prune-baseline",
        dest="prune_baseline",
        action="store_true",
        help=(
            "with --baseline FILE: rewrite FILE without the findings this run no longer has; "
            "never adds one, and leaves FILE alone when the run could not see everything"
        ),
    )
    add_project_flag(parser)
    add_branch(parser)
    parser.add_argument(
        "--revision",
        dest="revision",
        metavar="ID",
        help=(
            "validate against the stored plan revision ID instead of the live plan, so the "
            "verdicts do not change when the plan does (`tripl plan revisions` lists the ids); "
            f"overrides `revision` in the check config, and {REVISION_LIVE} unpins it for this run"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 on warnings too, and report values that are only known at runtime",
    )
    parser.add_argument(
        "--strict-suppressions",
        dest="strict_suppressions",
        action="store_true",
        help=(
            "exit 1 on a tripl-check-ignore marker that is malformed or that suppressed "
            "nothing in this run; with --new-from-rev or --new-from-patch, only in the "
            "files the change touches"
        ),
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=FORMATS,
        default=None,
        help=(
            "output format on stdout (default: text): json, sarif (SARIF 2.1.0, for code "
            "scanning), codequality (GitLab Code Quality), junit (JUnit XML) or markdown "
            "(a merge request note: the events the change adds, removes or changes)"
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
    parser.add_argument(
        "--coverage",
        dest="coverage",
        action="store_true",
        help=(
            "also list the planned events (status "
            f"{', '.join(coverage_mod.COVERED_STATUSES)}) that no scanned call sends; "
            "never changes the exit code"
        ),
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help=(
            "also record this run in tripl under its commit, ref, repository and pipeline, "
            "read from GitLab CI or GitHub Actions variables; a run on the default branch "
            "becomes what 'used in code' shows on each event, any other goes to the history; "
            "a failed upload is a warning and never changes the exit code"
        ),
    )
    parser.add_argument(
        "--default-branch",
        dest="default_branch",
        metavar="NAME",
        help=(
            "with --upload: the repository's default branch, instead of the one the CI "
            "names (CI_DEFAULT_BRANCH, or the GitHub event's repository)"
        ),
    )
    add_json(parser)
    add_timeout(parser)
    parser.set_defaults(handler=run, abandon=abandon, offline=sends_nothing)


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
    ``--report src/app.ts`` never touches a source file, and a report path that
    is also the baseline is left alone: the run refuses that pairing, and the
    committed baseline must not be what it removes.
    """
    kept = {
        Path(raw).resolve()
        for raw in (getattr(args, "baseline", None), getattr(args, "write_baseline", None))
        if raw
    }
    named = (spec.partition("=")[2] for spec in args.reports or ())
    _discard(
        Path(raw) for raw in named if raw and _names_a_file(raw) and Path(raw).resolve() not in kept
    )


def sends_nothing(args: argparse.Namespace) -> bool:
    """Whether this run talks to no instance, so ``cli.main`` reads no connection settings.

    True for ``--list`` alone. Every other mode validates against the plan, so
    it needs the URL and the key resolved, and is refused when they are malformed.
    """
    return bool(args.list)


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
    works with no connection settings at all, and with malformed ones:
    ``cli.main`` does not resolve them for this mode (``sends_nothing``).
    ``--timeout`` always has a value
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
    if args.strict_suppressions:
        return (
            "--strict-suppressions fails a check on its markers, and --list checks nothing; "
            "drop --strict-suppressions."
        )
    if args.allow_empty:
        return "--list never fails because nothing matched; drop --allow-empty."
    if args.revision is not None:
        return (
            "--revision names the plan to check against, and --list sends nothing; drop --revision."
        )
    if args.coverage:
        return (
            "--coverage compares the scan with the plan, and --list sends nothing; "
            "run tripl check --coverage without --list."
        )
    if args.upload or args.default_branch is not None:
        flag = "--upload" if args.upload else "--default-branch"
        return f"{flag} records a check's verdicts, and --list checks nothing; drop {flag}."
    gate = _gate_flag(args) or _baseline_flag(args)
    if gate == "--write-baseline":
        return "--write-baseline records a check's findings, and --list checks nothing; drop it."
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
    included, so the document still reaches stdout. On the primary output,
    which is always stdout, any other failure (``> /dev/full``) fails the run
    with one line and exit 1: what was asked for did not get out.
    """
    if stream is None:
        return
    with _writing(stream, primary=primary):
        _say(text, stream)


@contextlib.contextmanager
def _writing(stream: TextIO, *, primary: bool = True) -> Iterator[None]:
    """Write to ``stream`` in the block and flush it, on ``_emit``'s terms.

    The flush is here so a failure surfaces inside the run, where it can still
    fail the run (and remove its reports), rather than in the interpreter's
    flush at exit.
    """
    try:
        yield
        stream.flush()
    except BrokenPipeError:
        silence(stream)
    except OSError as exc:
        # Silenced either way, so the text still in the buffer cannot fail
        # again when the interpreter flushes at exit.
        silence(stream)
        if primary:
            raise TriplError(write_failure(exc)) from exc


def _gate_flag(args: argparse.Namespace) -> str | None:
    if args.new_from_rev is not None:
        return "--new-from-rev"
    if args.new_from_patch is not None:
        return "--new-from-patch"
    return None


def _baseline_flag(args: argparse.Namespace) -> str | None:
    if args.baseline is not None:
        return "--baseline"
    if args.write_baseline is not None:
        return "--write-baseline"
    if args.prune_baseline:
        return "--prune-baseline"
    return None


def _baseline_refusal(args: argparse.Namespace, targets: Sequence[tuple[str, Path]]) -> str | None:
    """Why the baseline flags cannot do what they say together; None when they can.

    Only the flags and the path to write are looked at here; the file
    ``--baseline`` reads is checked once the project is known (``baseline.load``).
    """
    write: str | None = args.write_baseline
    if write is not None and args.baseline is not None:
        return (
            "--write-baseline writes the baseline --baseline reads; pick one "
            "(--baseline FILE --prune-baseline updates FILE)."
        )
    if write is not None and args.prune_baseline:
        return "--write-baseline writes the whole file afresh; drop --prune-baseline."
    if args.prune_baseline and args.baseline is None:
        return "--prune-baseline rewrites the --baseline file; pass --baseline FILE."
    gate = _gate_flag(args)
    if write is not None and gate is not None:
        return (
            f"--write-baseline records every finding of the run, and {gate} only decides "
            f"which of them fail it; drop {gate}."
        )
    flag, path = ("--write-baseline", write) if write is not None else ("--baseline", args.baseline)
    if path is None:
        return None
    # Read or written, the baseline is never a report path too: a report
    # written over it, or removed by a run that fails, would take it with it.
    if any(Path(path).resolve() == report.resolve() for _, report in targets):
        return f"{flag} {path} is also a --report path; each needs its own file."
    if write is None and not args.prune_baseline:
        return None
    if not _names_a_file(path):
        return f"{flag} {path!r}: give it the path of a file."
    target = Path(path)
    if os.path.lexists(target) and (target.is_symlink() or not target.is_file()):
        # Written through a temporary file and a rename, as a report is: a
        # link would be replaced, not written through.
        return f"{flag} {path}: a directory, a link or a special file; give it a regular file."
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
) -> tuple[GateInfo, tuple[bool, ...], tuple[CheckItem, ...] | None] | None:
    """The gate: what the change is, which items it made new or changed, and the base's items.

    None for no gate; the base's items are None when the base could not be rebuilt.

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
        whole=marking.whole,
    )
    return info, marking.changed, marking.base


def default_runner() -> Runner:
    """The runner git goes through; a test replaces this binding, as install's does."""
    return subprocess_runner


def _check(args: argparse.Namespace, config: Config, targets: list[tuple[str, Path]]) -> int:
    output = _format(args)
    refusal = (
        _gate_refusal(args)
        or _baseline_refusal(args, targets)
        or _coverage_refusal(args)
        or _upload_refusal(args)
        or _suppressions_refusal(args)
    )
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
    mode = MODE_STATIC if args.payloads is None else MODE_PAYLOADS
    # Before the scan and any request: a baseline of another project, mode or
    # recipe must stop the run, never read as one that holds nothing.
    baseline = (
        None
        if args.baseline is None
        else baseline_mod.load(args.baseline, project=project, mode=mode)
    )
    selector: str | None = args.branch
    if selector is None and check_config is not None:
        selector = check_config.branch
    pin = _pinned_revision(args, check_config, selector)
    if pin is not None and args.coverage:
        raise TriplConfigError(
            "--coverage reads the events of the live plan, and this run is pinned to plan "
            f"revision {pin}; run it with --revision {REVISION_LIVE}."
        )
    # Before the scan and any request: an upload that cannot happen stops the run.
    ci = _upload_context(args, selector, pin)
    answered: list[JsonDict] = []
    files = 0
    skipped: tuple[SkippedFile, ...] = ()
    markers: tuple[Marker, ...] = ()
    items: tuple[CheckItem, ...]
    if args.payloads is not None:
        items = tuple(payloads_mod.load(args.payloads, allow_empty=args.allow_empty))
        if check_config is not None:
            # The captured names and keys get the same lint as a static scan's.
            items = tuple(lint_items(items, check_config.lint))
    else:
        assert check_config is not None  # raised above when missing
        outcome = scan_mod.scan(check_config)
        items, files, skipped = outcome.items, outcome.files, outcome.skipped
        markers = outcome.markers
        if not items and not args.allow_empty:
            # A gate that checked nothing must not read as one that passed: a
            # renamed wrapper or a moved directory would otherwise turn CI green.
            raise TriplConfigError(_nothing_matched(check_config, files))
    gated: tuple[GateInfo, tuple[bool, ...]] | None = None
    base_items: tuple[CheckItem, ...] | None = None
    if check_config is not None and _gate_flag(args) is not None:
        change = _change(args, check_config, items, skipped)
        if change is not None:
            gated, base_items = change[:2], change[2]
    summarised = FORMAT_MARKDOWN in (output, *(report_format for report_format, _ in targets))
    compared: summary_mod.ChangeSummary | None = None

    covered: coverage_mod.Coverage | None = None

    async def body(client: httpx.AsyncClient) -> tuple[Branch, tuple[CheckResult, ...], int]:
        nonlocal gated, covered, compared
        reader = context.reader(client)
        branch = await resolve_branch(reader, project, selector)
        results: tuple[CheckResult, ...] = ()
        if items:
            results = await validate(
                reader,
                project,
                items,
                branch_id=branch.id,
                strict=args.strict,
                revision=pin,
                answered=answered,
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
        if summarised and gated is not None and base_items is not None:
            compared = summary_mod.compare(
                await _base_results(reader, project, branch, pin, base_items, items, results),
                results,
            )
        if args.coverage:
            assert check_config is not None  # --coverage is refused with --payloads
            covered = await _coverage(reader, project, branch, check_config, results)
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
        coverage=covered,
        changes=compared,
        plan=_plan_pin(pin, answered),
        # --write-baseline records the findings and exits 0 unless a call got no
        # verdict; a baseline cannot hold a marker, so a marker must not fail it.
        strict_suppressions=args.strict_suppressions and args.write_baseline is None,
    )
    report, written = _with_baseline(args, report, baseline)
    uploaded: str | None = None
    if ci is not None:
        # Before the reports, so the JSON document can say what it did; its
        # stderr line waits for the end, after the run's own output.
        recorded, uploaded = _upload(
            config,
            context.reader,
            project,
            report,
            ci,
            check_root=upload_mod.check_directory(
                scan_root, repo_root, check_config.path if check_config is not None else None
            ),
            timeout=args.timeout,
        )
        report = replace(report, upload=recorded)
    # Every file before anything is printed: a report that cannot be written
    # fails the run, and must not do so after stdout already told a success.
    # The baseline last, so a run that fails writing a report leaves it alone.
    try:
        for report_format, path in targets:
            _write(path, _render(report_format, report, scan_root))
        if written is not None:
            _write(Path(written[0]), written[1], what="baseline")
    except TriplError:
        # tripl has the run already: say so before the error that ends it.
        if uploaded is not None:
            _emit(uploaded, sys.stderr, primary=False)
        raise
    human = sys.stdout if output == FORMAT_TEXT else sys.stderr
    source = config.sources.get("base_url", "unknown")
    lines = [render_header("check", context.base_url, source), ""]
    if mode == MODE_STATIC and not items:
        lines.append(
            "No tracking calls matched the check config; check `sources:` and the call specs."
        )
    lines.append(render_text(report))
    # Through _emit like --list: under --json/--format the human lines are the
    # SECONDARY stream, and a closed or full stderr (2>&-, 2>/dev/full) must not
    # end the run before the document reaches stdout.
    _emit("\n".join(lines), human, primary=output == FORMAT_TEXT)
    if output != FORMAT_TEXT and sys.stdout is not None:
        # Flushed here, not at exit: a document that cannot be written fails
        # the run and removes its reports, as the text summary does.
        try:
            with _writing(sys.stdout):
                sys.stdout.write(_render(output, report, scan_root))
        except TriplError:
            # tripl has the run already: say so before the error that ends it.
            if uploaded is not None:
                _emit(uploaded, sys.stderr, primary=False)
            raise
    if uploaded is not None:
        _emit(uploaded, sys.stderr, primary=False)
    return report.exit_code


def _upload_refusal(args: argparse.Namespace) -> str | None:
    """Why ``--upload`` or ``--default-branch`` cannot do what it says; None when it can."""
    if args.default_branch is not None and not args.upload:
        return (
            "--default-branch says which runs --upload records as the default branch's, "
            "and there is no --upload; pass --upload, or drop --default-branch."
        )
    if args.upload and args.payloads is not None:
        return (
            "--upload records where the code sends each event, and captured events are not "
            "in the code; drop --upload."
        )
    return None


def _upload_context(
    args: argparse.Namespace, branch: str | None, pin: str | None
) -> upload_mod.CiContext | None:
    """The CI's account of this run when it is to be uploaded; None without ``--upload``.

    Refused for a run against anything but the live plan: a plan branch or a
    pinned revision, from a flag or from the committed check config. What the
    run records is where the code sends each event of the plan analysts see,
    and the event ids of another plan are not that.
    """
    if not args.upload:
        return None
    if branch is not None:
        _refuse_upload(
            f"this run checks plan branch {branch!r} (--branch, or `branch` in the check "
            "config); drop the branch for the run you upload"
        )
    if pin is not None:
        _refuse_upload(
            f"this run is pinned to plan revision {pin} (--revision, or `revision` in the "
            f"check config); run it with --revision {REVISION_LIVE}"
        )
    return upload_mod.ci_context(os.environ, default_branch=args.default_branch)


def _refuse_upload(why: str) -> NoReturn:
    raise TriplConfigError(f"--upload records a run against the live plan, and {why}.")


def _upload(
    config: Config,
    reader_of: Callable[[httpx.AsyncClient], Reader],
    project: str,
    report: CheckReport,
    ci: upload_mod.CiContext,
    *,
    check_root: str,
    timeout: float,
) -> tuple[UploadOutcome, str]:
    """Send the finished run: what it recorded, or why it did not, and the stderr line saying so.

    The verdicts are this run's whatever the upload does, so a failed upload
    is a warning (and ``ok: false`` in the JSON document) and the exit code
    stays the check's.
    """
    body = upload_mod.run_body(report, ci, check_root)
    calls = len(body["calls"])
    if calls > check_runs_api.MAX_CALLS:
        reason = (
            f"the run has {calls} calls, and tripl records at most "
            f"{check_runs_api.MAX_CALLS} per run"
        )
        return _not_uploaded(ci, check_root, calls, reason)

    async def send(client: httpx.AsyncClient) -> object:
        return await reader_of(client).send(check_runs_api.upload(project, body))

    # Not ``run_gate``: its outage message says nothing was checked (exit 4),
    # and here the check has run and its exit code stands.
    try:
        answer = run_async(config, send, timeout=timeout)
    except TriplError as exc:
        return _not_uploaded(ci, check_root, calls, str(exc).rstrip("."))
    recorded = upload_mod.outcome(ci, check_root, calls, answer=answer)
    line = upload_mod.uploaded_line(ci, calls, check_root=check_root, current=recorded.current)
    return recorded, line


def _not_uploaded(
    ci: upload_mod.CiContext, check_root: str, calls: int, reason: str
) -> tuple[UploadOutcome, str]:
    return (
        upload_mod.outcome(ci, check_root, calls, error=reason),
        f"Not uploaded: {reason}. The check result stands.",
    )


def _pinned_revision(
    args: argparse.Namespace, check_config: CheckConfig | None, branch: str | None
) -> str | None:
    """The plan revision this run is pinned to, or None for the live plan.

    ``--revision`` wins over ``revision:`` in the check config, and
    ``--revision live`` unpins. Refused with a plan branch, from either place:
    a revision is one fixed plan, a branch a plan that moves.
    """
    flag: str | None = args.revision
    if flag is not None and flag.strip() == REVISION_LIVE:
        return None
    source = "--revision"
    raw = flag
    if raw is None and check_config is not None and check_config.revision is not None:
        raw, source = check_config.revision, "`revision` in the check config"
    if raw is None:
        return None
    try:
        pin = str(uuid.UUID(raw.strip()))
    except ValueError:
        raise TriplConfigError(
            f"{source} is not a plan revision id: {raw!r}. Use the id of a revision of the "
            f"project (`tripl plan revisions` and its History page list them), or "
            f"{REVISION_LIVE} for the live plan."
        ) from None
    if branch is not None:
        raise TriplConfigError(
            f"{source} pins plan revision {pin}, and plan branch {branch!r} is asked for too; "
            f"a run checks against one plan. Drop the branch, or pass --revision {REVISION_LIVE}."
        )
    return pin


def _plan_pin(pin: str | None, answered: Sequence[JsonDict]) -> PlanPin | None:
    if pin is None:
        return None
    plan = answered[0] if answered else {}
    plan_hash, created_at = plan.get("plan_hash"), plan.get("created_at")
    return PlanPin(
        revision=pin,
        plan_hash=plan_hash if isinstance(plan_hash, str) else None,
        created_at=created_at if isinstance(created_at, str) else None,
    )


async def _base_results(
    reader: Reader,
    project: str,
    branch: Branch,
    pin: str | None,
    base: Sequence[CheckItem],
    items: Sequence[CheckItem],
    results: Sequence[CheckResult],
) -> tuple[CheckResult, ...]:
    """The base tree's items with their verdicts, asking only about what this tree lacks.

    An item this tree still sends has its verdict already; the others (what the
    change removed or edited) are validated against the same plan, in the same
    ``run_gate``, so the summary can name a removed event by its identity.
    """

    def sent(item: CheckItem) -> str:
        return json.dumps(item.wire(), sort_keys=True, default=str)

    known = {sent(result.item): result for result in results}
    gone = [item for item in base if sent(item) not in known]
    asked = iter(
        await validate(reader, project, gone, branch_id=branch.id, strict=False, revision=pin)
        if gone
        else ()
    )
    return tuple(
        replace(known[sent(item)], item=item) if sent(item) in known else next(asked)
        for item in base
    )


def _coverage_refusal(args: argparse.Namespace) -> str | None:
    if args.coverage and args.payloads is not None:
        return (
            "--coverage lists the planned events no call in the source sends, and a capture "
            "holds only the events one session happened to send; drop --coverage."
        )
    return None


async def _planned_events(reader: Reader, project: str, branch: Branch) -> JsonList:
    """Every event in a covered status, page by page until the route's own total."""
    rows: JsonList = []
    while True:
        payload = await reader.send(
            events_api.list_events(
                project,
                status=list(coverage_mod.COVERED_STATUSES),
                offset=len(rows),
                limit=events_api.LIMIT_MAX,
                branch=branch.id,
            )
        )
        page = page_items(payload)
        rows.extend(page)
        total = page_total(payload)
        if not page or total is None or len(rows) >= total:
            return rows


async def _coverage(
    reader: Reader,
    project: str,
    branch: Branch,
    check_config: CheckConfig,
    results: Sequence[CheckResult],
) -> coverage_mod.Coverage:
    types = as_list(await reader.send(event_types_api.list_event_types(project, branch=branch.id)))
    type_names = {str(row.get("id")): str(row.get("name") or "") for row in types}
    events = await _planned_events(reader, project, branch)
    scanned = (spec.name for spec in check_config.event_types if spec.calls)
    return coverage_mod.build(events, type_names, scanned, results)


def _suppressions_refusal(args: argparse.Namespace) -> str | None:
    if args.strict_suppressions and args.payloads is not None:
        return (
            "--strict-suppressions fails a check on the tripl-check-ignore markers in the "
            "source, and a captured event has no source to hold one; drop "
            "--strict-suppressions."
        )
    return None


def _with_baseline(
    args: argparse.Namespace, report: CheckReport, baseline: baseline_mod.Baseline | None
) -> tuple[CheckReport, tuple[str, str] | None]:
    """``report`` matched against its baseline, and the baseline file to write, if any.

    ``--write-baseline`` matches the run against the baseline it writes, so
    it exits 0 unless a call got no verdict. ``--prune-baseline`` writes only
    when the file changes, and never after a run that did not see
    everything: what it did not see would be dropped.
    """
    if args.write_baseline is not None:
        entries = baseline_mod.collect(report)
        matching = baseline_mod.match(
            report.results, entries, presence=report.mode == MODE_PAYLOADS
        )
        info = BaselineInfo(
            path=args.write_baseline,
            action=BASELINE_WRITE,
            entries=len(entries),
            findings=sum(entry.count for entry in entries),
        )
        text = baseline_mod.render_file(entries, project=report.project, mode=report.mode)
        return replace(report, results=matching.results, baseline=info), (args.write_baseline, text)
    if baseline is None:
        return report, None
    matching = baseline_mod.match(
        report.results, baseline.entries, presence=report.mode == MODE_PAYLOADS
    )
    info = BaselineInfo(
        path=baseline.path,
        action=BASELINE_PRUNE if args.prune_baseline else BASELINE_CHECK,
        entries=len(baseline.entries),
        findings=baseline.findings,
        stale=matching.stale(baseline.entries),
    )
    written: tuple[str, str] | None = None  # unless the prune changes the file
    if args.prune_baseline:
        unseen = _unseen(report)
        if unseen is not None:
            info = replace(info, not_pruned=unseen)
        else:
            kept = baseline_mod.pruned(baseline.entries, matching)
            info = replace(info, pruned=baseline.findings - sum(entry.count for entry in kept))
            if kept != tuple(sorted(baseline.entries, key=baseline_mod.entry_order)):
                text = baseline_mod.render_file(kept, project=report.project, mode=report.mode)
                written = (baseline.path, text)
    return replace(report, results=matching.results, baseline=info), written


def _unseen(report: CheckReport) -> str | None:
    """Why this run cannot prune a baseline: what it could not see; None when it saw everything."""
    if not report.results:
        return "nothing was checked"
    if report.skipped:
        files = len(report.skipped)
        return (
            f"{plural(files, 'configured file')} {'was' if files == 1 else 'were'} not read, "
            "so the findings there are unknown"
        )
    blank = sum(
        1
        for result in report.results
        if any(finding.code == CODE_NO_VERDICT for finding in result.findings)
    )
    if blank:
        return (
            f"{plural(blank, 'item')} got no verdict, so {'its' if blank == 1 else 'their'} "
            "findings are unknown"
        )
    return None


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
    if report_format == FORMAT_MARKDOWN:
        return summary_mod.markdown_document(report)
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


def _write(path: Path, text: str, *, what: str = "report") -> None:
    """Write through a temporary file, so a reader never sees half a report (or baseline).

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
        raise TriplError(f"cannot write the {what} {path}: {exc}") from exc


def _discard(paths: Iterable[Path]) -> None:
    for path in paths:
        # A regular file only: never a link (what it points at is not this
        # run's), a directory or a device. Anything else, or no permission,
        # is left as it is; the error that ended the run is the one to report.
        with contextlib.suppress(OSError):
            if stat.S_ISREG(path.lstat().st_mode):
                path.unlink()
