"""Typed failures and process exit codes.

A leaf module on purpose: ``config`` and ``client`` both raise from here and
``cli`` catches, so keeping the exceptions out of either one leaves the import
graph acyclic.

It is also what keeps ``mcp`` out of this distribution. The client used to raise
``mcp.server.fastmcp.exceptions.ToolError`` directly; importing that here would
invert the dependency (tripl-mcp depends on tripl, not the other way round) and
drag the whole MCP SDK into every ``uvx tripl`` install. tripl-mcp re-attaches
its own agent-facing wording at its tool boundary instead — see
``tripl_mcp/errors.py``.
"""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
# The operation ran and failed: the API refused, or the instance is unreachable.
EXIT_FAILURE = 1
# Bad invocation or unusable configuration. 2 because that is argparse's own
# code for a parse error, and a caller should not have to tell the two apart.
EXIT_USAGE = 2
# "The tool worked, the checks did not": `tripl doctor` ran to completion and at
# least one check reported `fail` (or, under --strict, `warn`). Distinct from
# EXIT_FAILURE so a cron can tell "your instance has a problem" from "the tool
# itself broke" — doctor turns every API failure into a finding, so an exit 1
# out of doctor is a bug report, not a diagnosis.
EXIT_CHECKS_FAILED = 3
# 4+ stays unallocated. A per-severity code or a --fail-on=warn|fail was
# considered and dropped: one code plus --strict covers the cron contract, and
# every extra code is a promise to every consumer forever.


class TriplError(Exception):
    """Base for every failure the CLI reports as a message, not a traceback."""

    exit_code: int = EXIT_FAILURE


class TriplConfigError(TriplError):
    """The tool cannot be configured from flags, environment and config file."""

    exit_code = EXIT_USAGE


class TriplAPIError(TriplError):
    """The instance answered 4xx/5xx.

    Carries the status so each consumer can render its own guidance — the MCP
    server writes to an agent, the CLI writes to a terminal, and neither wording
    belongs in the transport. One class with a ``status_code`` rather than a
    subclass per status: ``raise_for_status`` is already an if-ladder on the
    code and consumers branch the same way, so per-status types would be
    speculative generality.
    """

    def __init__(self, status_code: int, detail: Any, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class TriplConnectionError(TriplError):
    """The instance could not be reached at all (DNS, TLS, refused, timeout).

    ``base_url`` is carried rather than baked into a "check your settings" hint:
    the CLI knows which of --url / $TRIPL_BASE_URL / the config file supplied it
    (``Config.sources``) and can say so; tripl-mcp only ever reads the env var.
    """

    def __init__(self, base_url: str, cause: Exception) -> None:
        super().__init__(f"Could not reach the tripl API at {base_url}: {cause!r}")
        self.base_url = base_url
        self.cause = cause
