"""Shared data shapes used across the pipeline and reporting code.

Kept in their own module, separate from both src/vpa/pipeline.py and
src/vpa/reporting/, so neither has to import the other just to describe
the same piece of data.
"""

from __future__ import annotations

from pydantic import BaseModel

from vpa.data.fake import FakeCandidate


class ScanMetadata(BaseModel):
    """Provenance information every report must carry."""

    git_commit_sha: str
    signal_spec_hash: str
    config_version: str
    model_version: str


class ScanResult(BaseModel):
    """Everything a report needs: the metadata plus the candidates."""

    metadata: ScanMetadata
    candidates: list[FakeCandidate]
