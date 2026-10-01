"""The text half of docs translations: what is sent to the model and what is kept.

Pure functions, no database: :func:`translate_content` takes a note's stored
content and a ``complete(system, user) -> str | None`` callable and returns the
translated content, or raises :class:`TranslationError` with a message safe to
show.

What must survive a translation byte for byte is taken out before the model
sees the text and put back after: fenced code blocks, inline code, ``[[…]]``
links, Markdown link targets, URLs and HTML. Each becomes a placeholder
(``⟦T0⟧``); a reply that drops or repeats one is refused, so a translation can
never break a link or change a query. The frontmatter keeps its keys; only
``title`` and ``description`` are translated.

A long note goes in chunks of whole paragraphs, so the reply fits the model's
output limit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import yaml

from tripl.services.docs_frontmatter import (
    DocContentError,
    load_frontmatter_mapping,
    parse_frontmatter,
    split_frontmatter,
)
from tripl.services.docs_links import LINK_PATTERN

Complete = Callable[[str, str], str | None]

#: A tag the catalog stores: lowercase BCP 47, a 2-3 letter language plus subtags.
_LANG_TAG = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")
MAX_LANG_CHARS = 35
#: Characters of protected text per request; the reply is about as long.
CHUNK_CHARS = 6000
CHUNK_MAX_TOKENS = 8000
_PLACEHOLDER = re.compile(r"⟦T(\d+)⟧")
_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})")
_PROTECTED = re.compile(
    "|".join(
        (
            r"(`+)(?:(?!\1).)+?\1",  # inline code
            LINK_PATTERN.pattern,  # [[kind:target|label]]
            r"\[\[[^\[\]\n]+\]\]",  # any other [[...]], a reference all the same
            r"\]\([^)\s]*(?:\s+\"[^\"\n]*\")?\)",  # a Markdown link's target
            r"<!--.*?-->",  # HTML comment
            r"</?[A-Za-z][^<>\n]*>",  # HTML tag or autolink
            r"https?://[^\s<>()\[\]]+",  # bare URL
        )
    ),
    re.DOTALL,
)


class TranslationError(Exception):
    """A translation that cannot be made; the message is safe to show."""


def normalize_lang(raw: str) -> str | None:
    """``raw`` as a stored tag when it already is one (``pt_BR`` -> ``pt-br``), else None."""
    tag = raw.strip().replace("_", "-").lower()
    if len(tag) > MAX_LANG_CHARS or not _LANG_TAG.fullmatch(tag):
        return None
    return tag


def is_plain_code(raw: str) -> bool:
    """Whether ``raw`` is a two-letter code (plus subtags), taken without asking the model.

    Three letters may be a word or an ISO 639-2 code with a shorter form
    (``rus``), so those go to the model like any other name.
    """
    tag = normalize_lang(raw)
    return tag is not None and len(tag.split("-")[0]) == 2


_LANG_SYSTEM = (
    "The user names a human language in any form: a name in any language, a "
    'code, a misspelling. Reply with a JSON object {"code": TAG} where TAG is '
    "its BCP 47 tag: the ISO 639-1 two-letter code when the language has one, "
    "else ISO 639-3, plus a script or region subtag only when the user asked "
    'for that variant. Reply {"code": null} when it is not a language.'
)


def resolve_lang(raw: str, complete: Complete) -> str:
    """The stored tag for what the user typed: as is when it is a code, else the model's."""
    plain = normalize_lang(raw) if is_plain_code(raw) else None
    if plain is not None:
        return plain
    reply = complete(_LANG_SYSTEM, raw.strip()[:200])
    if reply is None:
        raise TranslationError("The AI provider did not answer; try again or type a code like 'en'")
    try:
        parsed: Any = json.loads(_strip_fence(reply))
    except json.JSONDecodeError as exc:
        raise TranslationError(f"Could not tell which language '{raw}' is") from exc
    code = parsed.get("code") if isinstance(parsed, dict) else None
    tag = normalize_lang(code) if isinstance(code, str) else None
    if tag is None:
        raise TranslationError(f"'{raw}' is not a language the model knows")
    return tag


