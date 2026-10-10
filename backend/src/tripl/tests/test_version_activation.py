"""Unit tests for the shared release-activation gate and its use in the
app-version series builder.

Covers the root cause of the "a tiny dev/tester build is shown as the active
release" complaint: a low-traffic higher-SemVer build must not be picked as the
latest while a mature lower-SemVer release is active, and the pick falls back to
the raw SemVer-max only when nothing has activated.
"""

import re
from datetime import datetime

from tripl.semver import APP_VERSION_OTHER_LABEL, order_versions
from tripl.services.metrics_service import _build_app_version_series, _retained_versions
from tripl.services.version_activation import (
    activation_bucket,
    active_release_versions,
    compile_prerelease_pattern,
    is_prerelease_version,
    released_versions,
    resolve_share_min,
)

DAYS = [datetime(2026, 1, d) for d in range(1, 6)]  # 5 daily buckets


# --- pure helper -----------------------------------------------------------


def test_activation_bucket_returns_run_start() -> None:
    release = {d: 100 for d in DAYS}
    total = {d: 1000 for d in DAYS}
    # 10% share every bucket -> activates on the first bucket of the 2-run.
    assert activation_bucket(release, total, share_min=0.05, min_buckets=2) == DAYS[0]


def test_activation_bucket_none_below_share() -> None:
    release = {d: 10 for d in DAYS}  # ~1% share
    total = {d: 1000 for d in DAYS}
    assert activation_bucket(release, total, share_min=0.05, min_buckets=2) is None


def test_activation_bucket_needs_consecutive_buckets() -> None:
    # Alternating high/low share never reaches a 2-bucket run.
    release = {DAYS[0]: 100, DAYS[1]: 10, DAYS[2]: 100, DAYS[3]: 10, DAYS[4]: 100}
    total = {d: 1000 for d in DAYS}
    assert activation_bucket(release, total, share_min=0.05, min_buckets=2) is None


def test_active_release_versions_excludes_low_volume_and_low_share() -> None:
    per_version = {
        "1.0.0": {d: 1000 for d in DAYS},  # mature, high volume + share
        "2.0.0": {d: 10 for d in DAYS},  # dev build: 50 total, ~1% share
    }
    total = {d: 1010 for d in DAYS}
    active = active_release_versions(
        per_version, total, share_min=0.05, min_buckets=2, min_volume=200
    )
    assert active == {"1.0.0"}


def test_active_release_versions_volume_floor_alone_excludes() -> None:
    # High share but under the volume floor -> not active.
    per_version = {"9.9.9": {DAYS[0]: 100, DAYS[1]: 90}}  # 190 < 200
    total = {DAYS[0]: 100, DAYS[1]: 90}
    assert active_release_versions(per_version, total, min_volume=200) == set()


def test_active_release_versions_skips_a_higher_semver_inactive_build() -> None:
    per_version = {
        "1.0.0": {d: 1000 for d in DAYS},
        "2.0.0": {d: 10 for d in DAYS},  # higher SemVer, but inactive
    }
    total = {d: 1010 for d in DAYS}
    assert active_release_versions(per_version, total) == {"1.0.0"}


def test_active_release_versions_empty_when_nothing_activates() -> None:
    per_version = {
        "1.0.0": {d: 10 for d in DAYS},
        "2.0.0": {d: 5 for d in DAYS},
    }
    total = {d: 15 for d in DAYS}
    assert active_release_versions(per_version, total) == set()


def test_latest_pick_is_the_semver_max_among_active_releases() -> None:
    per_version = {
        "2.2.0": {d: 500 for d in DAYS},
        "2.10.0": {d: 500 for d in DAYS},  # SemVer-max, also active
        "2.9.0": {d: 500 for d in DAYS},
    }
    total = {d: 1500 for d in DAYS}
    active = active_release_versions(per_version, total)
    assert active == set(per_version)
    # Lexical max would be "2.9.0"; SemVer max is "2.10.0".
    assert order_versions(active, reverse=True)[0] == "2.10.0"


# --- _build_app_version_series integration ---------------------------------


def _rows(counts_by_bucket: dict[datetime, int]) -> list[tuple[datetime, int]]:
    return sorted(counts_by_bucket.items())


def test_build_series_low_traffic_dev_build_is_not_latest() -> None:
    # Mature 1.0.0 (active) vs higher-SemVer 2.0.0 dev build (tiny traffic).
    metric_rows = {
        ("1.0.0", False): _rows({d: 1000 for d in DAYS}),
        ("2.0.0", False): _rows({d: 10 for d in DAYS}),
    }
    latest_version, versions, series = _build_app_version_series(
        interval=None,
        metric_rows_by_series=metric_rows,
        keep_releases=5,
    )

    assert latest_version == "1.0.0"

    by_version = {v.version: v for v in versions}
    assert by_version["1.0.0"].is_latest is True
    assert by_version["1.0.0"].is_active is True
    assert by_version["2.0.0"].is_latest is False
    assert by_version["2.0.0"].is_active is False

    series_by_version = {s.version: s for s in series}
    assert series_by_version["1.0.0"].is_latest is True
    assert series_by_version["1.0.0"].is_active is True
    assert series_by_version["2.0.0"].is_active is False


