"""What happens when an output stream cannot be written.

Python block-buffers a stdout that is not a terminal, so a write that fails
(a full disk, a reader that closed the pipe) often surfaces only when the
buffer is flushed. Left to the interpreter, that flush happens at exit, after
``main`` has returned: it prints "Exception ignored ... flushing sys.stdout"
and replaces the exit code with 120, whatever the command decided.

So the flush is done here, while the outcome can still be reported properly:
a reader that left is not a failure, anything else is one line on stderr and
exit 1. Either way the stream's descriptor is then pointed at the null device,
so whatever is still buffered, and the interpreter's own last flush, land
harmlessly.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO


def silence(stream: TextIO) -> None:
    """Point ``stream``'s descriptor at the null device: later writes and flushes succeed."""
    try:
        descriptor = stream.fileno()
    except (OSError, ValueError):
        return  # not backed by a file (a test's stream), so no flush at exit reaches one
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, descriptor)
    finally:
        os.close(devnull)


def write_failure(exc: OSError) -> str:
    """The one-line message for a stdout that could not be written."""
    return f"cannot write to stdout: {exc.strerror or exc}"


def finish_stdout() -> str | None:
    """Flush stdout now; return a message when what was printed did not get out.

    None means there is nothing to report: the flush worked, there is no stdout
    (``>&-``), it was already closed, or the reader went away, which ends the
    output and not the run. Any other failure silences the stream and returns
    the message for the caller to print and turn into an exit code.
    """
    stream = sys.stdout
    if stream is None:
        return None
    try:
        stream.flush()
    except BrokenPipeError:
        silence(stream)
        return None
    except OSError as exc:
        silence(stream)
        return write_failure(exc)
    except ValueError:
        return None  # closed by the command itself: the interpreter skips it at exit too
    return None
