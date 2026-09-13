"""The hello-world pipeline.

Ties everything together: configuration, (for now, fake) candidate
data, the provenance metadata every report must carry, writing the
report to disk, and sending it out by email, push, and a dead-man's-
switch ping.

If any step fails, "SCAN UNAVAILABLE" is sent instead of a partial
report - see CLAUDE.md ("fail loudly, never partially").

No market data, no AI calls, and no pattern logic will ever be added
here directly - see CLAUDE.md.
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import date
from pathlib import Path

from vpa.config import AppConfig
from vpa.data.fake import generate_fake_candidates
from vpa.delivery.deadman import DeadManSwitch
from vpa.delivery.email import EmailSender
from vpa.delivery.push import PushSender
from vpa.models import ScanMetadata, ScanResult
from vpa.reporting.report import ReportPaths, write_report

#: There is no model in this milestone - report metadata always carries
#: this placeholder instead of a real model version.
MODEL_VERSION_PLACEHOLDER = "not-applicable-yet"

#: The repository root, derived from this file's location
#: (src/vpa/pipeline.py -> repo root is two levels up from src/vpa/).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The frozen signal specification area (see CLAUDE.md).
SIGNAL_DIR = REPO_ROOT / "src" / "vpa" / "signal"


class PipelineError(Exception):
    """Raised when a pipeline step fails."""


def get_git_commit_sha(repo_root: Path = REPO_ROOT) -> str:
    """Return the current git commit SHA for `repo_root`."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PipelineError(f"Could not determine git commit SHA: {exc}") from exc
    return result.stdout.strip()


def hash_signal_spec(signal_dir: Path = SIGNAL_DIR) -> str:
    """Return a deterministic hash of every file under `signal_dir`.

    This is how a report can prove exactly which version of the frozen
    signal specification (see CLAUDE.md) produced it. The hash covers
    both file paths and file contents, and does not depend on file
    order or modification times, so it only changes when the actual
    content of the signal package changes.
    """
    if not signal_dir.is_dir():
        raise PipelineError(f"Signal directory not found: {signal_dir}")

    digest = hashlib.sha256()
    file_paths = sorted(p for p in signal_dir.rglob("*") if p.is_file())
    for file_path in file_paths:
        relative_path = file_path.relative_to(signal_dir).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(file_path.read_bytes())
    return digest.hexdigest()


def run_pipeline(
    config: AppConfig,
    *,
    repo_root: Path = REPO_ROOT,
    signal_dir: Path = SIGNAL_DIR,
) -> ScanResult:
    """Run the hello-world pipeline.

    Gathers (fake, for now) candidates and assembles the provenance
    metadata every report must carry.
    """
    metadata = ScanMetadata(
        git_commit_sha=get_git_commit_sha(repo_root),
        signal_spec_hash=hash_signal_spec(signal_dir),
        config_version=config.config_version,
        model_version=MODEL_VERSION_PLACEHOLDER,
    )
    candidates = generate_fake_candidates()
    return ScanResult(metadata=metadata, candidates=candidates)


def _resolve_output_dir(repo_root: Path, output_dir: Path) -> Path:
    """`output_dir` from config is relative to `repo_root` unless it's
    already an absolute path."""
    return output_dir if output_dir.is_absolute() else repo_root / output_dir


def run_and_write_report(
    config: AppConfig,
    *,
    repo_root: Path = REPO_ROOT,
    signal_dir: Path = SIGNAL_DIR,
    report_date: date | None = None,
) -> ReportPaths:
    """Run the pipeline and write its report (Markdown + JSON) to disk.

    Delivery (email, push, dead-man's-switch) is not part of this
    function - see `run_scan` for the full, deliver-it-too version.
    """
    result = run_pipeline(config, repo_root=repo_root, signal_dir=signal_dir)
    output_dir = _resolve_output_dir(repo_root, config.report.output_dir)
    return write_report(result, output_dir, report_date or date.today())


def _send_success(
    report_date: date,
    result: ScanResult,
    paths: ReportPaths,
    email_sender: EmailSender,
    push_sender: PushSender,
    deadman: DeadManSwitch,
) -> None:
    email_sender.send(
        subject=f"VPA Scanner report - {report_date.isoformat()}",
        body=paths.markdown_path.read_text(),
    )
    push_sender.send(
        f"VPA scan {report_date.isoformat()}: {len(result.candidates)} candidate(s) "
        "(FAKE DATA - Milestone 1)."
    )
    deadman.ping_success()


def _send_failure(
    report_date: date,
    error: Exception,
    email_sender: EmailSender,
    push_sender: PushSender,
    deadman: DeadManSwitch,
) -> None:
    email_sender.send(
        subject=f"SCAN UNAVAILABLE - {report_date.isoformat()}",
        body=f"The scan failed and produced no report.\n\nError:\n{error}",
    )
    push_sender.send(f"SCAN UNAVAILABLE ({report_date.isoformat()}): {error}")
    deadman.ping_fail()


def run_scan(
    config: AppConfig,
    *,
    email_sender: EmailSender,
    push_sender: PushSender,
    deadman: DeadManSwitch,
    repo_root: Path = REPO_ROOT,
    signal_dir: Path = SIGNAL_DIR,
    report_date: date | None = None,
) -> ReportPaths:
    """Run the full hello-world scan and deliver it: build the report,
    email it, push a one-line summary, and ping the dead-man's-switch.

    If any step before delivery fails, sends "SCAN UNAVAILABLE" with the
    error by email and push instead of a partial report, pings the
    dead-man's-switch failure URL, and re-raises the original error so
    the caller (the supervisor script) knows the run failed.
    """
    resolved_date = report_date or date.today()

    try:
        result = run_pipeline(config, repo_root=repo_root, signal_dir=signal_dir)
        output_dir = _resolve_output_dir(repo_root, config.report.output_dir)
        paths = write_report(result, output_dir, resolved_date)
    except Exception as exc:
        _send_failure(resolved_date, exc, email_sender, push_sender, deadman)
        raise

    _send_success(resolved_date, result, paths, email_sender, push_sender, deadman)
    return paths