def test_build_series_falls_back_to_semver_max_when_nothing_active() -> None:
    # Neither release clears the activation gate (both tiny) -> raw SemVer-max.
    metric_rows = {
        ("1.0.0", False): _rows({d: 10 for d in DAYS}),
        ("2.0.0", False): _rows({d: 5 for d in DAYS}),
    }
    latest_version, versions, _series = _build_app_version_series(
        interval=None,
        metric_rows_by_series=metric_rows,
        keep_releases=5,
    )

    assert latest_version == "2.0.0"
    by_version = {v.version: v for v in versions}
    # Fallback names a "latest", but nothing is marked active.
    assert by_version["2.0.0"].is_latest is True
    assert by_version["2.0.0"].is_active is False
    assert by_version["1.0.0"].is_active is False


def test_build_series_uses_project_total_maturity_not_event_volume() -> None:
    # This event is rare in 2.0.0, but the release is mature across the project.
    # Version status must come from the project-total rows, not from this event's
    # own per-version volume.
    event_rows = {
        ("1.0.0", False): _rows({d: 1000 for d in DAYS}),
        ("2.0.0", False): _rows({d: 1 for d in DAYS}),
    }
    project_total_rows = {
        ("1.0.0", False): _rows({d: 10 for d in DAYS}),
        ("2.0.0", False): _rows({d: 990 for d in DAYS}),
    }

    latest_version, versions, _series = _build_app_version_series(
        interval=None,
        metric_rows_by_series=event_rows,
        maturity_metric_rows_by_series=project_total_rows,
        keep_releases=5,
    )

    assert latest_version == "2.0.0"
    by_version = {version.version: version for version in versions}
    assert by_version["2.0.0"].is_latest is True
    assert by_version["2.0.0"].is_active is True
    assert by_version["1.0.0"].is_active is False


def test_build_series_empty_input_is_inert() -> None:
    latest_version, versions, series = _build_app_version_series(
        interval=None,
        metric_rows_by_series={},
        keep_releases=5,
    )
    assert latest_version is None
    assert versions == []
    assert series == []


# --- active-first retention -------------------------------------------------


def test_retained_versions_prefers_active_over_higher_semver() -> None:
    # keep_releases=1: the active 1.0.0 takes the slot, not the higher-SemVer
    # (but inactive) 2.0.0 dev build.
    assert _retained_versions({"1.0.0", "2.0.0"}, {"1.0.0"}, 1) == {"1.0.0"}


def test_retained_versions_fills_remaining_slots_with_newest_ungated() -> None:
    # One active release + two ungated: keep 2 -> active 1.0.0 first, then the
    # newest ungated (3.0.0), NOT 2.0.0.
    assert _retained_versions({"1.0.0", "2.0.0", "3.0.0"}, {"1.0.0"}, 2) == {
        "1.0.0",
        "3.0.0",
    }


def test_retained_versions_pure_semver_when_nothing_active() -> None:
    assert _retained_versions({"1.0.0", "2.0.0"}, set(), 1) == {"2.0.0"}


def test_retained_versions_zero_keep_is_empty() -> None:
    assert _retained_versions({"1.0.0"}, {"1.0.0"}, 0) == set()


def test_build_series_dev_build_does_not_steal_retention_slot() -> None:
    # keep_releases=1 with a higher-SemVer dev build: the active 1.0.0 keeps the
    # only slot and the tiny 2.0.0 is folded into "Other" rather than displacing
    # the shipped release.
    metric_rows = {
        ("1.0.0", False): _rows({d: 1000 for d in DAYS}),
        ("2.0.0", False): _rows({d: 10 for d in DAYS}),
    }
    latest_version, _versions, series = _build_app_version_series(
        interval=None,
        metric_rows_by_series=metric_rows,
        keep_releases=1,
    )
    assert latest_version == "1.0.0"
    series_by_version = {s.version: s for s in series}
    assert "1.0.0" in series_by_version
    assert series_by_version["1.0.0"].is_active is True
    # The dev build was folded away, not kept as an explicit series.
    assert "2.0.0" not in series_by_version
    assert APP_VERSION_OTHER_LABEL in series_by_version


# --- prerelease exclusion (default SemVer-tag + per-scan pattern) -----------


def test_is_prerelease_version_semver_tag_default() -> None:
    assert is_prerelease_version("3.0.0-beta.1") is True
    assert is_prerelease_version("2.1.0-rc.2") is True
    assert is_prerelease_version("2.0.0") is False
    # Non-SemVer strings have no prerelease component.
    assert is_prerelease_version("build-42") is False


def test_is_prerelease_version_custom_pattern() -> None:
    pattern = re.compile(r"-nightly$")
    assert is_prerelease_version("2.0.0-nightly", prerelease_pattern=pattern) is True
    # A released SemVer that does not match the pattern stays released.
    assert is_prerelease_version("2.0.0", prerelease_pattern=pattern) is False


