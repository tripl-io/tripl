"""Which app versions earn an automatic "Release <version>" chart annotation.

Pure functions only, like :mod:`tripl.services.version_activation` whose gate
they reuse: the metrics worker loads the per-version series and writes the rows,
and the demo seeder draws its marker with the same label and colour, so a real
scan of the demo cannot add a second marker for the same release.

A release is marked at the bucket it ACTIVATED in — the first bucket of its run
of ``min_buckets`` buckets at ``share_min`` or more of total traffic, on at least
``min_volume`` events — never where it was first seen: a dev build surfacing at
0.1% is not a release anyone's chart should point at — and only for a version
that was absent when the loaded slice begins, since a version already carrying
traffic then shipped before it (:func:`releases_to_annotate`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from tripl.services.version_activation import (
    DEFAULT_ACTIVATION_MIN_BUCKETS,
    DEFAULT_ACTIVE_SHARE_MIN,
    DEFAULT_MIN_RELEASE_VOLUME,
    activation_bucket,
    released_versions,
)

# Muted slate, so an automatic marker reads as context next to the red a person
# picks for an incident.
RELEASE_ANNOTATION_COLOR = "#94a3b8"


def release_annotation_label(version: str) -> str:
    """The label a release marker carries, and so its permanent de-dup key."""
    return f"Release {version}"


def releases_to_annotate(
    per_bucket_totals_by_version: Mapping[str, Mapping[datetime, float]],
    all_by_bucket: Mapping[datetime, float],
    *,
    share_min: float = DEFAULT_ACTIVE_SHARE_MIN,
    min_buckets: int = DEFAULT_ACTIVATION_MIN_BUCKETS,
    min_volume: float = DEFAULT_MIN_RELEASE_VOLUME,
    prerelease_pattern: re.Pattern[str] | None = None,
) -> dict[str, datetime]:
    """Versions that shipped INSIDE the loaded slice, with their activation bucket.

    A version is a release candidate only when it had zero traffic in the
    leading buckets of the slice (the first ``min_buckets`` of them): it did not
    exist yet when the slice begins, so its later activation is the moment it
    shipped. Anything already carrying traffic there — the baseline, or a legacy
    version sitting under the gate at slice start and crossing it later, or the
    baseline itself re-crossing after a dip — was live before the slice and its
    activation inside it says nothing about when it shipped. That also makes the
    first scan after a deploy, which loads a series starting with every current
    version already live, mark none of them.

    When the leading buckets carry no traffic at all, nothing can be told apart
    from "already live", so nothing is marked.

    Among the candidates, the activation gate still decides WHEN (and whether)
    to mark: ``share_min`` of total traffic for ``min_buckets`` consecutive
    buckets on at least ``min_volume`` events. Prereleases (SemVer tag, or the
    scan's own pattern) are never marked, the same rule that keeps them from
    being the latest release elsewhere.
    """
    leading = sorted(all_by_bucket)[: max(min_buckets, 1)]
    if not leading or sum(all_by_bucket.get(bucket, 0) for bucket in leading) <= 0:
        return {}

    eligible = released_versions(
        per_bucket_totals_by_version, prerelease_pattern=prerelease_pattern
    )
    releases: dict[str, datetime] = {}
    for version in eligible:
        by_bucket = per_bucket_totals_by_version[version]
        if any(by_bucket.get(bucket, 0) > 0 for bucket in leading):
            continue  # live before the slice began: not shipped inside it
        if sum(by_bucket.values()) < min_volume:
            continue
        bucket = activation_bucket(
            by_bucket, all_by_bucket, share_min=share_min, min_buckets=min_buckets
        )
        if bucket is not None:
            releases[version] = bucket
    return releases
