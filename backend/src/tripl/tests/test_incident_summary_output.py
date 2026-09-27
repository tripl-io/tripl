"""Incident summary output validation (F14, #267): pure parser tests."""

from __future__ import annotations

import json
from typing import Any

from tripl.services._incident_summary_output import (
    MAX_SENTENCE_CHARS,
    MAX_SENTENCES,
    RESPONSE_FORMAT,
    UNKNOWN_CAUSE_TEXT,
    parse_summary,
)
from tripl.services.incident_summary_facts import SummaryFact

FACTS = (
    SummaryFact(1, "incident", "A drop on Landing Viewed.", "/p/demo-shop/alerting?incident=x"),
    SummaryFact(2, "scope", "Scope Landing Viewed.", "/p/demo-shop/monitoring/event/y"),
    SummaryFact(3, "attribution", "90% of the drop comes from platform = ios.", None),
)


def _reply(*sentences: dict[str, Any]) -> str:
    return json.dumps({"sentences": list(sentences)})


def test_valid_sentences_keep_their_citations_and_lose_inline_markers() -> None:
    parsed = parse_summary(
        _reply(
            {"text": "Landing Viewed dropped [1][2].", "role": "what_broke", "facts": [1, 2]},
            {"text": "iOS explains most of it [3].", "role": "cause", "facts": [3]},
        ),
        FACTS,
    )
    assert parsed is not None
    assert parsed.cause_known is True
    assert [s.text for s in parsed.sentences] == [
        "Landing Viewed dropped.",
        "iOS explains most of it.",
    ]
    assert [s.fact_ids for s in parsed.sentences] == [(1, 2), (3,)]
    assert all(s.generated for s in parsed.sentences)


def test_uncited_and_unknown_id_sentences_are_dropped() -> None:
    parsed = parse_summary(
        _reply(
            {"text": "Kept.", "role": "what_broke", "facts": [1]},
            {"text": "No citation.", "role": "what_broke", "facts": []},
            {"text": "Unknown id.", "role": "history", "facts": [1, 99]},
            {"text": "Bad role.", "role": "speculation", "facts": [1]},
        ),
        FACTS,
    )
    assert parsed is not None
    assert [s.text for s in parsed.sentences if s.generated] == ["Kept."]


def test_cause_without_a_cause_fact_is_dropped_and_unknown_cause_appended() -> None:
    parsed = parse_summary(
        _reply(
            {"text": "It dropped.", "role": "what_broke", "facts": [1]},
            {"text": "Probably a bad deploy.", "role": "cause", "facts": [1, 2]},
        ),
        FACTS,
    )
    assert parsed is not None
    assert parsed.cause_known is False
    last = parsed.sentences[-1]
    assert (last.text, last.role, last.fact_ids, last.generated) == (
        UNKNOWN_CAUSE_TEXT,
        "cause",
        (),
        False,
    )
    assert "bad deploy" not in " ".join(s.text for s in parsed.sentences)


def test_nothing_surviving_returns_none() -> None:
    assert parse_summary(_reply({"text": "x", "role": "cause", "facts": [42]}), FACTS) is None
    assert parse_summary("", FACTS) is None
    assert parse_summary("No markers anywhere.", FACTS) is None


def test_prose_fallback_reads_markers() -> None:
    parsed = parse_summary("Landing Viewed dropped [1]. Nothing else is known.", FACTS)
    assert parsed is not None
    generated = [s for s in parsed.sentences if s.generated]
    assert [(s.text, s.role, s.fact_ids) for s in generated] == [
        ("Landing Viewed dropped.", "what_broke", (1,))
    ]
    assert parsed.cause_known is False


def test_markdown_fences_are_stripped() -> None:
    raw = "```json\n" + _reply({"text": "Fenced.", "role": "what_broke", "facts": [1]}) + "\n```"
    parsed = parse_summary(raw, FACTS)
    assert parsed is not None
    assert parsed.sentences[0].text == "Fenced."


def test_length_and_count_caps() -> None:
    many = [
        {"text": "x" * (MAX_SENTENCE_CHARS + 50), "role": "what_broke", "facts": [1]}
        for _ in range(MAX_SENTENCES + 3)
    ]
    parsed = parse_summary(_reply(*many), FACTS)
    assert parsed is not None
    generated = [s for s in parsed.sentences if s.generated]
    assert len(generated) == MAX_SENTENCES
    assert all(len(s.text) <= MAX_SENTENCE_CHARS for s in generated)
    # With the fixed unknown-cause line, a summary never shows more than six.
    assert len(parsed.sentences) <= 6


def test_release_sentence_citing_only_the_incident_is_dropped() -> None:
    parsed = parse_summary(
        _reply(
            {"text": "It dropped.", "role": "what_broke", "facts": [1]},
            {"text": "Release 4.2 broke checkout.", "role": "release", "facts": [1]},
            {"text": "Seen before.", "role": "history", "facts": [2]},
            {"text": "The team discussed it.", "role": "discussion", "facts": [1]},
        ),
        FACTS,
    )
    assert parsed is not None
    assert [s.text for s in parsed.sentences if s.generated] == ["It dropped."]
    assert parsed.cause_known is False


def test_causal_claim_under_another_role_is_held_to_the_cause_rule() -> None:
    parsed = parse_summary(
        _reply(
            {"text": "It dropped.", "role": "what_broke", "facts": [1]},
            {
                "text": "The drop was caused by the new SDK.",
                "role": "what_broke",
                "facts": [1, 2],
            },
        ),
        FACTS,
    )
    assert parsed is not None
    assert "SDK" not in " ".join(s.text for s in parsed.sentences)
    assert parsed.cause_known is False
    assert parsed.sentences[-1].text == UNKNOWN_CAUSE_TEXT


def test_causal_claim_backed_by_a_cause_fact_is_relabelled_cause() -> None:
    parsed = parse_summary(
        _reply(
            {"text": "The drop is due to platform = ios.", "role": "what_broke", "facts": [3]},
        ),
        FACTS,
    )
    assert parsed is not None
    assert [(s.role, s.fact_ids) for s in parsed.sentences] == [("cause", (3,))]
    assert parsed.cause_known is True


def test_non_integer_citations_are_rejected() -> None:
    parsed = parse_summary(
        _reply(
            {"text": "Strings.", "role": "what_broke", "facts": ["1"]},
            {"text": "Bools.", "role": "what_broke", "facts": [True]},
        ),
        FACTS,
    )
    assert parsed is None


def test_response_format_is_strict_json_schema() -> None:
    assert RESPONSE_FORMAT["type"] == "json_schema"
    schema = RESPONSE_FORMAT["json_schema"]
    assert schema["strict"] is True
    item = schema["schema"]["properties"]["sentences"]["items"]
    assert item["properties"]["role"]["enum"] == [
        "what_broke",
        "cause",
        "release",
        "history",
        "discussion",
    ]
