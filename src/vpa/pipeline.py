"""The hello-world pipeline.

Ties configuration and (for now, fake) candidate data together, and
computes the provenance metadata every report must carry: the exact code
that produced it (git commit SHA and a hash of the frozen signal
specification), which configuration version was used, and a placeholder
for a model version.

Report rendering and delivery (email, push, dead-man's-switch) are not
implemented yet - they're separate, later pull requests.

No market data, no AI calls, and no pattern logic will ever be added
here directly - see CLAUDE.md.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from pydantic import BaseModel

from vpa.config import AppConfig
from vpa.data.fake import FakeCandidate, generate_fake_candidates

#: There is no model in this milestone - report metadata always carries
#: this placeholder instead of a real model version.
MODEL_VERSION_PLACEHOLDER = "not-applicable-yet"

#: The repository root, derived from this file's location
#: (src/vpa/pipeline.py -> repo root is two levels up from src/vpa/).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The frozen signal specification area (see CLAUDE.md).
SIGNAL_DIR = REPO_ROOT / "src" / "vpa" / "signal"


class ScanMetadata(BaseModel):
    """Provenance information every report must carry."""

    git_commit_sha: str
    signal_spec_hash: str
    config_version: str
    model_version: str


class ScanResult(BaseModel):
    """Everything the (future) report renderer needs."""

    metadata: ScanMetadata
    candidates: list[FakeCandidate]


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
    metadata every report must carry. Report rendering and delivery are
    separate pull requests - this function only produces the data they
    will need.
    """
    metadata = ScanMetadata(
        git_commit_sha=get_git_commit_sha(repo_root),
        signal_spec_hash=hash_signal_spec(signal_dir),
        config_version=config.config_version,
        model_version=MODEL_VERSION_PLACEHOLDER,
    )
    candidates = generate_fake_candidates()
    return ScanResult(metadata=metadata, candidates=candidates)
