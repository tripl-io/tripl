"""A stdout that cannot be written: one line and exit 1, never a traceback or exit 120.

Python block-buffers a stdout that is not a terminal, so most write failures
surface only when the buffer is flushed. Left to the interpreter's flush at
exit, that prints "Exception ignored ... flushing sys.stdout" and turns any
exit code into 120. ``main`` flushes first: after a result, an expected failure, Ctrl-C
or the parser's own exit.
"""

from __future__ import annotations

import argparse
import errno
import io
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tripl_cli import cli as cli_module
from tripl_cli.cli import EXIT_INTERRUPTED, main
from tripl_cli.config import Config
from tripl_cli.errors import EXIT_CHECKS_FAILED, EXIT_FAILURE, EXIT_OK, TriplError
from tripl_cli.streams import finish_stdout

FULL = "tripl: cannot write to stdout: No space left on device\n"

needs_dev_full = pytest.mark.skipif(
    not os.path.exists("/dev/full"), reason="needs a device that is always full"
)
needs_posix_pipe = pytest.mark.skipif(
    sys.platform == "win32", reason="a closed POSIX pipe raises EPIPE"
)


# The fixtures hand the test a switch rather than swapping sys.stdout during
# setup: pytest's own capture puts its stream back before the test body runs.
Redirect = Callable[[], None]


@pytest.fixture
def full_stdout(monkeypatch: pytest.MonkeyPatch) -> Iterator[Redirect]:
    """``> /dev/full``: every flush of what was written fails with ENOSPC."""
    stream = open("/dev/full", "w", encoding="utf-8")  # noqa: SIM115 - closed below
    yield lambda: monkeypatch.setattr(sys, "stdout", stream)
    # What the interpreter does at exit: on a descriptor still pointing at the
    # full device, anything left in the buffer would fail here (exit 120).
    stream.flush()
    stream.close()


@pytest.fixture
def left_stdout(monkeypatch: pytest.MonkeyPatch) -> Iterator[Redirect]:
    """``| true``: the reader closed the pipe before reading a line."""
    reader, writer = os.pipe()
    os.close(reader)
    stream = open(writer, "w", encoding="utf-8")  # noqa: SIM115 - closed below
    yield lambda: monkeypatch.setattr(sys, "stdout", stream)
    stream.flush()
    stream.close()


Install = Callable[[Callable[[], int]], None]


@pytest.fixture
def command(monkeypatch: pytest.MonkeyPatch) -> Install:
    """Register ``tripl dummy``, which prints a line and then runs ``finish``."""

    def install(finish: Callable[[], int]) -> None:
        def handler(_: argparse.Namespace, __: Config) -> int:
            print("some output")
            return finish()

        def register_all(
            subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
            parent: argparse.ArgumentParser,
        ) -> None:
            sub = subparsers.add_parser("dummy", parents=[parent])
            sub.set_defaults(handler=handler)

        monkeypatch.setattr(cli_module, "register_all", register_all)

    return install


# --- the parser's own output: --version and --help -------------------------------------
@needs_dev_full
@pytest.mark.parametrize("argv", [["--version"], ["--help"], ["check", "--help"]])
def test_version_and_help_on_a_full_disk_are_one_line_and_exit_1(
    argv: list[str], full_stdout: Redirect, capsys: pytest.CaptureFixture[str]
) -> None:
    full_stdout()
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == EXIT_FAILURE
    assert capsys.readouterr().err == FULL


@needs_posix_pipe
@pytest.mark.parametrize("argv", [["--version"], ["--help"], ["check", "--help"]])
def test_version_and_help_to_a_reader_that_left_still_exit_0(
    argv: list[str], left_stdout: Redirect, capsys: pytest.CaptureFixture[str]
) -> None:
    left_stdout()
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == EXIT_OK
    assert capsys.readouterr().err == ""


@needs_dev_full
def test_a_usage_error_keeps_exit_2_when_stdout_is_full(
    full_stdout: Redirect, capsys: pytest.CaptureFixture[str]
) -> None:
    full_stdout()
    # Nothing was printed to stdout, so there is nothing to report about it.
    with pytest.raises(SystemExit) as excinfo:
        main(["nope"])
    assert excinfo.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


# --- what a command printed, flushed before its exit code is final -----------------------
@needs_dev_full
def test_a_command_that_succeeded_but_could_not_print_exits_1(
    command: Install, full_stdout: Redirect, capsys: pytest.CaptureFixture[str]
) -> None:
    full_stdout()
    command(lambda: EXIT_OK)
    assert main(["dummy"]) == EXIT_FAILURE
    assert capsys.readouterr().err == FULL


@needs_dev_full
def test_a_command_that_already_failed_keeps_its_own_exit_code(
    command: Install, full_stdout: Redirect, capsys: pytest.CaptureFixture[str]
) -> None:
    full_stdout()
    command(lambda: EXIT_CHECKS_FAILED)
    assert main(["dummy"]) == EXIT_CHECKS_FAILED
    assert capsys.readouterr().err == FULL


