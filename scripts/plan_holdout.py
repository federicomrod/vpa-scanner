"""List what belongs in the holdout, and optionally move it.

Section 11.4 reserves the last 12 months and puts it beyond this
assistant's reach. This script exists so the move is exact rather than
approximate - it prints every directory that crosses the boundary and
what it comes to.

**It moves nothing unless `--move` is passed**, and the intended use is
that the project owner runs that, not the assistant. See
`docs/holdout-setup.md`.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

from vpa.data.raw_store import DEFAULT_DATA_ROOT

#: The first session of the holdout (Section 11.4's final 12 months).
HOLDOUT_FROM = date(2025, 9, 19)

#: Stores partitioned by session, which can be split cleanly.
DATED_STORES = [
    "raw/minute",
    "derived/hourly",
    "derived/daily",
    "derived/features/hourly",
    "derived/features/daily",
    "derived/events",
    "derived/outcomes",
]


def dated_parts(data_root: Path, boundary: date) -> list[Path]:
    """Every `date=...` directory on or after the boundary."""
    found = []
    for store in DATED_STORES:
        folder = data_root / store
        if not folder.exists():
            continue
        for part in sorted(folder.glob("date=*")):
            try:
                session = date.fromisoformat(part.name.removeprefix("date="))
            except ValueError:
                continue
            if session >= boundary:
                found.append(part)
    return found


def snapshots(data_root: Path, boundary: date) -> list[Path]:
    """Universe snapshots rebalanced on or after the boundary.

    These carry the close and market cap on their rebalance date, which
    is holdout price data however incidental it looks.
    """
    folder = data_root / "universe"
    if not folder.exists():
        return []
    return [p for p in sorted(folder.glob("????-??-??.parquet"))
            if date.fromisoformat(p.stem) >= boundary]  # fmt: skip


def size_of(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--boundary", type=date.fromisoformat, default=HOLDOUT_FROM)
    parser.add_argument("--to", type=Path, help="Where to move it, e.g. /Volumes/VPAHoldout")
    parser.add_argument("--move", action="store_true", help="Actually move. Needs --to.")
    args = parser.parse_args(argv)

    parts = dated_parts(args.data_root, args.boundary)
    snaps = snapshots(args.data_root, args.boundary)
    total = sum(size_of(p) for p in parts) + sum(p.stat().st_size for p in snaps)

    print(f"\nHoldout boundary: {args.boundary} (Section 11.4's final 12 months)")
    print("=" * 66)
    by_store: dict[str, list[Path]] = {}
    for part in parts:
        by_store.setdefault(str(part.parent.relative_to(args.data_root)), []).append(part)
    for store, found in sorted(by_store.items()):
        print(f"  {store:<28} {len(found):>5} sessions  "
              f"{sum(size_of(p) for p in found) / 1e9:>7.2f} GB")  # fmt: skip
    print(f"  {'universe (snapshots)':<28} {len(snaps):>5} files     "
          f"{sum(p.stat().st_size for p in snaps) / 1e9:>7.2f} GB")  # fmt: skip
    print(f"\n  {'total':<28} {'':>5}           {total / 1e9:>7.2f} GB")

    if not args.move:
        print("\nNothing moved. Pass --move --to /Volumes/VPAHoldout to move it,")
        print("after reading docs/holdout-setup.md. The project owner runs that,")
        print("not the assistant.")
        return 0

    if not args.to:
        print("\n--move needs --to")
        return 1
    for part in parts:
        target = args.to / part.relative_to(args.data_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(part), str(target))
    for snap in snaps:
        target = args.to / snap.relative_to(args.data_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(snap), str(target))
    print(f"\nMoved {len(parts)} session directories and {len(snaps)} snapshots to {args.to}.")
    print("Unmount the image when you are done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
