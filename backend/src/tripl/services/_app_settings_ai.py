"""AI section of the runtime settings: chat and search-embedding config.

Private half of :mod:`tripl.services.app_settings_service`, which re-exports
every name here; import from the facade.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any

from tripl.config import settings
from tripl.services._app_settings_core import build_service_values
from tripl.services.ai_defaults import (
    DEFAULT_ALERT_EXPLANATION_SYSTEM_PROMPT,
    DEFAULT_ASK_SYSTEM_PROMPT,
    DEFAULT_DESCRIBE_SYSTEM_PROMPT,
)

if TYPE_CHECKING:
    from tripl.services._app_settings_sources import ResolvedSettings


@dataclass(frozen=True)
class AiConfig:
    """Effective AI configuration used by LLM and embedding callers.

    API keys are already resolved (field-specific override -> field-specific
    env -> ``OPENAI_API_KEY`` env fallback) and decrypted.
    """

    ai_enabled: bool
    ai_base_url: str
    ai_model: str
    ai_api_key: str
    ai_timeout_seconds: int
    ai_max_output_tokens: int
    describe_system_prompt: str
    ask_system_prompt: str
    alert_explanation_system_prompt: str
    search_embeddings_enabled: bool
    search_embedding_provider: str
    search_embedding_model: str
    search_embedding_api_key: str
    #: Where the OpenAI-compatible embeddings endpoint lives: the operator's
    #: ``SEARCH_EMBEDDING_BASE_URL``, or an organization's own (F20 PR10).
    search_embedding_base_url: str
    # True when ``ai_base_url`` came from an ORGANIZATION (any deployment mode):
    # ``llm_service`` then re-checks the host right before the request
    # (reject_private_host, the DNS-rebinding half of the SSRF guard).
    host_guard: bool = False
    # The same for ``search_embedding_base_url`` and ``embedding_service``.
    embedding_host_guard: bool = False


AI_CONFIG_FIELDS = frozenset(f.name for f in fields(AiConfig)) - {
    "host_guard",
    "embedding_host_guard",
}


def default_ai_prompts() -> dict[str, str]:
    return {
        "describe_system_prompt": DEFAULT_DESCRIBE_SYSTEM_PROMPT,
        "ask_system_prompt": DEFAULT_ASK_SYSTEM_PROMPT,
        "alert_explanation_system_prompt": DEFAULT_ALERT_EXPLANATION_SYSTEM_PROMPT,
    }


def env_ai_config() -> AiConfig:
    return build_ai_config({})


def build_ai_config(overrides: dict[str, Any]) -> AiConfig:
    return _ai_config_from(build_service_values(overrides))


def _ai_config_from(
    values: Mapping[str, Any], *, host_guard: bool = False, embedding_host_guard: bool = False
) -> AiConfig:
    config = AiConfig(
        ai_enabled=bool(values["ai_enabled"]),
        ai_base_url=str(values["ai_base_url"]),
        ai_model=str(values["ai_model"]),
        ai_api_key=str(values["ai_api_key"]),
        ai_timeout_seconds=int(values["ai_timeout_seconds"]),
        ai_max_output_tokens=int(values["ai_max_output_tokens"]),
        describe_system_prompt=str(values["describe_system_prompt"]),
        ask_system_prompt=str(values["ask_system_prompt"]),
        alert_explanation_system_prompt=str(values["alert_explanation_system_prompt"]),
        search_embeddings_enabled=bool(values["search_embeddings_enabled"]),
        search_embedding_provider=str(values["search_embedding_provider"]),
        search_embedding_model=str(values["search_embedding_model"]),
        search_embedding_api_key=str(values["search_embedding_api_key"]),
        search_embedding_base_url=str(values["search_embedding_base_url"]),
        host_guard=host_guard,
        embedding_host_guard=embedding_host_guard,
    )
    if settings.public_demo:
        # Every call a stranger can trigger would run on the operator's key, so
        # a public demo has no AI whatever the settings say. Demo
        # projects still search semantically from their bundled embedding
        # fixture, which needs no provider.
        return _without_credentials(config)
    return config


def _without_credentials(config: AiConfig) -> AiConfig:
    return replace(
        config,
        ai_enabled=False,
        ai_api_key="",
        search_embeddings_enabled=False,
        search_embedding_api_key="",
        search_embedding_base_url="",
    )


def disabled_ai_config() -> AiConfig:
    """What an organization gets when its settings cannot be read: no AI at all.

    Built from the env values with every credential removed, so nothing in it
    can reach the operator's provider (critique #19: fail closed in org scope).
    """
    return _without_credentials(env_ai_config())


def ai_prompt_defaults() -> dict[str, str]:
    """The built-in system prompts, before any stored override."""
    return {
        "describe_system_prompt": DEFAULT_DESCRIBE_SYSTEM_PROMPT,
        "ask_system_prompt": DEFAULT_ASK_SYSTEM_PROMPT,
        "alert_explanation_system_prompt": DEFAULT_ALERT_EXPLANATION_SYSTEM_PROMPT,
    }


def ai_config_for(resolved: ResolvedSettings) -> AiConfig:
    return _ai_config_from(
        resolved.values,
        host_guard="ai_base_url" in resolved.guarded_hosts,
        embedding_host_guard="search_embedding_base_url" in resolved.guarded_hosts,
    )