@needs_dev_full
def test_an_expected_failure_keeps_its_exit_code_and_message(
    command: Install, full_stdout: Redirect, capsys: pytest.CaptureFixture[str]
) -> None:
    full_stdout()

    def fail() -> int:
        raise TriplError("the instance said no")

    command(fail)
    assert main(["dummy"]) == EXIT_FAILURE
    assert capsys.readouterr().err == "tripl: the instance said no\n" + FULL


@needs_dev_full
def test_an_interrupted_command_keeps_exit_130(
    command: Install, full_stdout: Redirect, capsys: pytest.CaptureFixture[str]
) -> None:
    full_stdout()

    def interrupt() -> int:
        raise KeyboardInterrupt

    command(interrupt)
    assert main(["dummy"]) == EXIT_INTERRUPTED
    assert capsys.readouterr().err == "\n" + FULL


@needs_posix_pipe
def test_a_command_whose_reader_left_still_exits_0(
    command: Install, left_stdout: Redirect, capsys: pytest.CaptureFixture[str]
) -> None:
    left_stdout()
    command(lambda: EXIT_OK)
    assert main(["dummy"]) == EXIT_OK
    assert capsys.readouterr().err == ""


@needs_dev_full
def test_no_stderr_left_still_leaves_the_exit_code_to_say_it(
    command: Install, full_stdout: Redirect, monkeypatch: pytest.MonkeyPatch
) -> None:
    full_stdout()
    command(lambda: EXIT_OK)
    monkeypatch.setattr(sys, "stderr", None)  # `2>&-`
    assert main(["dummy"]) == EXIT_FAILURE


# --- a stderr that cannot be written either -------------------------------------------
@pytest.fixture
def full_stderr(monkeypatch: pytest.MonkeyPatch) -> Iterator[Redirect]:
    """``2> /dev/full``, line-buffered like the real stderr, so a print fails at once."""
    stream = open("/dev/full", "w", buffering=1, encoding="utf-8")  # noqa: SIM115
    yield lambda: monkeypatch.setattr(sys, "stderr", stream)
    stream.flush()  # the interpreter's flush at exit: it fails unless stderr was silenced
    stream.close()


@needs_dev_full
def test_a_full_stderr_leaves_the_exit_code_to_report_a_full_stdout(
    command: Install, full_stdout: Redirect, full_stderr: Redirect
) -> None:
    full_stdout()
    full_stderr()
    command(lambda: EXIT_OK)
    assert main(["dummy"]) == EXIT_FAILURE


@needs_dev_full
def test_a_full_stderr_keeps_an_expected_failures_exit_code(
    command: Install, full_stderr: Redirect
) -> None:
    full_stderr()

    def fail() -> int:
        raise TriplError("the instance said no")

    command(fail)
    assert main(["dummy"]) == EXIT_FAILURE


@needs_dev_full
def test_a_full_stderr_keeps_exit_130(command: Install, full_stderr: Redirect) -> None:
    full_stderr()

    def interrupt() -> int:
        raise KeyboardInterrupt

    command(interrupt)
    assert main(["dummy"]) == EXIT_INTERRUPTED


@needs_dev_full
def test_a_full_stderr_keeps_a_usage_errors_exit_2(full_stderr: Redirect) -> None:
    full_stderr()
    with pytest.raises(SystemExit) as excinfo:
        main(["nope"])
    assert excinfo.value.code == 2


# The whole process, because exit 120 is decided by the interpreter's own flush
# of stdout and stderr after main has returned, which no in-process test sees.
@needs_dev_full
@pytest.mark.parametrize(
    ("argv", "stderr_too", "expected"),
    [
        (["--version"], "2>&1", EXIT_FAILURE),
        (["--version"], "2>/dev/full", EXIT_FAILURE),
        (["check", "--print-preset"], "2>&1", EXIT_FAILURE),
        (["check", "--print-preset"], "2>/dev/full", EXIT_FAILURE),
        (["nope"], "2>/dev/full", 2),
    ],
)
def test_the_process_exit_code_survives_a_full_stdout_and_stderr(
    argv: list[str], stderr_too: str, expected: int, tmp_path: Path
) -> None:
    with open("/dev/full", "w") as full:
        stderr = subprocess.STDOUT if stderr_too == "2>&1" else full
        done = subprocess.run(
            [sys.executable, "-m", "tripl_cli", *argv],
            stdout=full,
            stderr=stderr,
            cwd=tmp_path,
            check=False,
        )
    assert done.returncode == expected


# --- finish_stdout itself --------------------------------------------------------------
class _Failing(io.StringIO):
    def __init__(self, error: OSError) -> None:
        super().__init__()
        self.error = error

    def flush(self) -> None:
        raise self.error


def test_finish_stdout_is_quiet_when_the_flush_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert finish_stdout() is None


def test_finish_stdout_is_quiet_with_no_stdout_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)  # `>&-`
    assert finish_stdout() is None


def test_finish_stdout_is_quiet_on_a_stdout_the_command_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stdout", closed)
    assert finish_stdout() is None


def test_finish_stdout_is_quiet_when_the_reader_left(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", _Failing(BrokenPipeError(errno.EPIPE, "Broken pipe")))
    assert finish_stdout() is None


def test_finish_stdout_names_any_other_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    error = OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(sys, "stdout", _Failing(error))
    assert finish_stdout() == "cannot write to stdout: No space left on device"
