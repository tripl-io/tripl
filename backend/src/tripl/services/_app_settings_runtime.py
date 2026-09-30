"""Runtime section of the runtime settings: public URL and row-limit defaults.

Private half of :mod:`tripl.services.app_settings_service`, which re-exports
every name here; import from the facade.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tripl.services._app_settings_core import build_service_values


@dataclass(frozen=True)
class RuntimeConfig:
    app_base_url: str
    scan_row_limit_default: int
    metrics_row_limit_default: int


def env_runtime_config() -> RuntimeConfig:
    return build_runtime_config({})


def build_runtime_config(overrides: dict[str, Any]) -> RuntimeConfig:
    return _runtime_config_from(build_service_values(overrides))


def _runtime_config_from(values: Mapping[str, Any]) -> RuntimeConfig:
    return RuntimeConfig(
        app_base_url=str(values["app_base_url"]),
        scan_row_limit_default=int(values["scan_row_limit_default"]),
        metrics_row_limit_default=int(values["metrics_row_limit_default"]),
    )
