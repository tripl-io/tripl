"""Shared ``{key}`` grammar for scan event-name formats.

The scan pipeline builds event identities by substituting ``{column}`` /
``{column.json.path}`` placeholders with row values. The API uses this module to
generate the same names from manually entered field values so hand-created
events merge with their scan-generated counterparts.

This is the single definition of that grammar. ``event_generator`` and the
replay path in ``worker/tasks/metrics/generation.py`` used to re-declare the
pattern and keep it in step by a comment here telling all three they "MUST stay
identical"; they now import it (Copilot, PR #74). Keeping one copy is the point
— a placeholder the scan recognises but the reserved-column logic does not is
how production lost a scan for 17 hours.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tripl.json_paths import format_json_path_value

NAME_FORMAT_PATTERN = re.compile(r"\{([^}]+)\}")

# The OTHER grammar: ``${variable}`` references inside a stored field value.
#
# Distinct from ``NAME_FORMAT_PATTERN`` — that one names a warehouse COLUMN at
# scan time, this one names a :class:`~tripl.models.variable.Variable` inside a
# value the catalog stores — but it had drifted into three private capturing
# copies in two spellings: ``_TOKEN_PATTERN`` (``_event_generator_variables``)
# and ``_VARIABLE_NAME_PATTERN`` (``worker.tasks.metrics.generation``) at
# ``[^}]+``, ``_TEMPLATE_TOKEN_PATTERN`` (``event_service``) at ``[^}]*``.
#
# The looser spelling wins: a stray ``${}`` should be *seen* and then fail to
# resolve, not be invisible to the reader that decides whether a variable is
# still referenced. That decision is what made one copy stop being optional —
# ``core.variable_retirement`` deletes rows on the strength of "no stored value
# mentions this token", so it has to extract tokens exactly the way the writers
# of those values do.
#
# Two related patterns are deliberately NOT folded in here, because they answer
# different questions: ``generation._VARIABLE_TEMPLATE_PATTERN`` is the same
# grammar without a capture group (it tests whether a value is templated at all,
# and a capture would change what ``findall`` returns), and
# ``event_service._JSON_TEMPLATE_VALUE_PATTERN`` narrows this body to
# ``[^"\\}\x00-\x1f]+`` for two different reasons, which that file spells out:
# ``}`` is what terminates the token in ``VARIABLE_TOKEN_PATTERN`` here, while a
# quote, a backslash and the C0 control characters are what would break the JSON
# text a token is spliced back into VERBATIM after ``json.dumps``. Everything
# else a scan can write — spaces, commas, non-ASCII — stays saveable.
VARIABLE_TOKEN_PATTERN = re.compile(r"\$\{([^}]*)\}")


def variable_tokens(value: str) -> list[str]:
    """Every ``${token}`` name in *value*, in order, duplicates included.

    The ``"${" not in value`` guard is not decoration: the retirement pass runs
    this over every stored field value in a project, and most of them hold no
    token at all.
    """
    if "${" not in value:
        return []
    return VARIABLE_TOKEN_PATTERN.findall(value)


class NameFormatError(ValueError):
    """An ``event_name_format`` names a key the row cannot supply.

    Authored by tripl, never a driver string: it carries the format's own
    placeholder names and the columns that were available, no host, port or
    library text. ``worker.tasks._errors.user_facing_error`` therefore surfaces
    it verbatim, the same bargain ``core.adapters.errors.WarehouseCapabilityError``
    strikes with the data-source sanitiser.

    It exists because raising a bare ``ValueError`` collapsed the one
    self-diagnosing line ("references unknown keys: action") into "Scan failed
    due to an internal error." for four days of production collection failures
    (root cause of tripl-lpin). It lives in ``core`` because ``core``
    must never import ``worker`` — admitting it to the curated set in
    ``_errors`` keeps the import direction worker → core and means every caller
    of the name-format code gets the behaviour without opting in.

    A ``ValueError`` subclass so existing ``except ValueError`` handlers keep
    working.
    """


def format_keys(fmt: str) -> list[str]:
    return NAME_FORMAT_PATTERN.findall(fmt)


def apply_name_format(fmt: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Substitute ``{key}`` placeholders; returns ``(name, missing_keys)``.

    Missing keys are left in place as literal ``{key}`` text so previews stay
    readable; callers decide whether missing keys are an error.
    """
    missing: list[str] = []

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        missing.append(key)
        return match.group(0)

    return NAME_FORMAT_PATTERN.sub(_substitute, fmt), missing


def resolve_dotted_keys(fmt: str, values_by_field: dict[str, str]) -> dict[str, str]:
    """Field-name values plus ``{col.path}`` keys walked out of JSON values.

    A dotted key like ``page_data.extra.variant`` resolves by parsing the
    ``page_data`` field value as JSON and walking ``extra.variant``, and renders
    the value found there exactly as the scan does. Paths that are missing, and
    values that are unparseable (e.g. still templated with ``${var}``), stay
    unresolved.
    """
    resolved = dict(values_by_field)
    for key in format_keys(fmt):
        if key in resolved or "." not in key:
            continue
        column, path = key.split(".", 1)
        raw = values_by_field.get(column)
        if raw is None:
            continue
        try:
            node: Any = json.loads(raw)
        except ValueError, TypeError:
            continue
        found = True
        for segment in path.split("."):
            if not isinstance(node, dict) or segment not in node:
                found = False
                break
            node = node[segment]
        if not found:
            continue
        # The scan renders a JSON path value with ``format_json_path_value``, so
        # the API must too: ``true``/``false``/``null`` and a container's JSON,
        # never Python's ``True`` or an unresolved key, or a hand-made event and
        # its scanned twin get two different identities.
        resolved[key] = format_json_path_value(node)
    return resolved
