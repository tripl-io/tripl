# ruff: noqa: I001
import json
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from tripl.tests._tenancy import use_public_demo
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.services import llm_service
from tripl.services.app_settings_service import env_ai_config
from tripl.services.ai_service import _parse_describe_response, _strip_markdown_fences

# Importing metrics first initializes the worker task graph in dependency
# order; importing alerts directly would hit the celery_app ↔ metrics ↔ alerts
# import cycle (same pattern as test_alerting.py).
from tripl.worker.tasks import metrics as _metrics  # noqa: F401
from tripl.worker.tasks import alerts as alerts_task


async def _setup_event(client: AsyncClient, slug: str = "ai-proj") -> tuple[str, str]:
    await client.post("/api/v1/projects", json={"name": "AI", "slug": slug})
    et_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    et_id = et_resp.json()["id"]
    await client.post(
        f"/api/v1/projects/{slug}/event-types/{et_id}/fields",
        json={
            "name": "screen",
            "display_name": "Screen",
            "field_type": "string",
        },
    )
    ev_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "Home Page View"},
    )
    return et_id, ev_resp.json()["id"]


# --- status / disabled gating ---


@pytest.mark.asyncio
async def test_ai_status_disabled_by_default(client: AsyncClient):
    await client.post("/api/v1/projects", json={"name": "AI", "slug": "ai-status"})
    resp = await client.get("/api/v1/projects/ai-status/ai/status")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


@pytest.mark.asyncio
async def test_describe_event_returns_503_when_disabled(client: AsyncClient):
    _, event_id = await _setup_event(client, "ai-503")
    resp = await client.post(
        "/api/v1/projects/ai-503/ai/describe-event",
        json={"event_id": event_id},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_ask_returns_503_when_disabled(client: AsyncClient):
    await client.post("/api/v1/projects", json={"name": "AI", "slug": "ai-ask-503"})
    resp = await client.post(
        "/api/v1/projects/ai-ask-503/ai/ask",
        json={"question": "which events exist?"},
    )
    assert resp.status_code == 503


# --- llm_service ---


def test_llm_complete_returns_none_when_disabled():
    assert llm_service.is_enabled(env_ai_config()) is False
    assert llm_service.complete("system", "user", config=env_ai_config()) is None


def test_llm_is_enabled_requires_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm_service.settings, "ai_enabled", True)
    monkeypatch.setattr(llm_service.settings, "ai_api_key", "")
    monkeypatch.setattr(llm_service.settings, "openai_api_key", "")
    assert llm_service.is_enabled(env_ai_config()) is False
    monkeypatch.setattr(llm_service.settings, "ai_api_key", "sk-test")
    assert llm_service.is_enabled(env_ai_config()) is True


def test_a_public_demo_has_no_ai_whatever_the_operator_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    """Every call a visitor triggers would run on the operator's key."""
    monkeypatch.setattr(llm_service.settings, "ai_enabled", True)
    monkeypatch.setattr(llm_service.settings, "ai_api_key", "sk-test")
    monkeypatch.setattr(llm_service.settings, "search_embeddings_enabled", True)
    use_public_demo(monkeypatch)

    config = env_ai_config()

    assert llm_service.is_enabled(config) is False
    assert config.ai_api_key == ""
    assert config.search_embeddings_enabled is False
    assert config.search_embedding_api_key == ""
    assert llm_service.complete("system", "user", config=config) is None


def _success_body(text: str = "ok") -> str:
    return json.dumps({"choices": [{"message": {"content": text}}]})


def test_llm_complete_retries_with_max_completion_tokens(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm_service.settings, "ai_enabled", True)
    monkeypatch.setattr(llm_service.settings, "ai_api_key", "sk-test")
    sent_payloads = []

    def fake_post(url, payload, api_key, timeout):
        sent_payloads.append(json.loads(json.dumps(payload)))
        if "max_tokens" in payload:
            return None, {"code": "unsupported_parameter", "param": "max_tokens"}
        if "temperature" in payload:
            return None, {"code": "unsupported_value", "param": "temperature"}
        return _success_body("described"), None

    monkeypatch.setattr(llm_service, "_post_chat_completions", fake_post)
    assert (
        llm_service.complete("system", "user", max_tokens=50, config=env_ai_config()) == "described"
    )
    assert len(sent_payloads) == 3
    assert "max_tokens" not in sent_payloads[1]
    assert sent_payloads[1]["max_completion_tokens"] == 50
    assert "temperature" not in sent_payloads[2]


