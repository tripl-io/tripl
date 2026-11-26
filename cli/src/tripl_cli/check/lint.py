"""The plan-independent lint of event names and keys: ``lint:`` in the check config.

Three rules that need no plan and no server, each a fixed ``warning``, so a
finding fails the run only under ``--strict`` (like ``oversize_value``):

``name_characters``
    An event name, field key or property key with a non-ASCII character (a
    Cyrillic letter in a Latin name, a typographic quote), whitespace, or a
    straight quote. Such a name reads right in review and never matches.
``name_pattern``
    An event name that ``name_pattern`` does not match as a whole, or a field
    or property key that ``key_pattern`` does not.
``duplicate_constant``
    A call's event name or dictionary key read from a constant that holds the
    same value as another constant of the same object (``Events.SHARE_TAP``
    holding ``"save_tap"`` next to ``Events.SAVE_TAP``): one button logs the
    other's event. Only constants that a tracking call of the run reads a name
    or a key from count, grouped by the type or object that declares them; a
    file-scope constant has no object and is never grouped. The finding is on
    every call that reads one of the duplicated constants.

The findings are notes on the items (``CheckItem.notes``), as the size limits'
are (``payloads.enforce_limits``): never sent, merged with the verdict by
``validate``, and suppressed, fingerprinted, baselined and gated like any
other finding. A name's runtime part (``${id}``) is no character of it: the
characters rule reads the literal text around it, and ``name_pattern`` skips
a name that has one, because only the plan's name rule can say what fills it.
Messages write names with ``ascii()``, so the character at fault shows as its
escape (``\\u0430``) instead of looking like the letter it imitates.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import replace

from tripl_cli.check.config import LintConfig
from tripl_cli.check.model import (
    CODE_DUPLICATE_CONSTANT,
    CODE_NAME_CHARACTERS,
    CODE_NAME_PATTERN,
    SEVERITY_WARNING,
    CheckFinding,
    CheckItem,
    ConstantUse,
)

# A runtime part of a name or key, as the scan writes it (``symbols.template_text``).
_HOLE = re.compile(r"\$\{[^{}]*\}")
_QUOTES = frozenset("\"'`")
# The characters one finding names before the rest are only counted.
_NAMED = 3


def lint_items(items: Sequence[CheckItem], lint: LintConfig | None) -> list[CheckItem]:
    """``items``, each with the lint's findings added to its notes; unchanged with no lint.

    The duplicate rule reads every item at once: whether a constant is
    duplicated depends on the other calls of the run.
    """
    if lint is None:
        return list(items)
    twins = _duplicated(items) if lint.duplicates else {}
    return [_lint_item(item, lint, twins) for item in items]


def _lint_item(
    item: CheckItem, lint: LintConfig, twins: dict[tuple[str, ...], tuple[tuple[str, ...], ...]]
) -> CheckItem:
    notes: list[CheckFinding] = []
    if item.name is not None:
        notes.extend(_text_notes(item.name, None, "the event name", lint, lint.name_pattern))
    for key in item.fields:
        notes.extend(_text_notes(key, key, "the field key", lint, lint.key_pattern))
    for key in item.properties or {}:
        notes.extend(_text_notes(str(key), str(key), "the property key", lint, lint.key_pattern))
    for use in dict.fromkeys(item.constants):
        others = twins.get(use.path)
        if others:
            notes.append(_duplicate_note(use, others))
    if not notes:
        return item
    return replace(item, notes=(*item.notes, *notes))


def _text_notes(
    text: str,
    field: str | None,
    what: str,
    lint: LintConfig,
    pattern: re.Pattern[str] | None,
) -> list[CheckFinding]:
    notes: list[CheckFinding] = []
    if lint.characters:
        offenders = _offenders(_HOLE.sub("", text))
        if offenders:
            named = ", ".join(offenders[:_NAMED])
            more = len(offenders) - _NAMED
            if more > 0:
                named += f" and {more} more"
            notes.append(_note(CODE_NAME_CHARACTERS, f"{what} {ascii(text)} has {named}", field))
    if pattern is not None and not _HOLE.search(text) and pattern.fullmatch(text) is None:
        setting = "name_pattern" if field is None else "key_pattern"
        notes.append(
            _note(
                CODE_NAME_PATTERN,
                f"{what} {ascii(text)} does not match {setting} {ascii(pattern.pattern)}",
                field,
            )
        )
    return notes


def _offenders(text: str) -> list[str]:
    """Each distinct character of ``text`` the characters rule flags, described, in order."""
    found: list[str] = []
    for char in dict.fromkeys(text):
        if ord(char) > 0x7F:
            kind = "a non-ASCII character"
        elif char.isspace():
            kind = "whitespace"
        elif char in _QUOTES:
            kind = "a quote"
        else:
            continue
        found.append(f"{kind} U+{ord(char):04X} {unicodedata.name(char, 'UNNAMED')}")
    return found


def _duplicated(
    items: Sequence[CheckItem],
) -> dict[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Each constant a name or key is read from, mapped to its object's others of its value.

    Only a constant with such a twin is in the map.
    """
    by_object: dict[tuple[str, ...], dict[str, set[tuple[str, ...]]]] = {}
    for item in items:
        for use in item.constants:
            if len(use.path) < 2:
                continue  # a file-scope constant: no object to group by
            by_object.setdefault(use.path[:-1], {}).setdefault(use.value, set()).add(use.path)
    twins: dict[tuple[str, ...], tuple[tuple[str, ...], ...]] = {}
    for values in by_object.values():
        for paths in values.values():
            if len(paths) < 2:
                continue
            for path in paths:
                twins[path] = tuple(sorted(other for other in paths if other != path))
    return twins


def _duplicate_note(use: ConstantUse, others: tuple[tuple[str, ...], ...]) -> CheckFinding:
    constant = ".".join(use.path)
    owner = ".".join(use.path[:-1])
    named = ", ".join(".".join(other) for other in others)
    if use.key is None:
        read = "the event name is read from"
        meant = "names the wrong event"
    else:
        read = f"the key {ascii(use.key)} is read from"
        meant = "names the wrong key"
    return _note(
        CODE_DUPLICATE_CONSTANT,
        f"{read} {constant} = {ascii(use.value)}, the same value as {named} of {owner}; "
        f"one of them probably {meant}",
        use.key,
    )


def _note(code: str, message: str, field: str | None) -> CheckFinding:
    return CheckFinding(code=code, severity=SEVERITY_WARNING, message=message, field=field)
