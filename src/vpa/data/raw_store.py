"""The immutable raw data store (Concept v2 Section 3.1, Architecture v2
Section 6).

Layout, under the data root (default `~/vpa-data`):

    raw/<dataset>/<partition>/part-<run id>.parquet
    raw/<dataset>/<partition>/part-<run id>.manifest.json

e.g. `raw/minute/date=2026-09-18/part-20260919T101500Z.parquet`.

Rules:
- A file, once written, is never modified or replaced. New data for an
  existing partition goes into a new part file alongside the old ones.
- Every part has a manifest recording what it covers and its SHA-256
  checksum, so silent corruption can be detected later.
- The manifest is written last. A part without a manifest is an
  interrupted write and is ignored (and reported) when reading.
- Written files are made read-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_DATA_ROOT = Path.home() / "vpa-data"
READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH


class RawStoreError(Exception):
    """Raised when the raw store would be modified or is inconsistent."""


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def partition_dir(data_root: Path, dataset: str, partition: str) -> Path:
    return data_root / "raw" / dataset / partition


def write_part(
    data_root: Path,
    dataset: str,
    partition: str,
    run_id: str,
    table: pd.DataFrame,
    manifest: dict[str, Any],
) -> Path:
    """Write one new, read-only part file plus its manifest."""
    folder = partition_dir(data_root, dataset, partition)
    folder.mkdir(parents=True, exist_ok=True)
    data_path = folder / f"part-{run_id}.parquet"
    manifest_path = folder / f"part-{run_id}.manifest.json"

    _write_once(data_path, lambda p: table.to_parquet(p, index=False))
    full_manifest = {
        **manifest,
        "run_id": run_id,
        "written_utc": datetime.now(UTC).isoformat(),
        "file": data_path.name,
        "rows": len(table),
        "sha256": sha256_of(data_path),
    }
    _write_once(manifest_path, lambda p: p.write_text(json.dumps(full_manifest, indent=2)))
    return data_path


def manifests(data_root: Path, dataset: str, partition: str) -> list[dict[str, Any]]:
    """All complete parts' manifests for one partition."""
    folder = partition_dir(data_root, dataset, partition)
    return [json.loads(p.read_text()) for p in sorted(folder.glob("part-*.manifest.json"))]


def read_partition(data_root: Path, dataset: str, partition: str) -> pd.DataFrame:
    """Every complete part of one partition, checked against its checksum."""
    folder = partition_dir(data_root, dataset, partition)
    tables = []
    listed = set()
    for manifest in manifests(data_root, dataset, partition):
        path = folder / manifest["file"]
        listed.add(path.name)
        if sha256_of(path) != manifest["sha256"]:
            raise RawStoreError(f"Checksum mismatch - raw file has changed: {path}")
        tables.append(pd.read_parquet(path))
    for orphan in sorted({p.name for p in folder.glob("part-*.parquet")} - listed):
        log.warning("Ignoring incomplete part with no manifest: %s", folder / orphan)
    filled = [t for t in tables if not t.empty]
    return pd.concat(filled, ignore_index=True) if filled else pd.DataFrame()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_once(path: Path, write) -> None:
    """Write via a temporary file, make it read-only, then hard-link it
    into place - which fails, rather than replacing, if `path` exists."""
    if path.exists():
        raise RawStoreError(f"Refusing to overwrite raw file: {path}")
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        # Left over from a crash; it was never linked into place.
        os.chmod(partial, stat.S_IRUSR | stat.S_IWUSR)
        partial.unlink()
    write(partial)
    os.chmod(partial, READ_ONLY)
    try:
        os.link(partial, path)
    except FileExistsError as exc:
        raise RawStoreError(f"Refusing to overwrite raw file: {path}") from exc
    finally:
        partial.unlink()
