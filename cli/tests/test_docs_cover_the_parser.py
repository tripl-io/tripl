"""The two pages a user reads first are held to the parser, not to memory.

``cli/README.md`` is the PyPI page, and ``website/docs/run/cli.md`` is the
reference. Both drifted the same way: a command or flag was added to the
parser and never written up. The README was missing every ``events``, ``plan``
and ``docs`` verb and ``whoami``, and counted five writes when there were six;
the reference sent readers to ``--yes`` for an unorderable upgrade because
``--allow-unordered-tag`` was never named on it. Every set below is derived from
``build_parser()``, so the only way to add a command or a flag is to write it up.
"""

from __future__ import annotations

import re

from tests.test_contract import DOCS_PATH, REPO_ROOT, _command_paths, _leaf_parsers
from tripl_cli import __version__
from tripl_cli.cli import build_parser
from tripl_cli.install import files
from tripl_cli.install.plan import RELEASE_TAG

README_PATH = REPO_ROOT / "cli" / "README.md"

# argparse adds it to every parser; no page needs to name it per command.
_IMPLICIT_OPTIONS = frozenset({"-h", "--help"})


def _named(text: str, option: str) -> bool:
    """``--open-questions`` is not named by ``--no-open-questions``, nor ``--to`` by ``--top``."""
    return re.search(rf"(?<![\w-]){re.escape(option)}(?![\w-])", text) is not None


def test_every_option_of_every_command_is_in_the_reference() -> None:
    reference = DOCS_PATH.read_text(encoding="utf-8")
    leaves = _leaf_parsers(build_parser())
    assert leaves, "walked the parser and found no commands - the walk is broken"
    missing = sorted(
        f"{path} {option}"
        for path, parser in leaves.items()
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--")
        and option not in _IMPLICIT_OPTIONS
        and not _named(reference, option)
    )
    assert not missing, f"{DOCS_PATH.name} never names these flags: {missing}"


def test_every_command_and_verb_is_in_the_readme() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    paths = _command_paths(build_parser())
    assert paths, "walked the parser and found no commands - the walk is broken"
    missing = [path for path in paths if not _named(readme, path)]
    assert not missing, f"cli/README.md (the PyPI page) never shows: {missing}"


def _readme_writes(readme: str) -> set[str]:
    """The command lines the README's command block marks ``(WRITE)``."""
    leaves = set(_leaf_parsers(build_parser()))
    marked: set[str] = set()
    for line in readme.splitlines():
        if not (line.startswith("tripl ") and line.rstrip().endswith("(WRITE)")):
            continue
        words = line.split()
        # `tripl scans run ...` is a grouped verb, `tripl annotate ...` a
        # top-level one: the longest prefix that is a real command line wins.
        for length in (3, 2):
            candidate = " ".join(words[:length])
            if candidate in leaves:
                marked.add(candidate)
                break
    return marked


def test_the_readme_marks_exactly_the_commands_that_write() -> None:
    """The same derivation test_contract.py holds the reference's write table to.

    A verb that writes carries both ``--dry-run`` and ``--project``; ``install``
    and ``upgrade`` carry ``--dry-run`` too and are excluded because they act on
    a directory, not an instance - which is why the README marks them HOST.
    """
    leaves = _leaf_parsers(build_parser())
    writes = {
        path
        for path, parser in leaves.items()
        if any("--dry-run" in action.option_strings for action in parser._actions)
        and any("--project" in action.option_strings for action in parser._actions)
    }
    assert writes, "derived no mutating verbs at all - the derivation is broken"
    assert _readme_writes(README_PATH.read_text(encoding="utf-8")) == writes


# A version the README puts in front of a reader to type.
_SUGGESTED_VERSION = re.compile(r"(?:--version|--to)[ =](\S+)|TRIPL_VERSION=(\S+)")
# A version the README says a command prints, as a `# tripl 0.1.0` comment.
_ECHOED_VERSION = re.compile(r"^#\s*tripl\s+\d\S*", re.MULTILINE)


def test_the_readme_suggests_no_version_that_was_never_released() -> None:
    """The deploy docs once said ``--version 1.4.0`` while the newest release was 0.3.1.

    Every example is the placeholder, or an X.Y.Z no newer than this CLI's own
    version. The image, the CLI and tripl-mcp share one version, so a tag above
    the installed CLI's had not been released when this CLI was.
    """
    readme = README_PATH.read_text(encoding="utf-8")
    installed = RELEASE_TAG.fullmatch(__version__)
    assert installed is not None, f"the installed CLI's version {__version__!r} is not X.Y.Z"
    ceiling = tuple(int(part) for part in installed.groups())
    wrong: list[str] = []
    for match in _SUGGESTED_VERSION.finditer(readme):
        value = next(group for group in match.groups() if group is not None)
        if value == files.TAG_PLACEHOLDER:
            continue
        release = RELEASE_TAG.fullmatch(value)
        if release is None or tuple(int(part) for part in release.groups()) > ceiling:
            wrong.append(match.group(0))
    assert not wrong, f"cli/README.md suggests versions that may not exist: {wrong}"


def test_the_readme_links_resolve_on_pypi() -> None:
    """PyPI renders the README with no repository around it: ``../mcp-server`` 404s there."""
    readme = README_PATH.read_text(encoding="utf-8")
    relative = [
        target
        for target in re.findall(r"\]\(([^)]+)\)", readme)
        if not target.startswith(("http://", "https://", "#", "mailto:"))
    ]
    assert not relative, f"cli/README.md has links PyPI cannot follow: {relative}"


def test_the_readme_echoes_no_version_number() -> None:
    """``# tripl 0.1.0`` under ``tripl --version`` was a number nothing kept current."""
    echoed = _ECHOED_VERSION.findall(README_PATH.read_text(encoding="utf-8"))
    assert not echoed, f"cli/README.md shows a version a command prints: {echoed}"
