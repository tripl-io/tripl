"""Docs catalog path rules (F22, GH #299)."""

import pytest

from tripl.services.docs_paths import (
    MAX_PATH_CHARS,
    MAX_PATH_SEGMENTS,
    DocPathError,
    file_stem,
    is_under,
    normalize_doc_path,
    normalize_prefix,
    path_key,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("guides/setup.md", "guides/setup.md"),
        ("./guides//setup.md", "guides/setup.md"),
        ("  README.md ", "README.md"),
        ("SKILL.md", "SKILL.md"),
        ("references/api (v2).md", "references/api (v2).md"),
        ("notes/UPPER.MD", "notes/UPPER.MD"),
        # NFC: "e" + combining acute becomes the single code point.
        ("café.md", "café.md"),
    ],
)
def test_paths_are_normalised(raw: str, expected: str) -> None:
    if not expected.isascii():
        # The segment rule is ASCII only, so a composed accent is refused too —
        # but only after normalisation, which is what this case pins.
        with pytest.raises(DocPathError, match="not allowed"):
            normalize_doc_path(raw)
        return
    assert normalize_doc_path(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "empty"),
        ("/etc/passwd.md", "relative"),
        ("../secrets.md", r"'\.' or '\.\.'"),
        ("a/../b.md", r"'\.' or '\.\.'"),
        ("a/./b.md", r"'\.' or '\.\.'"),
        ("a\\b.md", "backslash"),
        ("a\x00b.md", "control"),
        ("notes.txt", "end in .md"),
        ("notes", "end in .md"),
        (".hidden.md", "not allowed"),
        ("a/-dash.md", "not allowed"),
        ("a/b*c.md", "not allowed"),
        ("/".join(["d"] * MAX_PATH_SEGMENTS) + "/x.md", "deeper"),
        ("a" * (MAX_PATH_CHARS + 1) + ".md", "longer"),
    ],
)
def test_bad_paths_are_refused(raw: str, message: str) -> None:
    with pytest.raises(DocPathError, match=message):
        normalize_doc_path(raw)


def test_prefix_accepts_a_trailing_slash_and_skips_the_md_rule() -> None:
    assert normalize_prefix("references/") == "references"
    assert normalize_prefix("./a//b/") == "a/b"
    with pytest.raises(DocPathError):
        normalize_prefix("/")
    with pytest.raises(DocPathError):
        normalize_prefix("../x")


def test_keys_and_prefixes_are_case_insensitive() -> None:
    assert path_key("Guides/Setup.md") == path_key("guides/setup.md")
    assert is_under("Refs/a.md", "refs")
    assert not is_under("refsX/a.md", "refs")
    assert not is_under("refs.md", "refs")


def test_file_stem() -> None:
    assert file_stem("a/b/Warehouse gotchas.md") == "Warehouse gotchas"
    assert file_stem("x.MD") == "x"
