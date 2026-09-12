"""Tests for src/vpa/pipeline.py.

No network calls - `git` commands here only ever run against throwaway
repositories created inside a pytest tmp_path, never the real repository
or the internet.
"""

import subprocess
from datetime import date
from pathlib import Path

import pytest

from vpa.config import AppConfig
from vpa.pipeline import (
    MODEL_VERSION_PLACEHOLDER,
    PipelineError,
    get_git_commit_sha,
    hash_signal_spec,
    run_and_write_report,
    run_pipeline,
)


def _init_throwaway_git_repo(path: Path) -> str:
    """Create a tiny git repo in `path` and return its HEAD commit SHA."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "placeholder.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_get_git_commit_sha_matches_repo_head(tmp_path):
    expected_sha = _init_throwaway_git_repo(tmp_path)

    assert get_git_commit_sha(tmp_path) == expected_sha


def test_get_git_commit_sha_raises_outside_a_repo(tmp_path):
    with pytest.raises(PipelineError):
        get_git_commit_sha(tmp_path)


def test_hash_signal_spec_is_deterministic(tmp_path):
    (tmp_path / "__init__.py").write_text("# a signal file\n")

    first_hash = hash_signal_spec(tmp_path)
    second_hash = hash_signal_spec(tmp_path)

    assert first_hash == second_hash


def test_hash_signal_spec_changes_when_contents_change(tmp_path):
    signal_file = tmp_path / "__init__.py"
    signal_file.write_text("version 1\n")
    original_hash = hash_signal_spec(tmp_path)

    signal_file.write_text("version 2\n")
    changed_hash = hash_signal_spec(tmp_path)

    assert original_hash != changed_hash


def test_hash_signal_spec_missing_directory_raises(tmp_path):
    with pytest.raises(PipelineError, match="not found"):
        hash_signal_spec(tmp_path / "does-not-exist")


def test_run_pipeline_assembles_a_complete_scan_result(tmp_path):
    git_sha = _init_throwaway_git_repo(tmp_path)
    signal_dir = tmp_path / "signal"
    signal_dir.mkdir()
    (signal_dir / "__init__.py").write_text("# frozen spec placeholder\n")

    config = AppConfig(config_version="test-config-version")

    result = run_pipeline(config, repo_root=tmp_path, signal_dir=signal_dir)

    assert result.metadata.git_commit_sha == git_sha
    assert result.metadata.signal_spec_hash == hash_signal_spec(signal_dir)
    assert result.metadata.config_version == "test-config-version"
    assert result.metadata.model_version == MODEL_VERSION_PLACEHOLDER
    assert len(result.candidates) == 3


def test_run_and_write_report_writes_files_under_configured_output_dir(tmp_path):
    _init_throwaway_git_repo(tmp_path)
    signal_dir = tmp_path / "signal"
    signal_dir.mkdir()
    (signal_dir / "__init__.py").write_text("# frozen spec placeholder\n")

    config = AppConfig(config_version="test-config-version")
    config.report.output_dir = Path("reports")  # relative - resolved against repo_root

    paths = run_and_write_report(
        config,
        repo_root=tmp_path,
        signal_dir=signal_dir,
        report_date=date(2026, 1, 15),
    )

    assert paths.markdown_path == tmp_path / "reports" / "2026-01-15.md"
    assert paths.json_path == tmp_path / "reports" / "2026-01-15.json"
    assert paths.markdown_path.exists()
    assert paths.json_path.exists()
    assert "FAKE DATA" in paths.markdown_path.read_text()


def test_run_and_write_report_respects_absolute_output_dir(tmp_path):
    _init_throwaway_git_repo(tmp_path)
    signal_dir = tmp_path / "signal"
    signal_dir.mkdir()
    (signal_dir / "__init__.py").write_text("# frozen spec placeholder\n")

    elsewhere = tmp_path / "elsewhere"
    config = AppConfig(config_version="test-config-version")
    config.report.output_dir = elsewhere

    paths = run_and_write_report(
        config,
        repo_root=tmp_path,
        signal_dir=signal_dir,
        report_date=date(2026, 1, 15),
    )

    assert paths.markdown_path.parent == elsewhere
