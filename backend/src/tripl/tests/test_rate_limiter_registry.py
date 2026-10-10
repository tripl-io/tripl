"""An extension builds its own limiters from the public ``limiter_for``.

The core keeps only the limiters its own routes use; the test fixtures reset
every limiter alive, so a suite that reuses them (``pytest_plugins``) never
has to name an extension's limiters to start each test with full buckets.
"""

from __future__ import annotations

import weakref

import pytest

from tripl.middleware import rate_limit
from tripl.middleware.rate_limit import RateLimitExceeded, limiter_for, reset_rate_limiters


def test_reset_reaches_a_limiter_built_outside_the_core() -> None:
    extensions_limiter = limiter_for(1, per_seconds=60.0, name="test_extension_probe")
    extensions_limiter.acquire("caller")
    with pytest.raises(RateLimitExceeded):
        extensions_limiter.acquire("caller")

    reset_rate_limiters()

    extensions_limiter.acquire("caller")  # a full bucket again


def test_the_registry_does_not_keep_a_dropped_limiter_alive() -> None:
    limiter = limiter_for(3, per_seconds=60.0, name="test_throwaway")
    assert limiter in rate_limit._LIMITERS
    alive = weakref.ref(limiter)

    del limiter

    assert alive() is None
