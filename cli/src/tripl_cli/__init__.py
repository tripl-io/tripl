"""tripl — operator CLI for a running tripl instance, and the shared REST client.

Two consumers, one implementation: the ``tripl`` console script defined here and
the separate ``tripl-mcp`` distribution, which depends on this one and imports
``tripl_cli.client`` rather than carrying its own copy. This repo
has grown the same defect four times over — query keys drifted four ways, the
event-name regex three, plus security headers and the signal significance gate —
so a second HTTP client was not on the table.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

# The DISTRIBUTION name, not the import package name: this is `tripl`, while the
# module you are reading is `tripl_cli`. version(__name__) would raise
# PackageNotFoundError forever — nothing publishes a distribution called
# tripl_cli.
DISTRIBUTION_NAME = "tripl"

try:
    __version__ = version(DISTRIBUTION_NAME)
except PackageNotFoundError:  # pragma: no cover - source tree with no install
    # Running straight out of a checkout without `uv sync`. Report something
    # honest rather than a plausible-looking number. Deliberately NOT a
    # hardcoded "0.1.0" duplicating pyproject's `version` — that literal can
    # drift from the packaged version and would ship silently in User-Agent.
    __version__ = "0.0.0+unknown"
