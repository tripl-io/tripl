"""Whole-identifier matching inside free SQL, for "possible" dependency edges.

A SQL metric's ``metric_sql``, a fact table's rowset ``sql``, its named row
filters and an operand's ``filter_sql`` all name columns as plain text. There is
no parse tree to ask, so the dependency graph can only say a column is
*possibly* read when its name appears as an identifier. Such an edge is
labelled ``possible`` and never blocks anything (GH #257).

The read-only SQL checker in ``core.adapters.measure_validator`` does not
tokenize: it masks the INSIDE of every quoted span (``_mask_quoted_spans``),
double-quoted and backticked identifiers included, which is right for a safety
gate and wrong here, where a quoted identifier is exactly what must still
match. So this scanner uses the same quote characters but keeps identifier
quotes: ``"x"`` and ```x``` are identifiers and kept, ``--`` and ``/* */``
comments are skipped. (``#`` is not a comment here: it is MySQL-only and shows
up inside JSON paths and ``#legacySQL`` headers.)

Single-quoted text is a literal, not an identifier, but a JSON accessor names
a column through one: ``JSON_VALUE(payload, '$.user_id')``,
``payload->>'user_id'``, ``get_json_object(p, '$.a.user_id')``. So a literal
also counts as a (still ``possible``) mention when, case-insensitively, it
equals the name, equals ``'$.' + name`` or ends with ``'.' + name``.

Matching is case-insensitive and whole-identifier: ``user_id`` does not match
``user_id_hash``. A dotted chain (``payload.user.id``) yields every contiguous
sub-chain, so a field named ``user.id`` or ``id`` both match.
"""

from __future__ import annotations

from functools import lru_cache

__all__ = ["sql_identifiers", "sql_literals", "sql_mentions"]

# The identifier half of ``measure_validator._QUOTE_CHARS`` (``'`` is the
# literal delimiter there and here).
_IDENTIFIER_QUOTES = frozenset({'"', "`"})


def _is_start(char: str) -> bool:
    return char.isalpha() or char == "_"


def _is_part(char: str) -> bool:
    return char.isalnum() or char in "_$"


def _scan(sql: str) -> tuple[list[list[str]], list[str]]:
    """Every dotted identifier chain in ``sql`` (as its parts) and every literal."""
    chains: list[list[str]] = []
    literals: list[str] = []
    current: list[str] = []
    expect_part = False  # just consumed a "." after an identifier
    i = 0
    length = len(sql)

    def close() -> None:
        nonlocal current
        if current:
            chains.append(current)
        current = []

    while i < length:
        char = sql[i]
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            i = length if end == -1 else end
            close()
            expect_part = False
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = length if end == -1 else end + 2
            close()
            expect_part = False
            continue
        if char == "'":
            i += 1
            text: list[str] = []
            while i < length:
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        text.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                if sql[i] == "\\" and i + 1 < length:
                    text.append(sql[i + 1])
                    i += 2
                    continue
                text.append(sql[i])
                i += 1
            literals.append("".join(text))
            close()
            expect_part = False
            continue
        if char in _IDENTIFIER_QUOTES:
            end = sql.find(char, i + 1)
            if end == -1:
                break
            quoted = sql[i + 1 : end]
            i = end + 1
            if not expect_part:
                close()
            # A backticked BigQuery path (`project.dataset.table`) is one quote
            # around a dotted chain.
            current.extend(part for part in quoted.split(".") if part)
            expect_part = False
            continue
        if _is_start(char):
            start = i
            while i < length and _is_part(sql[i]):
                i += 1
            if not expect_part:
                close()
            current.append(sql[start:i])
            expect_part = False
            continue
        if char == "." and current:
            expect_part = True
            i += 1
            continue
        close()
        expect_part = False
        i += 1
    close()
    return chains, literals


@lru_cache(maxsize=512)
def _scanned(sql: str) -> tuple[list[list[str]], list[str]]:
    return _scan(sql)


@lru_cache(maxsize=512)
def sql_literals(sql: str) -> frozenset[str]:
    """Every single-quoted literal in ``sql``, unescaped and lower-cased."""
    return frozenset(literal.strip().lower() for literal in _scanned(sql)[1])


@lru_cache(maxsize=512)
def sql_identifiers(sql: str) -> frozenset[str]:
    """Every identifier and dotted sub-chain in ``sql``, lower-cased."""
    found: set[str] = set()
    for parts in _scanned(sql)[0]:
        lowered = [part.lower() for part in parts]
        for start in range(len(lowered)):
            for end in range(start + 1, len(lowered) + 1):
                found.add(".".join(lowered[start:end]))
    return frozenset(found)


def _literal_names(literal: str, wanted: str) -> bool:
    return literal == wanted or literal == f"$.{wanted}" or literal.endswith(f".{wanted}")


def sql_mentions(sql: str | None, name: str) -> bool:
    """Whether ``name`` appears in ``sql`` (case-insensitive).

    As a whole identifier, or as a single-quoted literal that equals it,
    equals ``$.<name>`` or ends with ``.<name>`` (a JSON path).
    """
    if not sql or not name:
        return False
    wanted = name.strip().lower()
    if not wanted:
        return False
    if wanted in sql_identifiers(sql):
        return True
    return any(_literal_names(literal, wanted) for literal in sql_literals(sql))
