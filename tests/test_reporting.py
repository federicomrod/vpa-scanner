"""Tests for src/vpa/reporting/report.py. No network calls - everything
reads/writes only temporary files."""

import json
from datetime import date

from vpa.data.fake import FakeCandidate
from vpa.models import ScanMetadata, ScanResult
from vpa.reporting.report import render_json, render_markdown, write_report

REPORT_DATE = date(2026, 1, 15)


def _make_result(candidates=None) -> ScanResult:
    metadata = ScanMetadata(
        git_commit_sha="abc123",
        signal_spec_hash="deadbeef",
        config_version="test-config-version",
        model_version="not-applicable-yet",
    )
    if candidates is None:
        candidates = [FakeCandidate(ticker="FAKE1")]
    return ScanResult(metadata=metadata, candidates=candidates)


def test_render_markdown_includes_fake_data_banner():
    markdown = render_markdown(_make_result(), REPORT_DATE)

    assert "FAKE DATA" in markdown


def test_render_markdown_includes_metadata():
    markdown = render_markdown(_make_result(), REPORT_DATE)

    assert "abc123" in markdown
    assert "deadbeef" in markdown
    assert "test-config-version" in markdown
    assert "not-applicable-yet" in markdown


def test_render_markdown_includes_every_candidate():
    candidates = [FakeCandidate(ticker="FAKE1"), FakeCandidate(ticker="FAKE2")]
    markdown = render_markdown(_make_result(candidates), REPORT_DATE)

    assert "FAKE1" in markdown
    assert "FAKE2" in markdown


def test_render_markdown_handles_no_candidates():
    markdown = render_markdown(_make_result(candidates=[]), REPORT_DATE)

    assert "No candidates today." in markdown


def test_render_json_is_valid_and_matches_the_result():
    payload = json.loads(render_json(_make_result(), REPORT_DATE))

    assert payload["report_date"] == "2026-01-15"
    assert payload["metadata"]["git_commit_sha"] == "abc123"
    assert payload["candidates"][0]["ticker"] == "FAKE1"


def test_write_report_creates_both_files(tmp_path):
    output_dir = tmp_path / "reports"

    paths = write_report(_make_result(), output_dir, REPORT_DATE)

    assert paths.markdown_path == output_dir / "2026-01-15.md"
    assert paths.json_path == output_dir / "2026-01-15.json"
    assert paths.markdown_path.read_text() == render_markdown(_make_result(), REPORT_DATE)
    assert paths.json_path.read_text() == render_json(_make_result(), REPORT_DATE)


def test_write_report_creates_missing_output_dir(tmp_path):
    output_dir = tmp_path / "does" / "not" / "exist" / "yet"

    paths = write_report(_make_result(), output_dir, REPORT_DATE)

    assert paths.markdown_path.exists()
    assert paths.json_path.exists()
