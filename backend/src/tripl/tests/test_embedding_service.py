"""``embed_texts`` degrading rather than raising, and attributing vectors to the
text that was actually embedded.

The function documents exactly one failure mode — return ``[]`` and let the
caller carry on without embeddings — and the transport errors always honoured
it. A 200 carrying a body the parser cannot use did not: the parse ran outside
the ``try``, so a gateway answering 200 with an HTML error page raised out of the
service and surfaced as a 500 on ``GET /projects/{slug}/search``.

The second half is quieter and worse: the ``data`` array was walked
positionally, so an out-of-order or gapped response wrote one document's vector
onto another document's row — a wrong answer, not a missing one, and nothing
downstream can tell that it happened.
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.request
from dataclasses import replace

import pytest

from tripl.services import embedding_service
from tripl.services.app_settings_service import AiConfig, env_ai_config


def _enabled_config() -> AiConfig:
    return replace(
        env_ai_config(),
        search_embeddings_enabled=True,
        search_embedding_provider="openai",
        search_embedding_api_key="test-key",
    )


def _serve(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    """Answer the provider POST with ``body`` and a 200, as a proxy in front of a
    self-hosted endpoint does when the endpoint behind it is down."""

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return body

    def _fake_urlopen(request: urllib.request.Request, timeout: float | None = None) -> _Response:
        return _Response()

    monkeypatch.setattr(embedding_service.urllib.request, "urlopen", _fake_urlopen)


def _serve_read_failure(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Answer with a 200 whose BODY then fails to arrive, as a connection dropped
    after the status line does. ``urlopen`` has already returned by then, so the
    failure comes out of ``read()`` and not out of the call that opened it."""

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            raise error

    def _fake_urlopen(request: urllib.request.Request, timeout: float | None = None) -> _Response:
        return _Response()

    monkeypatch.setattr(embedding_service.urllib.request, "urlopen", _fake_urlopen)


def test_a_200_with_a_non_json_body_returns_no_embeddings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _serve(monkeypatch, b"<html><body>503 Service Unavailable</body></html>")

    with caplog.at_level(logging.ERROR, logger=embedding_service.logger.name):
        vectors = embedding_service.embed_texts(["hello"], config=_enabled_config())

    assert vectors == []
    # The operator has to be able to tell a malformed response from a provider
    # that simply returned nothing, since both degrade to the same empty list.
    assert "Search embedding response could not be parsed" in caplog.text


def test_a_json_body_that_is_not_an_object_returns_no_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, json.dumps([{"embedding": [0.1, 0.2]}]).encode("utf-8"))

    assert embedding_service.embed_texts(["hello"], config=_enabled_config()) == []


def test_a_vector_carrying_a_non_numeric_element_returns_no_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, json.dumps({"data": [{"embedding": [0.1, None]}]}).encode("utf-8"))

    assert embedding_service.embed_texts(["hello"], config=_enabled_config()) == []


def test_embed_query_degrades_to_an_empty_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    """The search path reads ``embed_query``, not ``embed_texts``, and treats an
    empty vector as "no semantic leg" — so the degrade has to survive the wrapper."""
    _serve(monkeypatch, b"not json at all")

    assert embedding_service.embed_query("hello", config=_enabled_config()) == []


def test_a_body_that_is_not_utf8_returns_no_embeddings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The decode is part of reading the response, not part of trusting it: a
    proxy answering 200 with a gzip frame or a latin-1 error page is the same
    class of failure as an HTML error page and degrades the same way."""
    _serve(monkeypatch, b"\x80\x81\x82 not utf-8")

    with caplog.at_level(logging.ERROR, logger=embedding_service.logger.name):
        vectors = embedding_service.embed_texts(["hello"], config=_enabled_config())

    assert vectors == []
    assert "Search embedding response could not be parsed" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        http.client.IncompleteRead(b"partial"),
        OSError("connection reset by peer"),
    ],
    ids=["incomplete-read", "reset"],
)
def test_a_body_that_never_finishes_arriving_returns_no_embeddings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """These raise out of ``read()`` — inside the ``with``, after the 200 was
    accepted — so they are transport failures that no response-parsing guard
    would ever see."""
    _serve_read_failure(monkeypatch, error)

    with caplog.at_level(logging.ERROR, logger=embedding_service.logger.name):
        vectors = embedding_service.embed_texts(["hello"], config=_enabled_config())

    assert vectors == []
    assert "Search embedding request failed" in caplog.text


def test_vectors_follow_the_providers_index_not_the_array_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OpenAI-compatible ``data`` array carries an ``index`` per item and makes
    no promise about order, so position in the array is not an identity."""
    _serve(
        monkeypatch,
        json.dumps(
            {
                "data": [
                    {"index": 2, "embedding": [0.3]},
                    {"index": 0, "embedding": [0.1]},
                    {"index": 1, "embedding": [0.2]},
                ]
            }
        ).encode("utf-8"),
    )

    vectors = embedding_service.embed_texts(["a", "b", "c"], config=_enabled_config())

    assert vectors == [[0.1], [0.2], [0.3]]


def test_an_unusable_entry_leaves_a_gap_instead_of_shifting_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this pins: skipping the middle entry used to append the third
    vector into the second slot, so document C's embedding was stored on document
    B and every later document was wrong too — silently, since the list still
    looked plausible. An empty vector is the honest answer for B; the worker's
    ``sanitize_embedding`` marks exactly that document failed."""
    _serve(
        monkeypatch,
        json.dumps(
            {
                "data": [
                    {"index": 0, "embedding": [0.1]},
                    {"index": 1, "embedding": "not a vector"},
                    {"index": 2, "embedding": [0.3]},
                ]
            }
        ).encode("utf-8"),
    )

    vectors = embedding_service.embed_texts(["a", "b", "c"], config=_enabled_config())

    assert vectors == [[0.1], [], [0.3]]


def test_a_response_missing_an_entry_entirely_leaves_that_slot_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short response is a gap at the index the provider skipped, not a
    truncation of the tail: the caller pairs the list with ``texts`` by position."""
    _serve(
        monkeypatch,
        json.dumps({"data": [{"index": 1, "embedding": [0.2]}]}).encode("utf-8"),
    )

    assert embedding_service.embed_texts(["a", "b", "c"], config=_enabled_config()) == [
        [],
        [0.2],
        [],
    ]


def test_an_index_free_response_keeps_each_entry_at_its_own_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not every OpenAI-compatible server sends ``index``. Falling back to the
    array position is safe as long as a skipped entry leaves ITS OWN position
    empty rather than pulling the tail forward."""
    _serve(
        monkeypatch,
        json.dumps(
            {"data": [{"embedding": [0.1]}, {"embedding": None}, {"embedding": [0.3]}]}
        ).encode("utf-8"),
    )

    vectors = embedding_service.embed_texts(["a", "b", "c"], config=_enabled_config())

    assert vectors == [[0.1], [], [0.3]]


def test_a_response_with_nothing_usable_returns_no_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response carrying no vector at all says nothing about any individual
    document, so it stays a request-level failure: the worker retries an empty
    list, where a list of gaps would mark every document in the batch failed."""
    _serve(monkeypatch, json.dumps({"data": []}).encode("utf-8"))

    assert embedding_service.embed_texts(["a", "b"], config=_enabled_config()) == []


def test_a_well_formed_response_is_still_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, json.dumps({"data": [{"embedding": [0.1, 0.2]}]}).encode("utf-8"))

    assert embedding_service.embed_texts(["hello"], config=_enabled_config()) == [[0.1, 0.2]]
