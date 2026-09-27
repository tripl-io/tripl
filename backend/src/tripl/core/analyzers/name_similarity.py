"""Name similarity and naming-convention lint for event names (GH #265, F12).

Pure: no database, no network, no LLM. Everything here works on plain strings
so the request path (``services.duplicate_service``) and the sync scan dry run
(``worker.tasks.scan_dry_run``) share one definition of "these two names are
the same event" and "this name breaks the project's convention".

Three layers:

* :func:`normalise` — split a name into lowercase tokens across every spelling
  the product meets: ``snake_case``, ``kebab-case``, ``camelCase``,
  ``PascalCase``, ``Title Case``, rule-shaped ``category:action:label`` and any
  script (``str`` methods are Unicode-aware; nothing assumes ASCII).
* :func:`lexical_score` / :func:`combined_score` — similarity in ``0..1``.
* :func:`infer_convention` / :func:`lint_name` / :func:`suggest` — the naming
  convention a catalog already follows, and what a new name would have to
  change to follow it.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal

from tripl.core.analyzers.name_rules import NameRule
from tripl.core.analyzers.name_vocabulary import (
    AMBIGUOUS_VERBS,
    IRREGULAR_PARTICIPLES,
    VERBS,
)

CaseStyle = Literal["snake", "camel", "pascal", "kebab", "space", "mixed"]
SpaceStyle = Literal["title", "lower", "sentence"]
VerbPosition = Literal["first", "last", "unknown"]
LintCode = Literal["case", "separator", "verb_order", "prefix"]

#: Characters that separate the PARTS of a rule-shaped name
#: (``{category}:{action}:{label}``), as opposed to the words inside a part.
RULE_SEPARATORS: tuple[str, ...] = (":", ".", "/", "|")

#: Words that pad a name without changing which event it is. They are KEPT by
#: :func:`normalise` — every token counts for the plain Jaccard and trigram
#: legs — and set aside only by the filler leg of :func:`lexical_score`, which
#: makes "Paywall View" and "Paywall Screen View" the same event (filler
#: ADDED) but not "Paywall Screen View" and "Paywall Button View" (swapped).
FILLER_TOKENS: frozenset[str] = frozenset(
    {"screen", "page", "button", "btn", "event", "the", "a", "an", "on", "of", "to"}
)

#: Lexical score is capped here when two names differ by a NUMBER on both
#: sides ("step 1" vs "step 2"): the character trigrams of such names are
#: nearly identical, and they are distinct events by construction.
NUMERIC_SUBSTITUTION_CAP = 0.5
#: The filler-insensitive leg never claims certainty: identical-after-filler
#: is strong evidence, not identity.
FILLER_LEG_CAP = 0.95
#: Two names with the same words in a different order ("home to profile" vs
#: "profile to home") are capped here — below the lowest lexical score an
#: embedding can lift to the duplicate threshold — unless the only word that
#: moved is an action verb ("View Paywall" vs "Paywall View").
ORDER_CAP = 0.75
#: Weight of the embedding cosine in :func:`combined_score`.
EMBEDDING_WEIGHT = 0.5

#: Convention inference needs this many names before it says anything.
MIN_CONVENTION_SAMPLE = 5
#: The dominant style must hold this share of the voting names.
MIN_STYLE_SHARE = 0.6
#: A per-type prefix is reported when this share of the type's names start
#: with it — and only from :data:`MIN_CONVENTION_SAMPLE` names up: three names
#: sharing a first word is a coincidence, not a convention.
MIN_PREFIX_SHARE = 0.7
#: Share of all-lowercase names that makes an otherwise silent catalog "snake".
LOWERCASE_SHARE = 0.9

_WORD_SPLIT = re.compile(r"[\W_]+", re.UNICODE)


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def _split_chunk(chunk: str) -> list[str]:
    """Split one separator-free chunk on case and letter/digit boundaries.

    ``paywallView`` -> ``paywall``, ``View``; ``HTTPServer`` -> ``HTTP``,
    ``Server``; ``step1`` -> ``step``, ``1``. Uses ``str.isupper``/``islower``
    so Cyrillic or Greek camel case splits the same way.
    """
    parts: list[str] = []
    current = ""
    for index, char in enumerate(chunk):
        if not current:
            current = char
            continue
        prev = current[-1]
        nxt = chunk[index + 1] if index + 1 < len(chunk) else ""
        # A lone lowercase letter opening the chunk is a brand prefix
        # ("iOS", "iPhone", "eCommerce"), not a word of its own.
        brand = index == 1 and prev.islower()
        boundary = (
            (char.isupper() and (prev.isdigit() or (prev.islower() and not brand)))
            or (char.isupper() and prev.isupper() and nxt.islower())
            or (char.isdigit() != prev.isdigit())
        )
        if boundary:
            parts.append(current)
            current = char
        else:
            current += char
    if current:
        parts.append(current)
    return parts


def display_tokens(name: str) -> tuple[str, ...]:
    """The words of ``name`` in their ORIGINAL spelling (``HTTP``, ``iOS`` kept)."""
    tokens: list[str] = []
    for chunk in _WORD_SPLIT.split(_nfkc(name or "")):
        if chunk:
            tokens.extend(part for part in _split_chunk(chunk) if part)
    return tuple(tokens)


def normalise(name: str) -> tuple[str, ...]:
    """Lowercase tokens of ``name``; every token is kept (no stop words)."""
    return tuple(token.casefold() for token in display_tokens(name))


def normalised_text(name: str) -> str:
    return " ".join(normalise(name))


def _trigrams(text: str) -> set[str]:
    padded = f"  {text} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _dice(a: frozenset[str], b: frozenset[str]) -> float:
    total = len(a) + len(b)
    return 2 * len(a & b) / total if total else 1.0


def _is_verb(token: str, *, strict: bool = False) -> bool:
    """Whether ``token`` (casefolded) is a known verb or an inflection of one.

    ``strict`` leaves out :data:`AMBIGUOUS_VERBS`, the noun-ish ones.
    """
    verbs = VERBS - AMBIGUOUS_VERBS if strict else VERBS
    if token in verbs:
        return True
    for suffix, trims in (("ed", (2, 1)), ("ing", (3,)), ("s", (1,))):
        if (
            token.endswith(suffix)
            and len(token) > len(suffix) + 2
            and any(token[:-trim] in verbs for trim in trims)
        ):
            return True
    return False


def _is_participle(token: str) -> bool:
    if token in IRREGULAR_PARTICIPLES:
        return True
    return token.endswith("ed") and len(token) > 4 and _is_verb(token)


def _same_order(a: Sequence[str], b: Sequence[str]) -> bool:
    """Same words in the same order once movable action verbs are set aside."""

    def fixed(tokens: Sequence[str]) -> list[str]:
        return [t for t in tokens if not (_is_verb(t, strict=True) and not _is_participle(t))]

    return fixed(a) == fixed(b)


@dataclass(frozen=True, slots=True)
class Prepared:
    """One token sequence with everything the scorer derives from it, computed once."""

    tokens: tuple[str, ...]
    token_set: frozenset[str]
    trigrams: frozenset[str]
    core: tuple[str, ...]
    filler: frozenset[str]


def prepare(tokens: Sequence[str]) -> Prepared:
    tokens = tuple(tokens)
    return Prepared(
        tokens=tokens,
        token_set=frozenset(tokens),
        trigrams=frozenset(_trigrams(" ".join(tokens))),
        core=tuple(t for t in tokens if t not in FILLER_TOKENS),
        filler=frozenset(t for t in tokens if t in FILLER_TOKENS),
    )


def _token_score(a: Prepared, b: Prepared) -> float:
    if not a.tokens and not b.tokens:
        return 1.0
    if not a.tokens or not b.tokens:
        return 0.0
    if a.tokens == b.tokens:
        return 1.0
    score = max(_jaccard(a.token_set, b.token_set), _dice(a.trigrams, b.trigrams))
    # Filler leg: the same words once filler is set aside, and one side only
    # ADDS filler ("Paywall View" / "Paywall Screen View"). Swapped filler
    # ("Paywall Screen View" / "Paywall Button View") names two things.
    core_a, core_b = frozenset(a.core), frozenset(b.core)
    if (
        core_a
        and core_a == core_b
        and a.filler != b.filler
        and (a.filler <= b.filler or b.filler <= a.filler)
    ):
        score = max(score, FILLER_LEG_CAP)
    if core_a and core_a == core_b and not _same_order(a.core, b.core):
        score = min(score, ORDER_CAP)
    only_a, only_b = a.token_set - b.token_set, b.token_set - a.token_set
    if any(t.isdigit() for t in only_a) and any(t.isdigit() for t in only_b):
        score = min(score, NUMERIC_SUBSTITUTION_CAP)
    return score


def lexical_score(a: str, b: str) -> float:
    """Similarity of two names in ``0..1``.

    The max of three legs over :func:`normalise` tokens: token Jaccard (case
    and spelling style do not matter), character-trigram Dice (typos, plurals,
    run-together words) and a filler leg (:data:`FILLER_LEG_CAP`) for names
    that are identical except that one ADDS filler words. Then two caps: the
    same words in another order (:data:`ORDER_CAP`, unless only an action verb
    moved) and a number differing on both sides (:data:`NUMERIC_SUBSTITUTION_CAP`).
    """
    return round(_token_score(prepare(normalise(a)), prepare(normalise(b))), 4)


@dataclass(frozen=True, slots=True)
class PreparedName:
    """A name ready to score: whole-name tokens plus its slots under a rule."""

    whole: Prepared
    slots: tuple[Prepared, ...] | None = None


def prepare_name(name: str, rule: NameRule | None = None) -> PreparedName:
    values = rule.split(name) if rule is not None else None
    return PreparedName(
        whole=prepare(normalise(name)),
        slots=None if values is None else tuple(prepare(normalise(v)) for v in values),
    )


def prepared_score(a: PreparedName, b: PreparedName) -> float:
    """:func:`rule_aware_score` over :func:`prepare_name` results."""
    if a.slots is not None and b.slots is not None and len(a.slots) == len(b.slots):
        return round(min(_token_score(x, y) for x, y in zip(a.slots, b.slots, strict=True)), 4)
    return round(_token_score(a.whole, b.whole), 4)


def rule_aware_score(a: str, b: str, rule: NameRule | None = None) -> float:
    """:func:`lexical_score`, slot by slot when both names follow one rule.

    Under a naming rule every slot is part of the identity: ``shop:tap:hat``
    and ``shop:tap:scarf`` are two events that differ in one value, not one
    event spelled twice. So the score is the WEAKEST slot's similarity; when
    either name does not follow the rule the whole names are compared.
    """
    return prepared_score(prepare_name(a, rule), prepare_name(b, rule))


def split_slots(name: str, separators: Sequence[str]) -> list[str]:
    """``name`` split on any of ``separators`` (authored, rule-less names)."""
    if not separators:
        return [name]
    pattern = "|".join(re.escape(sep) for sep in separators)
    return re.split(pattern, name)


def combined_score(lexical: float, embedding: float | None) -> float:
    """Blend lexical similarity with an embedding cosine when there is one.

    ``0.5 * lexical + 0.5 * cosine``, but never below ``lexical``: the stored
    vectors embed a whole search document (name, type, description, fields)
    while a candidate is often a bare name, so the cosine can only ADD
    evidence of a semantic duplicate, never hide a lexical one.
    """
    if embedding is None:
        return round(lexical, 4)
    cosine = max(0.0, min(1.0, embedding))
    blended = (1 - EMBEDDING_WEIGHT) * lexical + EMBEDDING_WEIGHT * cosine
    return round(max(lexical, blended), 4)


# ---------------------------------------------------------------------------
# Convention inference and lint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Convention:
    case: CaseStyle = "mixed"
    space_style: SpaceStyle | None = None
    separator: str | None = None
    verb_position: VerbPosition = "unknown"
    prefix: str | None = None
    confidence: float = 0.0
    sample_size: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _part_case(part: str) -> CaseStyle | None:
    """The spelling of one separator-free part, or None when it cannot vote."""
    part = part.strip()
    if not part:
        return None
    has_upper = any(c.isupper() for c in part)
    if " " in part:
        return "space"
    if "_" in part:
        return "mixed" if has_upper else "snake"
    if "-" in part:
        return "mixed" if has_upper else "kebab"
    if not any(c.isalpha() for c in part):
        return None
    if len(_split_chunk(part)) < 2 or not has_upper:
        return None  # a single lowercase word says nothing about the style
    if part.isupper():
        return None
    return "pascal" if part[0].isupper() else "camel"


def _uses_separators(name: str) -> list[str]:
    return [sep for sep in RULE_SEPARATORS if sep in name]


def detect_case(name: str) -> CaseStyle | None:
    parts = split_slots(name, _uses_separators(name))
    styles = {style for style in (_part_case(p) for p in parts) if style is not None}
    if not styles:
        return None
    return styles.pop() if len(styles) == 1 else "mixed"


def _space_style(name: str) -> SpaceStyle | None:
    words = [w for w in name.split() if any(c.isalpha() for c in w)]
    if len(words) < 2:
        return None
    starts = [w[0].isupper() for w in words if w[0].isalpha()]
    if not starts:
        return None
    if all(starts):
        return "title"
    if not any(starts):
        return "lower"
    if starts[0] and not any(starts[1:]):
        return "sentence"
    return None


def _verb_position(tokens: Sequence[str]) -> VerbPosition | None:
    if len(tokens) < 2:
        return None
    first, last = _is_verb(tokens[0]), _is_verb(tokens[-1])
    if first and not last:
        return "first"
    if last and not first:
        return "last"
    return None


def _first_part(name: str, separator: str | None) -> str:
    if separator and separator in name:
        return name.split(separator, 1)[0]
    tokens = normalise(name)
    return tokens[0] if tokens else ""


def _dominant[T](votes: Iterable[T]) -> tuple[T | None, float, int]:
    counter = Counter(votes)
    total = sum(counter.values())
    if not total:
        return None, 0.0, 0
    value, count = counter.most_common(1)[0]
    return value, count / total, total


def infer_convention(names: Sequence[str]) -> Convention:
    """The naming convention ``names`` follow, with a confidence in ``0..1``.

    ``confidence`` is the dominant case's share of the names that could vote;
    below :data:`MIN_STYLE_SHARE` the case is ``mixed`` and nothing is linted
    for case. ``prefix`` is the first rule part (or first word) that at least
    :data:`MIN_PREFIX_SHARE` of the names share — call this with one event
    type's names for a per-type prefix.
    """
    names = [n for n in names if n and n.strip()]
    sample = len(names)
    if not sample:
        return Convention()

    separator: str | None = None
    for sep in RULE_SEPARATORS:
        if sum(1 for n in names if sep in n) / sample >= MIN_PREFIX_SHARE:
            separator = sep
            break

    case, share, voters = _dominant(c for c in (detect_case(n) for n in names) if c is not None)
    if case is None or share < MIN_STYLE_SHARE or voters < MIN_CONVENTION_SAMPLE:
        case_style: CaseStyle = "mixed"
        # Mostly single lowercase words (``shop:open:tap``) cast no case vote,
        # yet they plainly say "lowercase": spelled as snake_case, which is what
        # a single lowercase word is in every lowercase style.
        lowercase_share = sum(1 for n in names if not any(c.isupper() for c in n)) / sample
        if sample >= MIN_CONVENTION_SAMPLE and lowercase_share >= LOWERCASE_SHARE:
            lower_votes = case in ("snake", "kebab") and share >= MIN_STYLE_SHARE
            case_style = case if lower_votes and case is not None else "snake"
            case, share = case_style, lowercase_share
    else:
        case_style = case
    space_style: SpaceStyle | None = None
    if case_style == "space":
        style, style_share, _ = _dominant(
            s for s in (_space_style(n) for n in names) if s is not None
        )
        space_style = style if style is not None and style_share >= MIN_STYLE_SHARE else None

    verb_names = [n for n in names if not _uses_separators(n)]
    position, verb_share, verb_voters = _dominant(
        p for p in (_verb_position(normalise(n)) for n in verb_names) if p is not None
    )
    verb_position: VerbPosition = (
        position
        if position is not None and verb_share >= MIN_STYLE_SHARE and verb_voters >= 3
        else "unknown"
    )

    prefix: str | None = None
    if sample >= MIN_CONVENTION_SAMPLE:
        firsts = [_first_part(n, separator).casefold() for n in names]
        top, top_share, _ = _dominant(f for f in firsts if f)
        # A leading verb is verb-first ORDER ("Open Cart", "Open Menu"), not a
        # prefix; and a prefix every name IS (single-word names) is not a prefix.
        if (
            top
            and top_share >= MIN_PREFIX_SHARE
            and not any(_is_verb(token) for token in normalise(top)[:1])
            and any(normalised_text(n) != normalised_text(top) for n in names)
        ):
            prefix = top

    return Convention(
        case=case_style,
        space_style=space_style,
        separator=separator,
        verb_position=verb_position,
        prefix=prefix,
        confidence=round(share if case is not None else 0.0, 4),
        sample_size=sample,
    )


def with_prefix(convention: Convention, prefix: str | None) -> Convention:
    """``convention`` with a per-type prefix swapped in."""
    return replace(convention, prefix=prefix)


def _keeps_spelling(token: str) -> bool:
    """An acronym (``HTTP``) or a mixed-case brand (``iOS``) keeps its spelling."""
    if len(token) < 2 or not any(c.isalpha() for c in token):
        return False
    if token.isupper():
        return True
    return any(c.isupper() for c in token[1:]) and any(c.islower() for c in token)


def _render(tokens: Sequence[str], case: CaseStyle, space_style: SpaceStyle | None) -> str:
    """``tokens`` (ORIGINAL spelling, see :func:`display_tokens`) in one style.

    Lowercase styles lowercase everything — an upper-case letter is what they
    forbid; the others keep acronyms and mixed-case words as written.
    """
    tokens = [t for t in tokens if t]
    if not tokens:
        return ""

    def low(token: str) -> str:
        return token if _keeps_spelling(token) else token.lower()

    def cap(token: str) -> str:
        return token if _keeps_spelling(token) else token[:1].upper() + token[1:].lower()

    match case:
        case "snake":
            return "_".join(t.lower() for t in tokens)
        case "kebab":
            return "-".join(t.lower() for t in tokens)
        case "camel":
            return tokens[0].lower() + "".join(cap(t) for t in tokens[1:])
        case "pascal":
            return "".join(cap(t) for t in tokens)
        case "space":
            if space_style == "lower":
                return " ".join(low(t) for t in tokens)
            if space_style == "sentence":
                return " ".join([cap(tokens[0]), *(low(t) for t in tokens[1:])])
            return " ".join(cap(t) for t in tokens)
        case _:
            return " ".join(tokens)


def _target_case(name: str, convention: Convention) -> tuple[CaseStyle, SpaceStyle | None]:
    if convention.case != "mixed" and convention.sample_size >= MIN_CONVENTION_SAMPLE:
        return convention.case, convention.space_style
    own = detect_case(name)
    if own is None or own == "mixed":
        return "space", _space_style(name) or "title"
    return own, _space_style(name) if own == "space" else None


def _movable_verb_position(tokens: Sequence[str]) -> VerbPosition | None:
    """Where the verb of ``tokens`` sits, when the lint may MOVE it.

    Stricter than :func:`_verb_position`: the verb must be unambiguous (not in
    :data:`AMBIGUOUS_VERBS`) and not a participle, and the word at the other
    end must be neither a verb nor a participle — "Signup Done" and "Tutorial
    Completed" describe states, and flipping them helps nobody.
    """
    if len(tokens) < 2:
        return None
    first, last = tokens[0].casefold(), tokens[-1].casefold()

    def movable(token: str) -> bool:
        return _is_verb(token, strict=True) and not _is_participle(token)

    def anchored(token: str) -> bool:
        return not _is_verb(token) and not _is_participle(token)

    if movable(first) and anchored(last):
        return "first"
    if movable(last) and anchored(first):
        return "last"
    return None


def _reorder_verb(tokens: list[str], position: VerbPosition) -> list[str]:
    current = _movable_verb_position(tokens)
    if position == "first" and current == "last":
        return [tokens[-1], *tokens[:-1]]
    if position == "last" and current == "first":
        return [*tokens[1:], tokens[0]]
    return tokens


def _with_prefix_tokens(tokens: list[str], prefix: str) -> list[str]:
    prefix_tokens = list(display_tokens(prefix))
    folded = [t.casefold() for t in tokens[: len(prefix_tokens)]]
    if not prefix_tokens or folded == [t.casefold() for t in prefix_tokens]:
        return tokens
    return prefix_tokens + tokens


def _render_structured(name: str, convention: Convention, *, fix_prefix: bool) -> str:
    separators = _uses_separators(name)
    parts = split_slots(name, separators)
    case, space_style = _target_case(name, convention)
    rendered = [_render(display_tokens(p), case, space_style) if p else "" for p in parts]
    joiner = convention.separator or separators[0]
    if (
        fix_prefix
        and convention.prefix
        and rendered
        and normalised_text(parts[0]) != normalised_text(convention.prefix)
    ):
        rendered.insert(0, _render(display_tokens(convention.prefix), case, space_style))
    return joiner.join(rendered)


def _prefix_ok(name: str, convention: Convention) -> bool:
    if not convention.prefix:
        return True
    if _uses_separators(name):
        separators = _uses_separators(name)
        return normalised_text(split_slots(name, separators)[0]) == normalised_text(
            convention.prefix
        )
    prefix_tokens = normalise(convention.prefix)
    return normalise(name)[: len(prefix_tokens)] == prefix_tokens


def suggest(name: str, convention: Convention) -> str:
    """``name`` rewritten to follow ``convention`` (case, separator, verb, prefix)."""
    if not name or not name.strip():
        return name
    if _uses_separators(name):
        return _render_structured(name, convention, fix_prefix=True)
    tokens = list(display_tokens(name))
    if not tokens:
        return name
    tokens = _reorder_verb(tokens, convention.verb_position)
    if convention.prefix:
        tokens = _with_prefix_tokens(tokens, convention.prefix)
    case, space_style = _target_case(name, convention)
    return _render(tokens, case, space_style)


def _case_matches(name: str, convention: Convention) -> bool:
    if convention.case in ("snake", "kebab") and any(c.isupper() for c in name):
        return False
    own = detect_case(name)
    if own is None:
        return True
    if own != convention.case:
        return False
    if own == "space" and convention.space_style is not None:
        style = _space_style(name)
        return style is None or style == convention.space_style
    return True


_CASE_LABELS: dict[str, str] = {
    "snake": "snake_case",
    "kebab": "kebab-case",
    "camel": "camelCase",
    "pascal": "PascalCase",
    "space": "space-separated words",
}


def lint_name(name: str, convention: Convention) -> list[dict[str, str]]:
    """Every way ``name`` departs from ``convention``, each with its own fix."""
    if not name or not name.strip() or convention.sample_size < MIN_CONVENTION_SAMPLE:
        return []
    issues: list[dict[str, str]] = []
    structured = bool(_uses_separators(name))
    tokens = list(display_tokens(name))

    if convention.case != "mixed" and not _case_matches(name, convention):
        label = _CASE_LABELS.get(convention.case, convention.case)
        if convention.case == "space" and convention.space_style:
            label = f"{convention.space_style}-case words"
        fixed = (
            _render_structured(name, replace(convention, prefix=None), fix_prefix=False)
            if structured
            else _render(tokens, convention.case, convention.space_style)
        )
        if fixed and fixed != name:
            issues.append(
                {
                    "code": "case",
                    "message": f"Names in this project use {label}.",
                    "suggestion": fixed,
                }
            )

    if convention.separator and structured:
        others = [sep for sep in _uses_separators(name) if sep != convention.separator]
        if others:
            fixed = name
            for sep in others:
                fixed = fixed.replace(sep, convention.separator)
            issues.append(
                {
                    "code": "separator",
                    "message": (
                        f"Name parts in this project are separated by '{convention.separator}'."
                    ),
                    "suggestion": fixed,
                }
            )

    if not structured and convention.verb_position != "unknown":
        reordered = _reorder_verb(tokens, convention.verb_position)
        if reordered != tokens:
            case, space_style = _target_case(name, convention)
            where = "first" if convention.verb_position == "first" else "last"
            issues.append(
                {
                    "code": "verb_order",
                    "message": f"Names in this project put the action verb {where}.",
                    "suggestion": _render(reordered, case, space_style),
                }
            )

    if convention.prefix and not _prefix_ok(name, convention):
        if structured:
            fixed = _render_structured(name, convention, fix_prefix=True)
        else:
            case, space_style = _target_case(name, convention)
            fixed = _render(_with_prefix_tokens(tokens, convention.prefix), case, space_style)
        issues.append(
            {
                "code": "prefix",
                "message": f"Names of this event type start with '{convention.prefix}'.",
                "suggestion": fixed,
            }
        )
    return issues
