import http.client
import json
from types import SimpleNamespace

import pytest

from tripl.services import ai_service, llm_service


@pytest.mark.parametrize("failure", [OSError("broken read"), http.client.IncompleteRead(b"part")])
def test_post_chat_completions_handles_mid_response_failure(monkeypatch, failure):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            raise failure

    monkeypatch.setattr(
        llm_service._NO_REDIRECT_OPENER, "open", lambda *_args, **_kwargs: Response()
    )
    assert llm_service._post_chat_completions("https://example.test", {}, "key", 1) == (None, None)


def test_post_chat_completions_handles_invalid_utf8(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"\xff"

    monkeypatch.setattr(
        llm_service._NO_REDIRECT_OPENER, "open", lambda *_args, **_kwargs: Response()
    )
    assert llm_service._post_chat_completions("https://example.test", {}, "key", 1) == (None, None)


@pytest.mark.parametrize("body", ["[]", "null", '"answer"', '{"choices": [null]}'])
def test_complete_handles_unexpected_json_shape(monkeypatch, body):
    config = SimpleNamespace(
        ai_enabled=True,
        ai_api_key="key",
        ai_model="test",
        ai_max_output_tokens=100,
        ai_base_url="https://example.test",
        ai_timeout_seconds=1,
        host_guard=False,
    )
    monkeypatch.setattr(llm_service, "_post_chat_completions", lambda *_args: (body, None))
    assert llm_service.complete("system", "user", config=config) is None


@pytest.mark.parametrize("raw", ["[]", "null", '"text"'])
def test_describe_handles_non_object_json(raw):
    response = ai_service._parse_describe_response(raw)
    assert response.description == raw
    assert response.field_suggestions == []


@pytest.mark.asyncio
async def test_ask_plan_keeps_question_in_truncated_prompt(monkeypatch):
    question = "Which event uses the purchase amount?"
    search_result = SimpleNamespace(
        items=[
            SimpleNamespace(
                title="Purchase",
                subtitle=None,
                entity_type="event",
                description="x" * 30_000,
                snippet=None,
                variable_values=None,
                route_path="/events/purchase",
            )
        ],
        semantic_used=False,
    )

    async def fake_search(*_args, **_kwargs):
        return search_result

    async def fake_config(_session, **_kwargs):
        return SimpleNamespace(ask_system_prompt="Answer")

    def fake_complete(_system_prompt, user_prompt, **_kwargs):
        assert question in user_prompt[: llm_service._MAX_USER_PROMPT_CHARS]
        return json.dumps({"answer": "Purchase"})

    monkeypatch.setattr(ai_service.search_service, "search_project", fake_search)
    monkeypatch.setattr(ai_service.app_settings_service, "get_ai_config", fake_config)
    monkeypatch.setattr(ai_service.llm_service, "complete", fake_complete)
    response = await ai_service.ask_plan(None, "project", question, None)
    assert response.sources[0].title == "Purchase"
