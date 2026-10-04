from __future__ import annotations

from tripl.semver import (
    compare_versions,
    latest_previous_versions,
    latest_version,
    order_versions,
    parse_version,
)


def test_parse_version_returns_semver_key() -> None:
    parsed = parse_version("v2.10.0-rc.1+sha.abc")

    assert parsed.is_semver is True
    assert parsed.release == (2, 10, 0)
    assert [part.raw for part in parsed.prerelease] == ["rc", "1"]
    assert [part.numeric for part in parsed.prerelease] == [None, 1]
    assert parsed.build == ("sha", "abc")


def test_parse_version_falls_back_for_invalid_semver() -> None:
    parsed = parse_version("1.02.0")

    assert parsed.is_semver is False
    assert parsed.normalized == "1.02.0"
    assert parsed.release is None


def test_order_versions_uses_numeric_semver_components() -> None:
    versions = ["2.10.0", "1.99.0", "2.9.0", "2.10.0-alpha.1"]

    assert order_versions(versions) == ["1.99.0", "2.9.0", "2.10.0-alpha.1", "2.10.0"]


def test_order_versions_uses_prerelease_precedence() -> None:
    versions = [
        "1.0.0",
        "1.0.0-rc.1",
        "1.0.0-beta.11",
        "1.0.0-beta.2",
        "1.0.0-beta",
        "1.0.0-alpha.beta",
        "1.0.0-alpha.1",
        "1.0.0-alpha",
    ]

    assert order_versions(versions) == [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    ]


def test_build_metadata_does_not_change_semver_precedence() -> None:
    assert compare_versions("1.0.0+build.1", "1.0.0+build.2") == 0
    assert compare_versions("1.0.0-alpha+build.1", "1.0.0") == -1
    assert order_versions(["1.0.0+build.2", "1.0.0+build.1"]) == [
        "1.0.0+build.1",
        "1.0.0+build.2",
    ]


def test_mixed_invalid_versions_use_deterministic_lexical_fallback() -> None:
    versions = ["beta", "2.0", "1.0.0", "2.0.0", "alpha", "v1.2.0"]

    # A dotted-numeric "2.0" now ranks numerically, tied with "2.0.0" and
    # placed just below it; free text still sorts lowest.
    assert order_versions(versions) == ["alpha", "beta", "1.0.0", "v1.2.0", "2.0", "2.0.0"]
    assert compare_versions("alpha", "beta") == -1
    assert compare_versions("1.0.0", "beta") == 1


def test_invalid_prerelease_numeric_identifier_uses_fallback() -> None:
    assert parse_version("1.0.0-alpha.01").is_semver is False
    assert order_versions(["1.0.0-alpha.01", "1.0.0-alpha.1"]) == [
        "1.0.0-alpha.01",
        "1.0.0-alpha.1",
    ]


def test_latest_previous_versions_select_distinct_precedence_values() -> None:
    versions = [
        "1.9.0",
        "2.0.0+build.2",
        "2.0.0-rc.1",
        "2.0.0+build.1",
        "2.0.0+build.2",
    ]

    assert latest_version(versions) == "2.0.0+build.2"
    assert latest_previous_versions(versions) == ("2.0.0+build.2", "2.0.0-rc.1")


def test_latest_previous_versions_handles_empty_and_single_value_inputs() -> None:
    assert latest_previous_versions([]) == (None, None)
    assert latest_previous_versions(["not-semver"]) == ("not-semver", None)