def test_llm_complete_drops_response_format_a_server_does_not_support(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(llm_service.settings, "ai_enabled", True)
    monkeypatch.setattr(llm_service.settings, "ai_api_key", "sk-test")
    sent_payloads = []

    def fake_post(url, payload, api_key, timeout):
        sent_payloads.append(json.loads(json.dumps(payload)))
        for param in ("max_tokens", "temperature", "response_format"):
            if param in payload:
                return None, {"code": "unsupported_parameter", "param": param}
        return _success_body("described"), None

    monkeypatch.setattr(llm_service, "_post_chat_completions", fake_post)
    result = llm_service.complete(
        "system",
        "user",
        max_tokens=50,
        response_format={"type": "json_object"},
        config=env_ai_config(),
    )
    assert result == "described"
    assert sent_payloads[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in sent_payloads[-1]
    assert len(sent_payloads) == 4


def test_llm_complete_returns_none_on_non_retryable_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm_service.settings, "ai_enabled", True)
    monkeypatch.setattr(llm_service.settings, "ai_api_key", "sk-test")
    calls = []

    def fake_post(url, payload, api_key, timeout):
        calls.append(payload)
        return None, {"code": "invalid_api_key", "param": None}

    monkeypatch.setattr(llm_service, "_post_chat_completions", fake_post)
    assert llm_service.complete("system", "user", config=env_ai_config()) is None
    assert len(calls) == 1


# --- describe response parsing ---


def test_strip_markdown_fences_plain_passthrough():
    assert _strip_markdown_fences('{"a": 1}') == '{"a": 1}'


def test_strip_markdown_fences_removes_json_fence():
    fenced = '```json\n{"a": 1}\n```'
    assert _strip_markdown_fences(fenced) == '{"a": 1}'


def test_parse_describe_response_valid_json():
    raw = json.dumps(
        {
            "description": "Fires when the home page renders.",
            "field_suggestions": [{"field_name": "screen", "description": "Screen slug."}],
        }
    )
    parsed = _parse_describe_response(raw)
    assert parsed.description == "Fires when the home page renders."
    assert len(parsed.field_suggestions) == 1
    assert parsed.field_suggestions[0].field_name == "screen"


def test_parse_describe_response_invalid_json_falls_back_to_raw_text():
    parsed = _parse_describe_response("Just a plain sentence.")
    assert parsed.description == "Just a plain sentence."
    assert parsed.field_suggestions == []


def test_parse_describe_response_skips_malformed_suggestions():
    raw = json.dumps({"description": "d", "field_suggestions": ["oops", {"field_name": "x"}]})
    parsed = _parse_describe_response(raw)
    assert [s.field_name for s in parsed.field_suggestions] == ["x"]


# --- enabled API paths (LLM mocked) ---


def _enable_env_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_service.settings, "ai_enabled", True)
    monkeypatch.setattr(llm_service.settings, "ai_api_key", "sk-test")
    monkeypatch.setattr(llm_service.settings, "openai_api_key", "")


