"""Tests for src/vpa/cli.py - the two pieces scripts/run_scan.sh calls.

No network calls: smtplib and requests are mocked in every test that
exercises `deliver`, exactly as in test_delivery.py.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vpa.cli import build_report, deliver, main

CONFIG_YAML = """
config_version: "test-config-version"
email:
  from_addr: "me@example.com"
  to_addr: "me@example.com"
  smtp_password: "app-password"
push:
  ntfy_topic: "my-private-topic"
deadman:
  base_url: "https://hc-ping.com/abc123"
"""

INCOMPLETE_CONFIG_YAML = """
config_version: "test-config-version"
"""


def _init_throwaway_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "placeholder.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def _mock_network(monkeypatch) -> None:
    fake_smtp = MagicMock()
    fake_smtp.__enter__.return_value = fake_smtp
    monkeypatch.setattr("smtplib.SMTP", MagicMock(return_value=fake_smtp))

    mock_post = MagicMock()
    mock_post.return_value.raise_for_status = MagicMock()
    monkeypatch.setattr("requests.post", mock_post)

    mock_get = MagicMock()
    mock_get.return_value.raise_for_status = MagicMock()
    monkeypatch.setattr("requests.get", mock_get)


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Set up a throwaway repo + signal dir + config.yaml under tmp_path."""
    _init_throwaway_git_repo(tmp_path)
    signal_dir = tmp_path / "signal"
    signal_dir.mkdir()
    (signal_dir / "__init__.py").write_text("# frozen spec placeholder\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_YAML)
    return config_path, signal_dir


def test_build_report_writes_files_and_returns_zero(tmp_path, capsys):
    config_path, signal_dir = _make_repo(tmp_path)

    exit_code = build_report(
        config_path=config_path,
        repo_root=tmp_path,
        signal_dir=signal_dir,
        report_date=date(2026, 1, 15),
    )

    assert exit_code == 0
    assert (tmp_path / "reports" / "2026-01-15.md").exists()
    assert (tmp_path / "reports" / "2026-01-15.json").exists()
    assert "2026-01-15.md" in capsys.readouterr().out


def test_build_report_returns_one_on_pipeline_failure(tmp_path):
    config_path, _ = _make_repo(tmp_path)

    exit_code = build_report(
        config_path=config_path,
        repo_root=tmp_path,
        signal_dir=tmp_path / "does-not-exist",
        report_date=date(2026, 1, 15),
    )

    assert exit_code == 1


def test_deliver_ok_reads_back_the_report_and_sends_it(tmp_path, monkeypatch):
    config_path, signal_dir = _make_repo(tmp_path)
    _mock_network(monkeypatch)

    build_exit = build_report(
        config_path=config_path,
        repo_root=tmp_path,
        signal_dir=signal_dir,
        report_date=date(2026, 1, 15),
    )
    assert build_exit == 0

    deliver_exit = deliver(
        ["--ok"], config_path=config_path, repo_root=tmp_path, report_date=date(2026, 1, 15)
    )

    assert deliver_exit == 0


def test_deliver_ok_without_a_report_returns_two(tmp_path, monkeypatch):
    config_path, _ = _make_repo(tmp_path)
    _mock_network(monkeypatch)

    exit_code = deliver(
        ["--ok"], config_path=config_path, repo_root=tmp_path, report_date=date(2026, 1, 15)
    )

    assert exit_code == 2


def test_deliver_failed_sends_scan_unavailable(tmp_path, monkeypatch):
    config_path, _ = _make_repo(tmp_path)
    _mock_network(monkeypatch)

    exit_code = deliver(
        ["--failed", "boom"],
        config_path=config_path,
        repo_root=tmp_path,
        report_date=date(2026, 1, 15),
    )

    assert exit_code == 0


def test_deliver_with_incomplete_config_returns_two(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(INCOMPLETE_CONFIG_YAML)
    _mock_network(monkeypatch)

    exit_code = deliver(["--ok"], config_path=config_path, repo_root=tmp_path)

    assert exit_code == 2


def test_main_with_no_args_returns_usage_error(capsys):
    assert main([]) == 64
    assert "Usage" in capsys.readouterr().err


def test_main_with_unknown_command_returns_usage_error(capsys):
    assert main(["not-a-real-command"]) == 64
    assert "Unknown command" in capsys.readouterr().err


def test_deliver_requires_ok_or_failed(tmp_path):
    config_path, _ = _make_repo(tmp_path)

    with pytest.raises(SystemExit):
        deliver([], config_path=config_path, repo_root=tmp_path)
