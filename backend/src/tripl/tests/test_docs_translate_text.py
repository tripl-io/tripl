"""The text half of docs translations: what reaches the model and what survives it."""

from __future__ import annotations

import json
import re

import pytest

from tripl.services import docs_translate_text as text
from tripl.services.docs_translate_text import TranslationError

NOTE = """---
title: Checkout funnel
description: How we count checkouts
tags: [funnel]
audience: agent
---
# Checkout funnel

Read [[event:checkout_started]] first, then the [guide](https://example.com/a_b).

```sql
select count(*) from events where name = 'checkout'
```

Use `user_id`, never <b>email</b>.
"""


def upper_model(system: str, user: str) -> str:
    """A stand-in model: upper-cases prose, keeps every placeholder, answers JSON as JSON."""
    if user.startswith("{"):
        return json.dumps({key: value.upper() for key, value in json.loads(user).items()})
    return re.sub(r"(⟦T\d+⟧)|[a-z]+", lambda m: m.group(1) or m.group(0).upper(), user)


def test_protected_runs_survive_and_prose_is_translated() -> None:
    out = text.translate_content(NOTE, "funnel.md", "de", upper_model)
    assert "[[event:checkout_started]]" in out
    assert "](https://example.com/a_b)" in out
    assert "select count(*) from events where name = 'checkout'" in out
    assert "`user_id`" in out and "<b>" in out and "</b>" in out
    assert "READ" in out and "FIRST" in out
    # Frontmatter: title and description translated, every other key kept.
    assert "title: CHECKOUT FUNNEL" in out
    assert "description: HOW WE COUNT CHECKOUTS" in out
    assert "audience: agent" in out and "funnel" in out


def test_a_bracket_reference_without_a_kind_is_kept_too() -> None:
    note = "See [[guides/setup]] and [[Setup notes]].\n"
    out = text.translate_content(note, "a.md", "de", upper_model)
    assert out == "SEE [[guides/setup]] AND [[Setup notes]].\n"


def test_a_reply_that_loses_a_placeholder_is_refused() -> None:
    def lossy(system: str, user: str) -> str:
        return user.replace("⟦T0⟧", "")

    with pytest.raises(TranslationError, match="protected text"):
        text.translate_content("See [[event:a]] now.\n", "a.md", "de", lossy)


def test_a_reply_that_repeats_a_placeholder_is_refused() -> None:
    with pytest.raises(TranslationError, match="protected text"):
        text.translate_content("See [[event:a]].\n", "a.md", "de", lambda s, u: u + u)


def test_a_failed_request_and_a_truncated_reply_are_errors() -> None:
    with pytest.raises(TranslationError, match="request failed"):
        text.translate_content("Hello.\n", "a.md", "de", lambda s, u: None)
    long = "word " * 100
    with pytest.raises(TranslationError, match="cut short"):
        text.translate_content(long, "a.md", "de", lambda s, u: "x")


def test_a_reply_wrapped_in_a_code_fence_is_unwrapped() -> None:
    out = text.translate_content("Hello.\n", "a.md", "de", lambda s, u: "```markdown\nHallo.\n```")
    assert out == "Hallo.\n"


def test_a_note_of_code_only_is_not_sent() -> None:
    def never(system: str, user: str) -> str:
        raise AssertionError("nothing to translate")

    body = "```\ncode\n```\n"
    assert text.translate_content(body, "a.md", "de", never) == body


def test_long_notes_go_in_paragraph_chunks_that_join_back() -> None:
    paragraphs = [f"Paragraph {i} " + "word " * 300 for i in range(10)]
    body = "\n\n".join(paragraphs)
    pieces = text.chunks(body, limit=4000)
    assert len(pieces) > 1
    assert "".join(pieces) == body
    assert all(len(piece) <= 4000 for piece in pieces)
    calls: list[str] = []

    def counting(system: str, user: str) -> str:
        calls.append(user)
        return user

    assert text.translate_content(body, "a.md", "de", counting) == body
    assert len(calls) == len(text.chunks(text.protect(body)[0]))


def test_a_paragraph_too_long_for_one_request_is_cut_inside_it() -> None:
    sentences = "This is a sentence about checkouts. " * 400  # ~14k characters, one paragraph
    for body in (sentences, "x" * 9000, "abc ⟦T0⟧ " * 2000, "a⟦T0⟧" * 400):
        pieces = text.chunks(body, limit=1000)
        assert "".join(pieces) == body
        assert all(len(piece) <= 1000 for piece in pieces)
        assert all(piece.count("⟦") == piece.count("⟧") for piece in pieces)


def test_frontmatter_values_keep_their_protected_runs() -> None:
    note = "---\ntitle: Setup\ndescription: See https://example.com/a_b and `run_it`\n---\nBody\n"
    out = text.translate_content(note, "a.md", "de", upper_model)
    assert "description: SEE https://example.com/a_b AND `run_it`" in out


def test_unclosed_fence_is_protected_to_the_end() -> None:
    protected, kept = text.protect("Intro\n```\nnot closed\nstill code")
    assert protected == "Intro\n⟦T0⟧"
    assert kept == ["```\nnot closed\nstill code"]


def test_the_reserved_marker_is_refused() -> None:
    with pytest.raises(TranslationError, match="reserves"):
        text.protect("a ⟦T0⟧ b")


@pytest.mark.parametrize(
    ("raw", "tag"),
    [("en", "en"), ("pt_BR", "pt-br"), (" DE ", "de"), ("zh-Hant", "zh-hant")],
)
def test_two_letter_codes_are_taken_without_the_model(raw: str, tag: str) -> None:
    def never(system: str, user: str) -> str:
        raise AssertionError("no model call for a code")

    assert text.resolve_lang(raw, never) == tag


def test_a_language_name_is_asked_of_the_model() -> None:
    assert text.resolve_lang("немецкий", lambda s, u: '{"code": "de"}') == "de"
    assert text.resolve_lang("rus", lambda s, u: '```json\n{"code": "ru"}\n```') == "ru"
    with pytest.raises(TranslationError, match="not a language"):
        text.resolve_lang("banana", lambda s, u: '{"code": null}')
    with pytest.raises(TranslationError, match="not a language"):
        text.resolve_lang("x", lambda s, u: '{"code": "not a tag!"}')
    with pytest.raises(TranslationError, match="did not answer"):
        text.resolve_lang("German", lambda s, u: None)
