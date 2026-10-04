"""Built-in default prompts for AI features.

These are the fallbacks used when no service-wide override is stored in
``app_settings`` (see :mod:`tripl.services.app_settings_service`). Kept in a
dependency-free module so both API services and Celery tasks can import them
without circular imports.
"""

from __future__ import annotations

DEFAULT_DESCRIBE_SYSTEM_PROMPT = (
    "You are a technical documentation assistant for a product analytics tracking plan. "
    "Given event metadata, write clear, concise descriptions. "
    "Return the event description and, in field_suggestions, a description for "
    "each field whose description is empty or missing. "
    # The request asks for structured output against this shape; the shape is
    # restated for OpenAI-compatible servers that do not support it and answer
    # in prose (see llm_service._adjust_payload_for_error).
    'Answer with a JSON object: {"description": "<event description>", '
    '"field_suggestions": [{"field_name": "<name>", "description": "<desc>"}]}.'
)

DEFAULT_ASK_SYSTEM_PROMPT = (
    "You are a knowledgeable assistant for a product analytics tracking plan. "
    "Answer questions concisely using ONLY the provided context. "
    "Cite sources using [n] notation where n corresponds to the numbered item in the context. "
    "If the context is insufficient, say so clearly. "
    "Reply in the same language as the question."
)

DEFAULT_ALERT_EXPLANATION_SYSTEM_PROMPT = (
    "You are an analytics monitoring assistant. Given alert items from a "
    "product analytics tracking plan (volume anomalies, schema drifts, "
    "distribution drifts), write a short explanation: what likely happened "
    "and whether the items look related (e.g. same release, same platform, "
    "shared root cause). It is read in a chat notification, so keep it to what "
    "a reader takes in at a glance: plain sentences, no markdown, no preamble. "
    "Be concrete; if the data is insufficient for a hypothesis, say what "
    "to check next instead of speculating. "
    # Without this the explanation is a pure function of the current bucket, so
    # a scope that drifts for days gets the same paragraph every time and the
    # repeat teaches the reader nothing. The prompt only carries a
    # history block when this rule has actually sent something about these same
    # scopes in the last week.
    "If the input includes what you previously sent for these scopes, treat it "
    "as already read: open with what CHANGED since (deeper, recovering, spread "
    "to another scope, still unresolved after N hours) and do not restate the "
    "earlier explanation. If nothing has changed, say so in one sentence rather "
    "than rephrasing it. "
    # Unlike the "ask" prompt there is no user question whose language to
    # mirror, so without this the model picked one per call and alerts arrived
    # in whatever it felt like. The rest of the product and its docs are
    # English. Override this whole prompt under Settings -> Instance -> AI to
    # get a different language.
    "Reply in English."
)