def _fence_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of the fenced code blocks, fences included (as ``docs_links`` reads them)."""
    spans: list[tuple[int, int]] = []
    offset = 0
    fence: str | None = None
    start = 0
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\n")
        match = _FENCE.match(bare)
        if fence is None:
            if match:
                fence = match.group("fence")
                start = offset
        elif (
            match
            and match.group("fence")[0] == fence[0]
            and len(match.group("fence")) >= len(fence)
            and not bare.strip()[len(match.group("fence")) :].strip()
        ):
            spans.append((start, offset + len(bare)))
            fence = None
        offset += len(line)
    if fence is not None:
        spans.append((start, len(text)))
    return spans


def protect(text: str) -> tuple[str, list[str]]:
    """``text`` with every protected run replaced by ``⟦Tn⟧``, and the runs in order."""
    if "⟦T" in text:
        raise TranslationError("The note contains the text '⟦T', which translation reserves")
    kept: list[str] = []
    out: list[str] = []

    def hold(run: str) -> str:
        kept.append(run)
        return f"⟦T{len(kept) - 1}⟧"

    def prose(segment: str) -> str:
        return _PROTECTED.sub(lambda match: hold(match.group(0)), segment)

    cursor = 0
    for start, end in _fence_spans(text):
        out.append(prose(text[cursor:start]))
        out.append(hold(text[start:end]))
        cursor = end
    out.append(prose(text[cursor:]))
    return "".join(out), kept


def restore(text: str, kept: list[str]) -> str:
    """Put the protected runs back; refuse a reply that lost or repeated one."""
    seen = [int(match.group(1)) for match in _PLACEHOLDER.finditer(text)]
    if sorted(seen) != list(range(len(kept))):
        missing = len(set(range(len(kept))) - set(seen))
        raise TranslationError(
            "The model changed protected text (code, links or URLs)"
            + (f": {missing} missing" if missing else "")
            + "; try again"
        )
    return _PLACEHOLDER.sub(lambda match: kept[int(match.group(1))], text)


#: Where a piece too long for one request is cut, best first: paragraphs, lines,
#: sentences, words. Each keeps its separator, so the pieces join back exactly.
_BREAKS = (
    re.compile(r"(\n{2,})"),
    re.compile(r"(\n)"),
    re.compile(r"(?<=[.!?。！？])(\s+)"),
    re.compile(r"(\s+)"),
)


def _hard_cut(text: str, limit: int) -> list[str]:
    """``text`` in ``limit``-sized pieces, never through a placeholder."""
    out: list[str] = []
    start = 0
    while len(text) - start > limit:
        end = start + limit
        opening = text.rfind("⟦", start, end)
        if opening > start and text.find("⟧", opening) >= end:
            end = opening
        out.append(text[start:end])
        start = end
    out.append(text[start:])
    return out


def chunks(text: str, limit: int = CHUNK_CHARS, level: int = 0) -> list[str]:
    """``text`` cut into pieces of at most ``limit`` characters, between paragraphs
    where it can and inside a long paragraph where it must (lines, then sentences,
    then words). Joining the pieces gives ``text`` back exactly.
    """
    if len(text) <= limit:
        return [text] if text else []
    if level == len(_BREAKS):
        return _hard_cut(text, limit)
    out: list[str] = []
    current = ""
    for part in _BREAKS[level].split(text):
        if current and len(current) + len(part) > limit and part.strip():
            out.append(current)
            current = ""
        current += part
    if current:
        out.append(current)
    return [piece for chunk in out for piece in chunks(chunk, limit, level + 1)]


def _strip_fence(reply: str) -> str:
    """A reply wrapped in a ```markdown fence, unwrapped."""
    stripped = reply.strip()
    match = re.fullmatch(r"```[A-Za-z]*\n(.*)\n```", stripped, re.DOTALL)
    return match.group(1) if match else stripped


def _system_prompt(lang: str) -> str:
    return (
        f"You translate Markdown documentation into the language with the BCP 47 tag "
        f"'{lang}'. Translate the prose only. Each token like ⟦T0⟧ stands for code, a "
        "link or a URL: copy every one exactly once, unchanged, where it belongs in the "
        "sentence. Keep the Markdown structure (headings, lists, tables, emphasis, line "
        "breaks) as it is. Names of events, properties and other identifiers stay as "
        "written. Reply with the translated Markdown only: no notes, no code fence "
        "around it."
    )


def _translate_chunk(piece: str, lang: str, complete: Complete) -> str:
    if not _PLACEHOLDER.sub("", piece).strip():
        return piece  # only whitespace or protected runs: nothing to translate
    reply = complete(_system_prompt(lang), piece)
    if reply is None:
        raise TranslationError(
            "The AI provider request failed; check the AI settings and try again"
        )
    translated = _strip_fence(reply)
    if len(piece.strip()) > 200 and len(translated) < 0.3 * len(piece.strip()):
        raise TranslationError("The model's reply looks cut short; try again")
    # Keep the piece's own leading and trailing whitespace (paragraph breaks).
    lead = piece[: len(piece) - len(piece.lstrip())]
    trail = piece[len(piece.rstrip()) :]
    return lead + translated.strip() + trail


_META_SYSTEM = (
    "Translate the string values of this JSON object into the language with the BCP 47 "
    "tag '{lang}'. Keep the keys. Reply with the JSON object only."
)


def _translate_frontmatter(yaml_text: str, lang: str, complete: Complete) -> str:
    """The block with ``title`` and ``description`` translated; verbatim when it has neither."""
    try:
        meta = load_frontmatter_mapping(yaml_text)
    except DocContentError as exc:
        raise TranslationError(str(exc)) from exc
    wanted = {
        key: value
        for key in ("title", "description")
        if isinstance(value := meta.get(key), str) and value.strip()
    }
    # The same protection as the body: a URL or `code` in a description survives.
    held = {key: protect(value) for key, value in wanted.items()}
    send = {key: text for key, (text, _) in held.items() if _PLACEHOLDER.sub("", text).strip()}
    if not send:
        return yaml_text
    reply = complete(_META_SYSTEM.format(lang=lang), json.dumps(send, ensure_ascii=False))
    if reply is None:
        raise TranslationError(
            "The AI provider request failed; check the AI settings and try again"
        )
    try:
        parsed: Any = json.loads(_strip_fence(reply))
    except json.JSONDecodeError as exc:
        raise TranslationError("The model did not return the title and description") from exc
    if not isinstance(parsed, dict):
        raise TranslationError("The model did not return the title and description")
    updated = dict(meta)
    for key in send:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            updated[key] = restore(value.strip(), held[key][1])
    dumped: str = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=10_000)
    return dumped.rstrip("\n")


def translate_content(content: str, path: str, lang: str, complete: Complete) -> str:
    """The note's content in ``lang``; :class:`TranslationError` when it cannot be made."""
    yaml_text, body = split_frontmatter(content)
    protected, kept = protect(body)
    translated_body = restore(
        "".join(_translate_chunk(piece, lang, complete) for piece in chunks(protected)), kept
    )
    if yaml_text is None:
        result = translated_body
    else:
        front = _translate_frontmatter(yaml_text, lang, complete)
        result = f"---\n{front}\n---\n{translated_body}"
    try:
        parse_frontmatter(result, path)
    except DocContentError as exc:
        raise TranslationError(f"The translated note is not valid: {exc}") from exc
    return result
