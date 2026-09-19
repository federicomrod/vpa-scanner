"""Universe snapshot files - storage for Concept v2 Section 2.

Each month's universe is written once to `<universe_dir>/YYYY-MM-DD.parquet`
and never modified afterwards: writing refuses to replace an existing file,
and the finished file is made read-only. The selection rules themselves
live in `vpa.signal.universe`; this module only builds, stores and finds
the snapshot files.
"""

from __future__ import annotations

import logging
import os
import stat
from datetime import date
from pathlib import Path

import pandas as pd

from vpa.signal.universe import UniverseResult, UniverseRules, select_universe

log = logging.getLogger(__name__)


class SnapshotExistsError(Exception):
    """Raised when asked to write a snapshot that already exists."""


def snapshot_path(universe_dir: Path, rebalance_date: date) -> Path:
    return universe_dir / f"{rebalance_date.isoformat()}.parquet"


def build_snapshot(
    rebalance_date: date,
    securities: pd.DataFrame,
    daily_bars: pd.DataFrame,
    splits: pd.DataFrame,
    universe_dir: Path,
    rules: UniverseRules | None = None,
) -> Path:
    """Select the universe for `rebalance_date` and write its snapshot.

    Refuses (SnapshotExistsError) if the snapshot already exists, before
    doing any work. Logs how many stocks each rule removed.
    """
    path = snapshot_path(universe_dir, rebalance_date)
    if path.exists():
        raise SnapshotExistsError(f"Universe snapshot already exists, not overwriting: {path}")

    result = select_universe(rebalance_date, securities, daily_bars, splits, rules)
    _log_summary(result, screened=len(securities))
    write_snapshot(result.selected, path)
    return path


def write_snapshot(selected: pd.DataFrame, path: Path) -> None:
    """Write a snapshot file once: no overwriting, and read-only afterwards.

    Written to a temporary name first and then renamed, so an interrupted
    write never leaves a half-written file under the real name.
    """
    if path.exists():
        raise SnapshotExistsError(f"Universe snapshot already exists, not overwriting: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    selected.to_parquet(partial, index=False)
    os.chmod(partial, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    # os.link fails if `path` appeared in the meantime, unlike a rename,
    # which would silently replace it.
    try:
        os.link(partial, path)
    except FileExistsError as exc:
        raise SnapshotExistsError(
            f"Universe snapshot already exists, not overwriting: {path}"
        ) from exc
    finally:
        partial.unlink()
    log.info("Wrote universe snapshot %s (%d tickers)", path, len(selected))


def read_snapshot(universe_dir: Path, rebalance_date: date) -> pd.DataFrame:
    return pd.read_parquet(snapshot_path(universe_dir, rebalance_date))


def snapshot_in_force(universe_dir: Path, day: date) -> date:
    """The rebalance date of the snapshot that applies on `day`: the most
    recent one dated on or before it. Raises if there is none."""
    dates = sorted(date.fromisoformat(p.stem) for p in universe_dir.glob("????-??-??.parquet"))
    in_force = [d for d in dates if d <= day]
    if not in_force:
        raise FileNotFoundError(f"No universe snapshot in {universe_dir} on or before {day}")
    return in_force[-1]


def _log_summary(result: UniverseResult, screened: int) -> None:
    log.info(
        "Universe for %s (measured at the %s close): %d stocks screened",
        result.rebalance_date,
        result.as_of_session,
        screened,
    )
    for label, removed in result.removed_by_step.items():
        log.info("  removed %5d: %s", removed, label)
    log.info("  selected %4d", len(result.selected))