@pytest.mark.asyncio
async def test_describe_event_returns_suggestion(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    _, event_id = await _setup_event(client, "ai-describe")
    captured: dict[str, object] = {}

    def fake_complete(system_prompt: str, user_prompt: str, **kwargs: object) -> str:
        captured["user_prompt"] = user_prompt
        captured["response_format"] = kwargs.get("response_format")
        return json.dumps(
            {
                "description": "Home page render event.",
                "field_suggestions": [{"field_name": "screen", "description": "Screen slug."}],
            }
        )

    _enable_env_ai(monkeypatch)
    monkeypatch.setattr("tripl.services.llm_service.complete", fake_complete)

    resp = await client.post(
        "/api/v1/projects/ai-describe/ai/describe-event",
        json={"event_id": event_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "Home page render event."
    assert data["field_suggestions"] == [{"field_name": "screen", "description": "Screen slug."}]
    # The prompt carries the event identity and its fields.
    assert "Home Page View" in str(captured["user_prompt"])
    assert "screen" in str(captured["user_prompt"])
    # The answer is requested as structured output, not only asked for in prose.
    response_format = captured["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"


@pytest.mark.asyncio
async def test_describe_event_404_for_unknown_event(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    await _setup_event(client, "ai-404")
    _enable_env_ai(monkeypatch)
    resp = await client.post(
        "/api/v1/projects/ai-404/ai/describe-event",
        json={"event_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_describe_event_type_returns_suggestion(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    et_id, _ = await _setup_event(client, "ai-describe-et")
    _enable_env_ai(monkeypatch)
    monkeypatch.setattr(
        "tripl.services.llm_service.complete",
        lambda *a, **k: json.dumps({"description": "Page view family.", "field_suggestions": []}),
    )
    resp = await client.post(
        "/api/v1/projects/ai-describe-et/ai/describe-event-type",
        json={"event_type_id": et_id},
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == "Page view family."


@pytest.mark.asyncio
async def test_ask_returns_answer_with_sources(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    await _setup_event(client, "ai-ask")
    # Reindex so the search context has documents to cite.
    await client.post("/api/v1/projects/ai-ask/search/reindex")
    _enable_env_ai(monkeypatch)
    monkeypatch.setattr(
        "tripl.services.llm_service.complete",
        lambda *a, **k: "Home Page View fires on home render [1].",
    )
    resp = await client.post(
        "/api/v1/projects/ai-ask/ai/ask",
        json={"question": "what page view events exist?"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "Home Page View" in data["answer"]
    assert isinstance(data["sources"], list)
    assert all({"title", "entity_type", "route_path"} <= set(s) for s in data["sources"])


@pytest.mark.asyncio
async def test_ask_degrades_gracefully_when_llm_returns_none(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    await _setup_event(client, "ai-ask-degrade")
    _enable_env_ai(monkeypatch)
    monkeypatch.setattr("tripl.services.llm_service.complete", lambda *a, **k: None)
    resp = await client.post(
        "/api/v1/projects/ai-ask-degrade/ai/ask",
        json={"question": "what events exist?"},
    )
    assert resp.status_code == 200
    assert "unavailable" in resp.json()["answer"]


# --- worker alert explanation ---


def _delivery_with_item() -> AlertDelivery:
    delivery = AlertDelivery(
        project_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        rule_id=uuid.uuid4(),
        matched_count=1,
    )
    item = AlertDeliveryItem(
        id=uuid.uuid4(),
        scope_type="event",
        scope_ref=str(uuid.uuid4()),
        scope_name="Home Page View",
        bucket=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
        direction="drop",
        actual_count=10,
        expected_count=100,
        absolute_delta=-90,
        percent_delta=-90.0,
    )
    delivery.items = [item]
    return delivery


def test_build_ai_explanation_returns_none_when_disabled():
    delivery = _delivery_with_item()
    result = alerts_task._build_ai_explanation(
        delivery,
        scan_name="main",
        project_name="AI",
        item_context_cache={},
    )
    assert result is None


def test_build_ai_explanation_includes_item_context(monkeypatch: pytest.MonkeyPatch):
    delivery = _delivery_with_item()
    item = delivery.items[0]
    captured: dict[str, str] = {}

    def fake_complete(system_prompt: str, user_prompt: str, **kwargs: object) -> str:
        captured["user_prompt"] = user_prompt
        return "The drop is isolated to the home screen."

    monkeypatch.setattr("tripl.services.llm_service.is_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr("tripl.services.llm_service.complete", fake_complete)

    result = alerts_task._build_ai_explanation(
        delivery,
        scan_name="main",
        project_name="AI",
        item_context_cache={item.id: ("▁▂▃", "platform=ios −90%")},
    )
    assert result == "The drop is isolated to the home screen."
    assert "Home Page View" in captured["user_prompt"]
    assert "drop" in captured["user_prompt"]
    assert "▁▂▃" in captured["user_prompt"]
    assert "platform=ios" in captured["user_prompt"]


def _capture_prompt(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    captured: dict[str, str] = {}

    def fake_complete(system_prompt: str, user_prompt: str, **kwargs: object) -> str:
        captured["user_prompt"] = user_prompt
        return "explained"

    monkeypatch.setattr("tripl.services.llm_service.is_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr("tripl.services.llm_service.complete", fake_complete)
    return captured


def test_build_ai_explanation_describes_a_value_drift_as_new_values(
    monkeypatch: pytest.MonkeyPatch,
):
    """A value drift's row says "spike", actual = how many new values, expected 0.

    Quoted as a volume line, that was "spike, actual 2 vs expected 0", and the
    note the model wrote from it explained a rise in traffic.
    """
    captured = _capture_prompt(monkeypatch)
    delivery = _delivery_with_item()
    item = delivery.items[0]
    item.scope_type = "variable_value_drift"
    item.scope_name = "checkout:completed.tier"
    item.direction = "spike"
    item.actual_count = 2
    item.expected_count = 0
    item.drift_field = "tier"
    item.sample_value = "platinum, diamond"

    alerts_task._build_ai_explanation(
        delivery,
        scan_name="main",
        project_name="AI",
        item_context_cache={},
    )

    assert (
        "- [value drift] checkout:completed.tier: ${tier} observed platinum, diamond"
        in captured["user_prompt"]
    )
    assert "spike" not in captured["user_prompt"]


def test_build_ai_explanation_marks_items_that_co_fired(monkeypatch: pytest.MonkeyPatch):
    """Two scopes moving together on one bucket is context the model needs.

    The tag used to be derived from the correlation id, which is now the
    per-scope inbox handle — every item carries its own, so counting group
    members would find one member each and the tag would quietly never appear.
    """
    captured = _capture_prompt(monkeypatch)
    delivery = _delivery_with_item()
    peer = AlertDeliveryItem(
        id=uuid.uuid4(),
        scope_type="event",
        scope_ref=str(uuid.uuid4()),
        scope_name="Checkout Started",
        bucket=delivery.items[0].bucket,
        direction="drop",
        actual_count=5,
        expected_count=80,
        absolute_delta=-75,
        percent_delta=-93.75,
        correlation_group_id=uuid.uuid4(),
    )
    delivery.items[0].correlation_group_id = uuid.uuid4()
    delivery.items = [delivery.items[0], peer]

    alerts_task._build_ai_explanation(
        delivery,
        scan_name="main",
        project_name="AI",
        item_context_cache={},
    )

    assert captured["user_prompt"].count("[co-fired with other items]") == 2


def test_build_ai_explanation_does_not_claim_a_lone_item_co_fired(
    monkeypatch: pytest.MonkeyPatch,
):
    """One scope in the delivery has nobody to have fired with."""
    captured = _capture_prompt(monkeypatch)
    delivery = _delivery_with_item()
    delivery.items[0].correlation_group_id = uuid.uuid4()

    alerts_task._build_ai_explanation(
        delivery,
        scan_name="main",
        project_name="AI",
        item_context_cache={},
    )

    assert "co-fired" not in captured["user_prompt"]


def test_build_ai_explanation_does_not_tell_the_model_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
):
    """A zero baseline has no percentage, and "+0%" reads as "no movement".

    The note the model writes from this prompt is what the reader receives, so
    the prompt must not describe a scope that went from nothing to 137 as a 0%
    change.
    """
    captured = _capture_prompt(monkeypatch)
    delivery = _delivery_with_item()
    item = delivery.items[0]
    item.direction = "spike"
    item.actual_count = 137
    item.expected_count = 0
    item.absolute_delta = 137
    item.percent_delta = 0.0

    alerts_task._build_ai_explanation(
        delivery,
        scan_name="main",
        project_name="AI",
        item_context_cache={},
    )

    assert "(+0%)" not in captured["user_prompt"]
    assert "no baseline" in captured["user_prompt"]
    assert "actual 137 vs expected 0" in captured["user_prompt"]


def test_build_ai_explanation_swallows_llm_errors(monkeypatch: pytest.MonkeyPatch):
    delivery = _delivery_with_item()
    monkeypatch.setattr("tripl.services.llm_service.is_enabled", lambda *_a, **_k: True)

    def boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr("tripl.services.llm_service.complete", boom)
    result = alerts_task._build_ai_explanation(
        delivery,
        scan_name="main",
        project_name="AI",
        item_context_cache={},
    )
    assert result is None


def test_append_ai_explanation_escapes_for_format():
    text = alerts_task._append_ai_explanation("body", "a<b & c", "telegram_html")
    assert text.startswith("body\n\nAI: ")
    assert "<b" not in text.split("AI: ", 1)[1]


# --- alert explanation remembers what it already sent ---


def _sync_alert_session(tmp_path):
    """File-backed SQLite session with just the alerting tables created."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tripl.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/alerts.db")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _past_delivery(rule_id, scope_ref, *, status, sent_at, said, scope_name="Home Page View"):
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        destination_id=uuid.uuid4(),
        rule_id=rule_id,
        matched_count=1,
        status=status,
        channel="slack",
        sent_at=sent_at,
        payload_snapshot={"ai_explanation": said} if said else {},
    )
    delivery.items = [
        AlertDeliveryItem(
            id=uuid.uuid4(),
            scope_type="event",
            scope_ref=scope_ref,
            scope_name=scope_name,
            bucket=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
            direction="drop",
            actual_count=20,
            expected_count=100,
            absolute_delta=-80,
            percent_delta=-80.0,
        )
    ]
    return delivery


def test_recent_alert_history_recalls_only_what_was_really_sent(tmp_path):
    """The prompt must not claim to have told the reader something it never sent."""
    from tripl.worker.tasks import alerts_messages

    factory = _sync_alert_session(tmp_path)
    current = _delivery_with_item()
    scope_ref = current.items[0].scope_ref
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)

    with factory() as session:
        session.add_all(
            [
                _past_delivery(
                    current.rule_id,
                    scope_ref,
                    status="sent",
                    sent_at=datetime(2026, 6, 10, 9, 0, tzinfo=UTC),
                    said="Checkout dropped after the 4.2 rollout.",
                ),
                # Never reached anyone, so recalling it would be a lie.
                _past_delivery(
                    current.rule_id,
                    scope_ref,
                    status="failed",
                    sent_at=datetime(2026, 6, 10, 10, 0, tzinfo=UTC),
                    said="This one never went out.",
                ),
                # A different event under the same rule is not this event's history.
                _past_delivery(
                    current.rule_id,
                    str(uuid.uuid4()),
                    status="sent",
                    sent_at=datetime(2026, 6, 10, 11, 0, tzinfo=UTC),
                    said="Unrelated scope.",
                    scope_name="Other Event",
                ),
                # Older than the window.
                _past_delivery(
                    current.rule_id,
                    scope_ref,
                    status="sent",
                    sent_at=datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
                    said="Ancient history.",
                ),
            ]
        )
        session.commit()

        lines = alerts_messages._recent_alert_history(session, current, now=now)

    joined = "\n".join(lines)
    assert len(lines) == 1, joined
    assert "Checkout dropped after the 4.2 rollout." in joined
    assert "3h ago" in joined
    assert "This one never went out." not in joined
    assert "Unrelated scope." not in joined
    assert "Ancient history." not in joined


def test_build_ai_explanation_tells_the_model_what_it_already_said(
    monkeypatch: pytest.MonkeyPatch,
):
    """Same signal, second alert: the prompt carries the earlier note."""
    from tripl.worker.tasks import alerts_messages

    delivery = _delivery_with_item()
    captured: dict[str, str] = {}

    def fake_complete(system_prompt: str, user_prompt: str, **kwargs: object) -> str:
        captured["user_prompt"] = user_prompt
        return "Still falling, now 90% below expected."

    monkeypatch.setattr("tripl.services.llm_service.is_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr("tripl.services.llm_service.complete", fake_complete)
    monkeypatch.setattr(
        alerts_messages,
        "_recent_alert_history",
        lambda session, d, *, now: ['- 3h ago. You already told them: "Dropped after 4.2."'],
    )

    result = alerts_task._build_ai_explanation(
        delivery,
        scan_name="main",
        project_name="AI",
        item_context_cache={},
        session=object(),
    )

    assert result == "Still falling, now 90% below expected."
    prompt = captured["user_prompt"]
    assert "Dropped after 4.2." in prompt
    assert "do not repeat what the reader already read" in prompt


def test_build_ai_explanation_without_a_session_is_unchanged(monkeypatch: pytest.MonkeyPatch):
    """A first-ever alert has no history block to carry."""
    delivery = _delivery_with_item()
    captured: dict[str, str] = {}

    def fake_complete(system_prompt: str, user_prompt: str, **kwargs: object) -> str:
        captured["user_prompt"] = user_prompt
        return "First time."

    monkeypatch.setattr("tripl.services.llm_service.is_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr("tripl.services.llm_service.complete", fake_complete)

    assert (
        alerts_task._build_ai_explanation(
            delivery, scan_name="main", project_name="AI", item_context_cache={}
        )
        == "First time."
    )
    assert "Previously sent" not in captured["user_prompt"]


# --- non-OpenAI providers behind their OpenAI-compatible endpoints ---


@pytest.mark.parametrize(
    "error",
    [
        # Anthropic: no code/param, the refusal is in the message.
        {"type": "invalid_request_error", "message": "response_format: not supported"},
        # Gemini: an INVALID_ARGUMENT status with the field named in the message.
        {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "Invalid JSON payload: unknown name response_format",
        },
    ],
)
def test_llm_complete_drops_a_parameter_a_provider_refuses_in_words(
    monkeypatch: pytest.MonkeyPatch, error: dict[str, object]
):
    monkeypatch.setattr(llm_service.settings, "ai_enabled", True)
    monkeypatch.setattr(llm_service.settings, "ai_api_key", "sk-test")
    sent = []

    def fake_post(url, payload, api_key, timeout):
        sent.append(dict(payload))
        if "response_format" in payload:
            return None, error
        return _success_body("ok"), None

    monkeypatch.setattr(llm_service, "_post_chat_completions", fake_post)
    result = llm_service.complete(
        "system", "user", response_format={"type": "json_object"}, config=env_ai_config()
    )
    assert result == "ok"
    assert len(sent) == 2
    assert "response_format" not in sent[1]


def test_llm_complete_does_not_retry_an_error_that_names_no_refused_parameter(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(llm_service.settings, "ai_enabled", True)
    monkeypatch.setattr(llm_service.settings, "ai_api_key", "sk-test")
    calls = []

    def fake_post(url, payload, api_key, timeout):
        calls.append(payload)
        return None, {"type": "authentication_error", "message": "invalid x-api-key"}

    monkeypatch.setattr(llm_service, "_post_chat_completions", fake_post)
    assert llm_service.complete("system", "user", config=env_ai_config()) is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"type": "error", "error": {"type": "invalid_request_error", "message": "m"}}', "m"),
        ('[{"error": {"code": 400, "message": "g"}}]', "g"),
        ('{"error": {"code": "unsupported_parameter", "message": "o"}}', "o"),
        ("not json", None),
    ],
)
def test_provider_errors_are_unwrapped_from_each_shape(body: str, expected: str | None):
    import contextlib

    error = {}
    with contextlib.suppress(json.JSONDecodeError):
        error = llm_service._provider_error(json.loads(body))
    assert error.get("message") == expected


def test_openrouter_requests_carry_its_attribution_headers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm_service.settings, "app_base_url", "https://tripl.example.com")
    headers = llm_service._provider_headers("https://openrouter.ai/api/v1/chat/completions")
    assert headers == {"X-Title": "tripl", "HTTP-Referer": "https://tripl.example.com"}
    assert llm_service._provider_headers("https://api.openai.com/v1/chat/completions") == {}
    assert llm_service._provider_headers("https://openrouter.ai.evil.test/v1") == {}