def test_released_versions_excludes_semver_prereleases() -> None:
    versions = {"1.0.0", "2.0.0-beta.1", "2.0.0"}
    assert released_versions(versions) == {"1.0.0", "2.0.0"}


def test_released_versions_applies_custom_pattern_on_top_of_default() -> None:
    versions = {"1.0.0", "2.0.0-rc.1", "3.0.0", "3.0.0-internal"}
    pattern = compile_prerelease_pattern(r"internal")
    # SemVer-prerelease "2.0.0-rc.1" is excluded by default; "3.0.0-internal" is a
    # valid-looking build string excluded only by the custom pattern.
    assert released_versions(versions, prerelease_pattern=pattern) == {"1.0.0", "3.0.0"}


def test_compile_prerelease_pattern_invalid_regex_is_none() -> None:
    # An unbalanced group must not raise — it degrades to "no pattern".
    assert compile_prerelease_pattern("(") is None
    assert compile_prerelease_pattern("") is None
    assert compile_prerelease_pattern(None) is None
    assert compile_prerelease_pattern(r"-beta") is not None


def test_resolve_share_min_defaults_and_override() -> None:
    assert resolve_share_min(None) == 0.05
    assert resolve_share_min(0.20) == 0.20


def test_build_series_semver_prerelease_never_latest_even_when_max() -> None:
    # 2.0.0-beta.1 is the SemVer max AND clears the volume/share gate, yet a
    # prerelease is ineligible to be latest/active. The released 1.0.0 wins.
    metric_rows = {
        ("1.0.0", False): _rows({d: 1000 for d in DAYS}),
        ("2.0.0-beta.1", False): _rows({d: 1000 for d in DAYS}),
    }
    latest_version, versions, series = _build_app_version_series(
        interval=None,
        metric_rows_by_series=metric_rows,
        keep_releases=5,
    )
    assert latest_version == "1.0.0"
    by_version = {v.version: v for v in versions}
    assert by_version["1.0.0"].is_latest is True
    assert by_version["1.0.0"].is_active is True
    # The prerelease is still visible as its own series, just never latest/active.
    assert by_version["2.0.0-beta.1"].is_latest is False
    assert by_version["2.0.0-beta.1"].is_active is False
    series_by_version = {s.version: s for s in series}
    assert "2.0.0-beta.1" in series_by_version


def test_build_series_custom_pattern_excludes_matching_version() -> None:
    # Non-SemVer-prerelease build "2.0.0-internal" is only excluded via the
    # per-scan pattern; without it, it would be the SemVer-max latest.
    metric_rows = {
        ("1.0.0", False): _rows({d: 1000 for d in DAYS}),
        ("2.0.0-internal", False): _rows({d: 1000 for d in DAYS}),
    }
    latest_version, versions, _series = _build_app_version_series(
        interval=None,
        metric_rows_by_series=metric_rows,
        keep_releases=5,
        prerelease_pattern=compile_prerelease_pattern(r"internal"),
    )
    assert latest_version == "1.0.0"
    by_version = {v.version: v for v in versions}
    assert by_version["2.0.0-internal"].is_latest is False
    assert by_version["2.0.0-internal"].is_active is False


def test_build_series_prerelease_does_not_steal_retention_slot() -> None:
    # keep_releases=1: the released 1.0.0 keeps the only slot even though the
    # prerelease 2.0.0-beta.1 is higher SemVer and has real traffic.
    metric_rows = {
        ("1.0.0", False): _rows({d: 1000 for d in DAYS}),
        ("2.0.0-beta.1", False): _rows({d: 1000 for d in DAYS}),
    }
    _latest, _versions, series = _build_app_version_series(
        interval=None,
        metric_rows_by_series=metric_rows,
        keep_releases=1,
    )
    series_by_version = {s.version: s for s in series}
    assert "1.0.0" in series_by_version
    assert "2.0.0-beta.1" not in series_by_version
    assert APP_VERSION_OTHER_LABEL in series_by_version


def test_build_series_share_min_override_changes_activation() -> None:
    # 2.0.0 holds ~9% share (90 of ~1000/bucket). Under the default 0.05 floor it
    # activates and, as SemVer-max active, becomes latest. Under a 0.15 override it
    # no longer activates, so latest falls back to the active 1.0.0.
    metric_rows = {
        ("1.0.0", False): _rows({d: 910 for d in DAYS}),
        ("2.0.0", False): _rows({d: 90 for d in DAYS}),
    }
    latest_default, _v, _s = _build_app_version_series(
        interval=None,
        metric_rows_by_series=metric_rows,
        keep_releases=5,
    )
    assert latest_default == "2.0.0"

    latest_strict, versions_strict, _s2 = _build_app_version_series(
        interval=None,
        metric_rows_by_series=metric_rows,
        keep_releases=5,
        share_min=0.15,
    )
    assert latest_strict == "1.0.0"
    by_version = {v.version: v for v in versions_strict}
    assert by_version["2.0.0"].is_active is False
