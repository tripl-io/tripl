"""Waiting for the stack to actually serve, and reading its bootstrap state.

``docker compose up -d`` returns as soon as the containers are CREATED, which on
a first run is a minute or more before the app answers: the `migrate` one-shot
still has to apply every Alembic revision, and only then do app and workers
start. A command that printed "installed" at that moment would be lying about
the one thing the operator cares about.

Why not ``docker compose up --wait``: its handling of a one-shot service that
exits 0 (``migrate``) has changed across Compose releases, and ``/health`` is the
probe the deployment docs already tell operators to curl. Polling it also means
we are asserting the thing we actually promise — that a browser can reach the
instance — rather than that Docker is happy.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tripl_cli.diagnostics.collect import probe_auth_status, probe_health
from tripl_cli.model import Fetched, JsonDict
from tripl_cli.runner import REQUEST_TIMEOUT_SECONDS, run_unauthenticated

# How long between polls. Short enough that a fast start feels immediate, long
# enough that a five-minute wait is 60 requests and not 3000.
POLL_INTERVAL_SECONDS = 5.0

# Injectable so the tests run in virtual time - no test in this repository ever
# sleeps for real (see conftest.FakeClock).
Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]


# Why no probe was made when the deadline is zero. Callers that skip for their
# own reason - `upgrade` with no APP_BASE_URL in .env - build the outcome
# themselves and supply theirs, because a command that reported "--wait 0" for a
# check it could not make was blaming a flag nobody passed.
SKIPPED_BY_FLAG = "--wait 0"


@dataclass(frozen=True)
class HealthOutcome:
    """Did it come up, how long did we wait, and what did the last probe say."""

    ok: bool
    waited_seconds: float
    attempts: int
    # None only when waiting was skipped entirely.
    last: Fetched[JsonDict] | None = None
    skipped: bool = False
    # Non-empty exactly when `skipped`. One of the two constants above, so the
    # human line and the --json document give the same reason and a consumer can
    # tell "I opted out" from "I could not check".
    skipped_reason: str = ""

    @property
    def status(self) -> str:
        """The token the --json document carries: ``ok`` / ``timeout`` / ``skipped``."""
        if self.skipped:
            return "skipped"
        return "ok" if self.ok else "timeout"


async def _poll(
    base_url: str,
    timeout: float,
    *,
    deadline_seconds: float,
    sleep: Sleep,
    monotonic: Monotonic,
) -> HealthOutcome:
    started = monotonic()
    attempts = 0
    last: Fetched[JsonDict] | None = None
    while True:
        attempts += 1
        last = await probe_health(base_url, timeout)
        if last.ok:
            return HealthOutcome(
                ok=True, waited_seconds=monotonic() - started, attempts=attempts, last=last
            )
        elapsed = monotonic() - started
        if elapsed + POLL_INTERVAL_SECONDS > deadline_seconds:
            return HealthOutcome(ok=False, waited_seconds=elapsed, attempts=attempts, last=last)
        await sleep(POLL_INTERVAL_SECONDS)


def wait_for_health(
    base_url: str,
    *,
    deadline_seconds: float,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    sleep: Sleep = asyncio.sleep,
    monotonic: Monotonic = time.monotonic,
) -> HealthOutcome:
    """Poll ``/health`` until it answers ok, or the deadline passes.

    ``deadline_seconds == 0`` skips waiting entirely rather than probing once:
    ``--wait 0`` means "I will check myself", and a single probe against a stack
    that is three seconds old would report a failure that is not one.
    """
    if deadline_seconds <= 0:
        return HealthOutcome(
            ok=False,
            waited_seconds=0.0,
            attempts=0,
            skipped=True,
            skipped_reason=SKIPPED_BY_FLAG,
        )
    return run_unauthenticated(
        lambda per_request: _poll(
            base_url,
            per_request,
            deadline_seconds=deadline_seconds,
            sleep=sleep,
            monotonic=monotonic,
        ),
        timeout=timeout,
    )


def read_bootstrap(base_url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Fetched[JsonDict]:
    """Read ``/auth/status``. A failure is data, never an exception.

    The install has already succeeded by the time this runs; being unable to
    read the bootstrap state changes the wording of the next steps and nothing
    else, so it must not change the exit code.
    """
    return run_unauthenticated(
        lambda per_request: probe_auth_status(base_url, per_request), timeout=timeout
    )
