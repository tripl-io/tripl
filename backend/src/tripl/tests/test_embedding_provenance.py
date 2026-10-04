"""Regression coverage for fixed vector width and embedding-space provenance."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from tripl.config import Settings, settings
from tripl.services.app_settings_service import env_ai_config
from tripl.services.embedding_service import embedding_provenance, sanitize_embedding


def test_vector_width_must_match_postgres_column() -> None:
    with pytest.raises(ValidationError, match="vector\\(1536\\)"):
        Settings(search_embedding_dimensions=768)


def test_provenance_changes_with_model_or_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = env_ai_config()
    original = embedding_provenance(config)
    assert original != embedding_provenance(replace(config, search_embedding_model="another-model"))
    # The endpoint is the CONFIG's (F20 PR10: an organization's own), so it is
    # read when the config is built, not off ``settings`` at hash time.
    monkeypatch.setattr(settings, "search_embedding_base_url", "https://other.example/v1")
    assert original != embedding_provenance(env_ai_config())
    assert original != embedding_provenance(
        replace(config, search_embedding_base_url="https://org.example/v1")
    )
    assert len(original) <= 128


def test_invalid_query_vector_degrades_to_lexical() -> None:
    assert sanitize_embedding([0.1] * 768) == []
    assert sanitize_embedding([float("nan")] * 1536) == []
