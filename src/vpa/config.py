"""Configuration loading.

Settings live in a `config.yaml` file. A small number of environment
variables can override individual values on top of that - this is meant
for things like "run with a different report folder for a one-off test",
not for secrets.

This module never reads a `.env` file itself. Environment variables are
read from whatever the process already has (`os.environ`); how they got
there is somebody else's concern (see CLAUDE.md, rule 3 - `.env` files
are never read or referenced by this codebase).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

#: Environment variables that override configuration must start with this.
ENV_PREFIX = "VPA_"

#: Within an environment variable name, this separates nested fields.
#: For example, VPA_REPORT__OUTPUT_DIR overrides report.output_dir.
ENV_NESTED_SEPARATOR = "__"


class ReportConfig(BaseModel):
    """Where generated reports get written."""

    output_dir: Path = Path("reports")


class AppConfig(BaseModel):
    """The full, validated application configuration."""

    #: Identifies which version of this configuration produced a report.
    #: Bump it by hand whenever config.yaml's meaning changes.
    config_version: str

    report: ReportConfig = Field(default_factory=ReportConfig)


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


def load_config(config_path: Path, env: Mapping[str, str] | None = None) -> AppConfig:
    """Load and validate configuration from a YAML file.

    Environment variables prefixed with `VPA_` override values from the
    file - e.g. `VPA_CONFIG_VERSION` overrides `config_version`, and
    `VPA_REPORT__OUTPUT_DIR` overrides the nested `report.output_dir`.

    Raises ConfigError if the file is missing, isn't valid YAML, or
    doesn't match the expected shape.
    """
    if env is None:
        env = os.environ

    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")

    _apply_env_overrides(raw, env)

    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration in {config_path}: {exc}") from exc


def _apply_env_overrides(raw: dict, env: Mapping[str, str]) -> None:
    """Mutate `raw` in place, applying any `VPA_`-prefixed overrides from `env`."""
    for key, value in env.items():
        if not key.startswith(ENV_PREFIX):
            continue
        field_path = key[len(ENV_PREFIX) :].lower().split(ENV_NESTED_SEPARATOR)
        _set_nested(raw, field_path, value)


def _set_nested(raw: dict, field_path: list[str], value: str) -> None:
    node = raw
    for part in field_path[:-1]:
        node = node.setdefault(part, {})
    node[field_path[-1]] = value
