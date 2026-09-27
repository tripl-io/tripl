"""Parse and validate the model's incident summary (F14, #267). Pure.

The model is asked for ``{sentences: [{text, role, facts: [int]}]}``. Whatever
comes back is held to the facts it was given:

* a sentence that cites nothing, or cites an id not in the fact set, is dropped;
* every other role may cite only the fact kinds it describes
  (``ROLE_FACT_KINDS``): a ``release`` sentence cites release facts, a
  ``history`` sentence past verdicts, and so on;
* a sentence that claims a cause ("caused", "because", "due to", ...) is held
  to the ``cause`` rule whatever role the model gave it, and is relabelled
  ``cause``;
* a ``cause`` sentence survives only when it cites a fact that can carry a
  cause (attribution, release, past verdict, note or comment) — the model may
  not promote "what broke" into "why";
* inline ``[n]`` markers are stripped from the text: citations come only from
  the structured list, and the UI renders them.

When no cause sentence survives, a fixed, non-generated sentence says the cause
is unknown, so a summary never reads as if it had found one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from tripl.services.incident_summary_facts import SummaryFact

ROLES: tuple[str, ...] = ("what_broke", "cause", "release", "history", "discussion")
CAUSE_FACT_KINDS: frozenset[str] = frozenset(
    {"attribution", "release", "similar", "note", "comment"}
)
# The fixed unknown-cause line can follow, so a summary shows at most six.
MAX_SENTENCES = 5
MAX_SENTENCE_CHARS = 400
# The fact kinds each non-cause role may cite. ``cause`` may cite any fact but
# needs at least one of ``CAUSE_FACT_KINDS``.
ROLE_FACT_KINDS: dict[str, frozenset[str]] = {
    "what_broke": frozenset({"incident", "scope", "attribution"}),
    "release": frozenset({"release"}),
    "history": frozenset({"similar"}),
    "discussion": frozenset({"note", "comment"}),
}
UNKNOWN_CAUSE_TEXT = "The cause is unknown: no attribution, release or past verdict points to one."

RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "incident_summary",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["sentences"],
            "properties": {
                "sentences": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["text", "role", "facts"],
                        "properties": {
                            "text": {"type": "string"},
                            "role": {"type": "string", "enum": list(ROLES)},
                            "facts": {"type": "array", "items": {"type": "integer"}},
                        },
                    },
                },
            },
        },
    },
}

_MARKER = re.compile(r"\s*\[(\d+(?:\s*,\s*\d+)*)\]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_CAUSAL_CLAIM = re.compile(
    r"\b(?:caus(?:e|ed|es|ing)|because|due to|result(?:ed|s)? (?:of|from)|led to|"
    r"leads? to|triggered|attributed to|driven by|responsible for)\b",
    re.IGNORECASE,
)
# "Checkout broke" is what broke; "Release 4.2 broke checkout" is a cause.
_BROKE = re.compile(r"\b(?:broke|breaks|broken by)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ValidatedSentence:
    text: str
    role: str
    fact_ids: tuple[int, ...]
    generated: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "role": self.role,
            "fact_ids": list(self.fact_ids),
            "generated": self.generated,
        }


@dataclass(frozen=True)
class ValidatedSummary:
    sentences: tuple[ValidatedSentence, ...]
    cause_known: bool


def strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _markers(text: str) -> list[int]:
    ids: list[int] = []
    for match in _MARKER.finditer(text):
        ids.extend(int(part) for part in match.group(1).split(","))
    return ids


def _strip_markers(text: str) -> str:
    return " ".join(_MARKER.sub("", text).split())


def _coerce_ids(raw: object) -> list[int] | None:
    """The cited ids, or None when the list is not a list of integers."""
    if not isinstance(raw, list):
        return None
    ids: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        ids.append(value)
    return ids


def _structured(data: object) -> list[tuple[str, str, list[int] | None]] | None:
    if not isinstance(data, dict):
        return None
    raw = data.get("sentences")
    if not isinstance(raw, list):
        return None
    out: list[tuple[str, str, list[int] | None]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        role = entry.get("role")
        if not isinstance(text, str) or not isinstance(role, str):
            continue
        out.append((text, role, _coerce_ids(entry.get("facts"))))
    return out


def _prose(raw: str) -> list[tuple[str, str, list[int] | None]]:
    return [
        (part, "what_broke", _markers(part))
        for part in _SENTENCE_SPLIT.split(" ".join(raw.split()))
        if part.strip()
    ]


def _claims_cause(text: str, role: str) -> bool:
    if _CAUSAL_CLAIM.search(text):
        return True
    return role != "what_broke" and _BROKE.search(text) is not None


def _checked_role(role: str, text: str, ids: Sequence[int], kinds: dict[int, str]) -> str | None:
    """The role the sentence is kept under, or None when its facts do not
    support it. A causal claim under any role is held to the cause rule."""
    if role != "cause" and _claims_cause(text, role):
        role = "cause"
    if role == "cause":
        supported = any(kinds[fact_id] in CAUSE_FACT_KINDS for fact_id in ids)
        return role if supported else None
    allowed = ROLE_FACT_KINDS[role]
    return role if all(kinds[fact_id] in allowed for fact_id in ids) else None


def _dedupe(ids: Iterable[int]) -> tuple[int, ...]:
    return tuple(dict.fromkeys(ids))


def parse_summary(raw: str | None, facts: Sequence[SummaryFact]) -> ValidatedSummary | None:
    """The validated summary, or None when nothing the model wrote survives."""
    if not raw or not raw.strip():
        return None
    cleaned = strip_markdown_fences(raw)
    candidates: list[tuple[str, str, list[int] | None]] | None
    try:
        candidates = _structured(json.loads(cleaned))
    except ValueError:
        candidates = None
    if candidates is None:
        candidates = _prose(cleaned)

    kinds = {fact.id: fact.kind for fact in facts}
    kept: list[ValidatedSentence] = []
    for text, role, ids in candidates:
        if role not in ROLES or not ids:
            continue
        if any(fact_id not in kinds for fact_id in ids):
            continue
        clean = _strip_markers(text)[:MAX_SENTENCE_CHARS].strip()
        if not clean:
            continue
        checked = _checked_role(role, clean, ids, kinds)
        if checked is None:
            continue
        kept.append(ValidatedSentence(text=clean, role=checked, fact_ids=_dedupe(ids)))
        if len(kept) >= MAX_SENTENCES:
            break
    if not kept:
        return None
    cause_known = any(sentence.role == "cause" for sentence in kept)
    if not cause_known:
        kept.append(
            ValidatedSentence(text=UNKNOWN_CAUSE_TEXT, role="cause", fact_ids=(), generated=False)
        )
    return ValidatedSummary(sentences=tuple(kept), cause_known=cause_known)
