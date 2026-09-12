"""Tests for src/vpa/config.py. No network calls - everything reads from
temporary files and in-memory environment dicts."""

from pathlib import Path

import pytest

from vpa.config import ConfigError, load_config

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def test_load_config_reads_sample_fixture():
    config = load_config(FIXTURES_DIR / "sample_config.yaml", env={})

    assert config.config_version == "test-1"
    assert config.report.output_dir == Path("reports")


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.yaml", env={})


def test_load_config_invalid_yaml_raises(tmp_path):
    bad_file = tmp_path / "config.yaml"
    bad_file.write_text("config_version: [unterminated")

    with pytest.raises(ConfigError, match="Could not parse"):
        load_config(bad_file, env={})


def test_load_config_non_mapping_raises(tmp_path):
    bad_file = tmp_path / "config.yaml"
    bad_file.write_text("- just\n- a\n- list\n")

    with pytest.raises(ConfigError, match="mapping"):
        load_config(bad_file, env={})


def test_load_config_missing_required_field_raises(tmp_path):
    incomplete_file = tmp_path / "config.yaml"
    incomplete_file.write_text("report:\n  output_dir: reports\n")

    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_config(incomplete_file, env={})


def test_env_override_top_level_field(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("config_version: 'from-file'\n")

    config = load_config(config_file, env={"VPA_CONFIG_VERSION": "from-env"})

    assert config.config_version == "from-env"


def test_env_override_nested_field(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("config_version: 'v1'\n")

    config = load_config(
        config_file,
        env={"VPA_REPORT__OUTPUT_DIR": "/tmp/somewhere-else"},
    )

    assert config.report.output_dir == Path("/tmp/somewhere-else")


def test_unrelated_env_vars_are_ignored(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("config_version: 'v1'\n")

    config = load_config(config_file, env={"PATH": "/usr/bin", "HOME": "/root"})

    assert config.config_version == "v1"
