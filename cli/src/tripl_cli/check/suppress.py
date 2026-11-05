"""Inline suppression: ``tripl-check-ignore[code] -- reason`` comments, and the calls they cover.

A marker waives the codes it lists on the calls it covers, and only with a
reason. The grammar is the same in every language the scan reads::

    tripl-check-ignore[<target>(,<target>)*] -- <reason>
    target := code [":" field]      code := [a-z][a-z0-9_]*      field := [^,\\]]+

It must begin a comment line: right after ``//``, ``/*`` or a block comment's
leading ``*``, so a comment that only mentions the keyword mid-sentence is not
one. Codes are not checked against a list (a misspelled one simply waives
nothing, and shows as unused). A marker that names no code, names
``no_verdict`` or gives no reason suppresses nothing; it is reported, never
refused, so the finding it meant to waive still fails the run.

Comments are found in the GAPS between the lexer's tokens: every character the
lexer does not drop is part of a token, so a gap holds whitespace and comments
only, and a marker inside a string, a template literal or a regex is never read.

A marker covers every matched call whose lines (its builder chain included)
contain the marker's line; and, when nothing but whitespace precedes it on its
line, every call starting on the next line that holds code.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from tripl_cli.check.calls import Call
from tripl_cli.check.lexer import LineIndex, Token
from tripl_cli.check.model import (
    CODE_NO_VERDICT,
    MARKER_NO_CODES,
    MARKER_NO_REASON,
    MARKER_NOT_SUPPRESSIBLE,
    CheckResult,
    Marker,
    MarkerState,
    Suppression,
)

KEYWORD = "tripl-check-ignore"

# A comment line that is a marker: the opener (or a block comment's leading
# stars), whitespace, then the keyword as a whole word.
_MARKER_LINE = re.compile(r"[ \t]*(?:/\*+|//+|\*+(?!/))?[ \t]*(tripl-check-ignore)(?![\w-])")
_TARGETS = re.compile(r"\[([^\]]*)\]")
_TARGET = re.compile(r"([a-z][a-z0-9_]*)(?::([^,\]]+))?")
_REASON = re.compile(r"\s*--(.*)", re.DOTALL)

# How each problem reads after the marker's location.
PROBLEM_TEXT: Mapping[str, str] = {
    MARKER_NO_CODES: f"{KEYWORD} needs the codes it waives, as {KEYWORD}[<code>]",
    MARKER_NOT_SUPPRESSIBLE: f"{KEYWORD} cannot waive {CODE_NO_VERDICT}",
    MARKER_NO_REASON: f'{KEYWORD} needs "-- <reason>"',
}


@dataclass(frozen=True)
class Placed:
    """A marker with what decides which calls it covers.

    ``own_line`` is true when no code precedes the marker on its line;
    ``next_line`` is the line of the first token after its comment, None at
    the end of the file.
    """

    marker: Marker
    own_line: bool
    next_line: int | None


def comments(text: str, tokens: Sequence[Token], language: str) -> Iterator[tuple[int, int, int]]:
    """``(start, end, gap)`` for each comment in ``text``, ``gap`` the index of the token after it.

    Gaps are the text before the first token, between each two and after the
    last. A ``/* */`` nests in Swift and Kotlin, as the lexer reads it.
    """
    nests = language in ("swift", "kotlin")
    bounds = [0, *(token.end for token in tokens)]
    for gap, start in enumerate(bounds):
        stop = tokens[gap].offset if gap < len(tokens) else len(text)
        index = start
        while index < stop:
            if text.startswith("//", index):
                end = text.find("\n", index, stop)
                end = stop if end < 0 else end
            elif text.startswith("/*", index):
                end = _block_end(text, index, stop, nests)
            else:
                index += 1
                continue
            yield index, end, gap
            index = end


def _block_end(text: str, index: int, stop: int, nests: bool) -> int:
    """Where the block comment opening at ``index`` ends; the lexer's ``_block_comment``."""
    depth = 0
    while index < stop:
        if text.startswith("/*", index):
            depth = depth + 1 if nests or depth == 0 else depth
            index += 2
        elif text.startswith("*/", index):
            depth -= 1
            index += 2
            if depth <= 0:
                return index
        else:
            index += 1
    return stop


