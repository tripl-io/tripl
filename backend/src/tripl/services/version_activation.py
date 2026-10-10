"""Shared release-activation logic.

A release is "active" only once it takes a real share of user traffic. This is
what separates a shipped release from a dev/tester build that surfaces with a
higher SemVer but negligible traffic. Both the release-regression analyzer
(``tripl.core.analyzers.release_regression``) and the app-version series
selection (``tripl.services.metrics_service``) build on these primitives so
there is a single definition of "active".

Pure functions only — no database or warehouse access — so the logic stays
unit-testable. Buckets are ``datetime`` timestamps throughout.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from datetime import datetime

from tripl.semver import is_prerelease

logger = logging.getLogger(__name__)

# The single source of truth for the activation gate. These began life in a
# decision note that has since been deleted as stale, so the values here ARE the
# specification — there is no other document to reconcile them against.
#
# A release counts as active once it holds >=5% of total traffic for >=2 buckets
# on >=200 events. That is what excludes the dev/tester build phase, before a
# rollout reaches real users, from being compared against a mature release.
DEFAULT_ACTIVE_SHARE_MIN = 0.05
DEFAULT_ACTIVATION_MIN_BUCKETS = 2
DEFAULT_MIN_RELEASE_VOLUME = 200


def resolve_share_min(share_min: float | None) -> float:
    """Per-scan activation share override, falling back to the system default.

    ``None`` (column unset) keeps ``DEFAULT_ACTIVE_SHARE_MIN`` (0.05). An explicit
    value — including small fractions — is honored as-is.
    """
    return share_min if share_min is not None else DEFAULT_ACTIVE_SHARE_MIN


def compile_prerelease_pattern(pattern: str | None) -> re.Pattern[str] | None:
    """Compile a per-scan prerelease regex, or ``None`` when unset or invalid.

    An invalid regex is treated as "no pattern" so a bad config value can never
    crash a scan — selection just falls back to the SemVer prerelease-tag default.
    """
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        logger.warning("Invalid prerelease pattern in saved scan config: %r", pattern)
        return None


def is_prerelease_version(
    version: str, *, prerelease_pattern: re.Pattern[str] | None = None
) -> bool:
    """Whether ``version`` is a prerelease/dev build ineligible to be latest/active.

    True when the string carries a SemVer prerelease tag (the always-on default)
    or, when a per-scan ``prerelease_pattern`` is supplied, when the raw string
    matches that pattern.
    """
    if is_prerelease(version):
        return True
    return prerelease_pattern is not None and prerelease_pattern.search(version) is not None


def released_versions(
    versions: Iterable[str], *, prerelease_pattern: re.Pattern[str] | None = None
) -> set[str]:
    """Subset of ``versions`` eligible to be the latest/active release.

    Excludes SemVer prereleases (default) and, when ``prerelease_pattern`` is set,
    any string matching it. Prereleases stay visible as their own series
    elsewhere; this gate only governs latest-selection and retention priority.
    """
    return {
        version
        for version in versions
        if not is_prerelease_version(version, prerelease_pattern=prerelease_pattern)
    }


def activation_bucket(
    release_by_bucket: Mapping[datetime, float],
    all_by_bucket: Mapping[datetime, float],
    *,
    share_min: float = DEFAULT_ACTIVE_SHARE_MIN,
    min_buckets: int = DEFAULT_ACTIVATION_MIN_BUCKETS,
) -> datetime | None:
    """First bucket starting a run of ``min_buckets`` consecutive buckets whose
    share of total traffic is at least ``share_min``.

    Buckets are iterated in sorted order. Returns ``None`` when no such run
    exists (the release never took a real share of traffic).
    """
    run_start: datetime | None = None
    run_len = 0
    for bucket in sorted(all_by_bucket):
        total = all_by_bucket.get(bucket, 0)
        share = (release_by_bucket.get(bucket, 0) / total) if total > 0 else 0.0
        if share >= share_min:
            if run_len == 0:
                run_start = bucket
            run_len += 1
            if run_len >= min_buckets:
                return run_start
        else:
            run_len = 0
            run_start = None
    return None


def _total_traffic_by_bucket(
    per_bucket_totals_by_version: Mapping[str, Mapping[datetime, float]],
) -> dict[datetime, float]:
    totals: dict[datetime, float] = {}
    for by_bucket in per_bucket_totals_by_version.values():
        for bucket, count in by_bucket.items():
            totals[bucket] = totals.get(bucket, 0) + count
    return totals


def active_release_versions(
    per_bucket_totals_by_version: Mapping[str, Mapping[datetime, float]],
    all_by_bucket: Mapping[datetime, float] | None = None,
    *,
    share_min: float = DEFAULT_ACTIVE_SHARE_MIN,
    min_buckets: int = DEFAULT_ACTIVATION_MIN_BUCKETS,
    min_volume: float = DEFAULT_MIN_RELEASE_VOLUME,
) -> set[str]:
    """Return the set of versions that have activated.

    A version activates when it both clears an absolute volume floor
    (``min_volume`` events summed across all buckets) and holds at least
    ``share_min`` of total traffic for ``min_buckets`` consecutive buckets.

    ``all_by_bucket`` is the traffic denominator; when omitted it is derived by
    summing every version's per-bucket totals. Callers that track an "Other"
    bucket (rolled-up non-retained releases) should pass an explicit
    ``all_by_bucket`` that includes it so shares are not inflated.
    """
    denominator = (
        all_by_bucket
        if all_by_bucket is not None
        else _total_traffic_by_bucket(per_bucket_totals_by_version)
    )
    active: set[str] = set()
    for version, by_bucket in per_bucket_totals_by_version.items():
        if sum(by_bucket.values()) < min_volume:
            continue
        if (
            activation_bucket(by_bucket, denominator, share_min=share_min, min_buckets=min_buckets)
            is not None
        ):
            active.add(version)
    return active
