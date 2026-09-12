"""Turning a scan result into the files that (eventually) get emailed.

Two outputs are always written together: a Markdown report meant for a
human to read, and a JSON record carrying the same information in a
machine-readable form. Nothing in this module sends anything anywhere -
see src/vpa/pipeline.py for how these get produced, and a later pull
request for how they get delivered by email and push.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from vpa.models import ScanResult

FAKE_DATA_BANNER = (
    "> **⚠️ FAKE DATA.** This is a Milestone 1 plumbing test. None of "
    "the candidates below are real - see CLAUDE.md."
)


@dataclass(frozen=True)
class ReportPaths:
    """Where a report's two files were written."""

    markdown_path: Path
    json_path: Path


def render_markdown(result: ScanResult, report_date: date) -> str:
    """Render a scan result as a Markdown report for a human to read."""
    lines = [
        f"# VPA Scanner report - {report_date.isoformat()}",
        "",
        FAKE_DATA_BANNER,
        "",
        "## Metadata",
        "",
        f"- Git commit: `{result.metadata.git_commit_sha}`",
        f"- Signal spec hash: `{result.metadata.signal_spec_hash}`",
        f"- Config version: `{result.metadata.config_version}`",
        f"- Model version: `{result.metadata.model_version}`",
        "",
        "## Candidates",
        "",
    ]

    if not result.candidates:
        lines.append("No candidates today.")
    else:
        lines.append("| Ticker | Note |")
        lines.append("|---|---|")
        for candidate in result.candidates:
            lines.append(f"| {candidate.ticker} | {candidate.note} |")

    lines.append("")
    return "\n".join(lines)


def render_json(result: ScanResult, report_date: date) -> str:
    """Render a scan result as a JSON record carrying the same information."""
    payload = {
        "report_date": report_date.isoformat(),
        "metadata": result.metadata.model_dump(),
        "candidates": [candidate.model_dump() for candidate in result.candidates],
    }
    return json.dumps(payload, indent=2) + "\n"


def write_report(result: ScanResult, output_dir: Path, report_date: date) -> ReportPaths:
    """Render and write both report files, creating `output_dir` if needed.

    Returns the paths that were written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    markdown_path = output_dir / f"{report_date.isoformat()}.md"
    json_path = output_dir / f"{report_date.isoformat()}.json"

    markdown_path.write_text(render_markdown(result, report_date))
    json_path.write_text(render_json(result, report_date))

    return ReportPaths(markdown_path=markdown_path, json_path=json_path)