def markers(
    path: str, text: str, tokens: Sequence[Token], language: str, lines: LineIndex
) -> list[Placed]:
    """Every marker in ``text``, in file order. A file without the keyword is not walked."""
    if KEYWORD not in text:
        return []
    found: list[Placed] = []
    for start, end, gap in comments(text, tokens, language):
        body = text[start:end]
        if KEYWORD not in body:
            continue
        block = body.startswith("/*")
        before, after = gap - 1, gap
        if (
            language == "ts"
            and before >= 0
            and after < len(tokens)
            and tokens[before].is_punct("{")
            and tokens[after].is_punct("}")
        ):
            # JSX's `{/* … */}`: the braces only hold the comment, so they are
            # not code before it, and the line after them is the next one.
            before, after = before - 1, after + 1
        previous = lines.position(tokens[before].end - 1)[0] if before >= 0 else 0
        following = lines.position(tokens[after].offset)[0] if after < len(tokens) else None
        offset = start
        for raw in body.split("\n"):
            match = _MARKER_LINE.match(raw)
            if match is not None:
                rest = raw[match.end() :]
                if block:
                    # The reason ends where the comment (or a comment nested in it) does.
                    rest = re.split(r"\*/|/\*", rest, maxsplit=1)[0]
                at = offset + match.start(1)
                line, column = lines.position(at)
                found.append(
                    Placed(
                        _marker(path, line, column, rest),
                        own_line=previous < line,
                        next_line=following,
                    )
                )
            offset += len(raw) + 1
    return found


def _marker(path: str, line: int, column: int, rest: str) -> Marker:
    text = " ".join(f"{KEYWORD}{rest}".split())
    targets: tuple[tuple[str, str | None], ...] = ()
    tail = rest
    listed = _TARGETS.match(rest)
    if listed is not None:
        targets = _targets(listed.group(1))
        tail = rest[listed.end() :]
    said = _REASON.match(tail)
    reason = said.group(1).strip() if said is not None else None
    problem: str | None = None
    if not targets:
        problem = MARKER_NO_CODES
    elif any(code == CODE_NO_VERDICT for code, _ in targets):
        problem = MARKER_NOT_SUPPRESSIBLE
    elif reason is None or not any(char.isalnum() for char in reason):
        problem = MARKER_NO_REASON
    return Marker(path, line, column, text, targets, reason, problem)


def _targets(listed: str) -> tuple[tuple[str, str | None], ...]:
    """The ``(code, field)`` pairs of a marker's brackets; empty when any one is malformed."""
    targets: list[tuple[str, str | None]] = []
    for part in listed.split(","):
        match = _TARGET.fullmatch(part.strip())
        if match is None:
            return ()
        field = match.group(2).strip() if match.group(2) is not None else None
        targets.append((match.group(1), field or None))
    return tuple(targets)


def attach(
    placed: Sequence[Placed], calls: Sequence[Call], lines: LineIndex
) -> dict[int, tuple[Suppression, ...]]:
    """Each call's index in ``calls`` -> the valid markers covering it, in file order."""
    valid = [
        (entry, suppression)
        for entry in placed
        if (suppression := entry.marker.suppression()) is not None
    ]
    covering: dict[int, tuple[Suppression, ...]] = {}
    if not valid:
        return covering
    for index, call in enumerate(calls):
        first = lines.position(call.offset)[0]
        end = max([call.end, *(link.end for link in call.chain)])
        last = lines.position(max(end - 1, call.offset))[0]
        own = tuple(
            suppression
            for entry, suppression in valid
            if first <= entry.marker.line <= last or (entry.own_line and entry.next_line == first)
        )
        if own:
            covering[index] = own
    return covering


def marker_states(
    found: Sequence[Marker], results: Sequence[CheckResult]
) -> tuple[MarkerState, ...]:
    """Each marker with whether it covers a finding it targets in ``results``.

    Whether it covers one, not whether the finding carries it: a finding
    carries only the first marker covering it, and a second marker on the
    same call (one above it, one trailing) waives it just as well.
    """
    used = {
        (suppression.path, suppression.line, suppression.column)
        for result in results
        for suppression in result.item.suppressions
        if any(suppression.covers(finding) for finding in result.findings)
    }
    return tuple(
        MarkerState(marker, (marker.path, marker.line, marker.column) in used) for marker in found
    )


def label(marker: Marker) -> str:
    """The marker as its targets read: ``tripl-check-ignore[unknown_event,unknown_field:x]``."""
    targets = ",".join(
        code if field is None else f"{code}:{field}" for code, field in marker.targets
    )
    return f"{KEYWORD}[{targets}]"
