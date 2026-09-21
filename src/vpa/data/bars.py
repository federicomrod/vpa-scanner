"""Building and reading hourly and daily bars.

Reads the immutable raw minute store, applies the Section 4 rules
(`vpa.signal.bars`), and writes the result under `~/vpa-data/derived/`,
one file per session:

    derived/hourly/date=YYYY-MM-DD/bars.parquet
    derived/daily/date=YYYY-MM-DD/bars.parquet

Derived data is **regenerable**, unlike the raw store: rebuilding a
session replaces its file. Nothing here ever writes to `raw/`.

Each session records which raw parts it was built from, so a session
built when only some securities had been downloaded is rebuilt
automatically once the rest arrive. Deciding "already built" from the
file's existence alone is how a session ends up holding ten securities
when the raw store has a thousand.

Prices are stored exactly as traded. `read_bars(..., as_of=...)` applies
split adjustment at read time (Concept v2 Section 3.1), using the splits
in the raw corporate-actions tables, and never dividend adjustment.

Run it with `uv run python -m vpa.data.bars --help`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time as timer
from datetime import date
from pathlib import Path

import pandas as pd

from vpa.data.calendar import nyse_calendar, sessions_between
from vpa.data.ingest import setup_logging
from vpa.data.raw_store import DEFAULT_DATA_ROOT, read_partition
from vpa.signal.adjust import split_adjust_daily
from vpa.signal.bars import ET, daily_bars, hourly_bars

log = logging.getLogger("vpa.bars")

HOURLY, DAILY = "hourly", "daily"

#: The raw dataset bars are built from.
MINUTE_DATASET = "minute"


def derived_path(data_root: Path, kind: str, session: date) -> Path:
    return data_root / "derived" / kind / f"date={session.isoformat()}" / "bars.parquet"


def sources_path(data_root: Path, session: date) -> Path:
    """Where a session records the raw parts its bars were built from."""
    return data_root / "derived" / HOURLY / f"date={session.isoformat()}" / "sources.json"


def raw_parts(data_root: Path, session: date) -> list[str]:
    """The raw minute parts currently stored for a session."""
    folder = data_root / "raw" / MINUTE_DATASET / f"date={session.isoformat()}"
    return sorted(p.name for p in folder.glob("part-*.manifest.json"))


def needs_building(data_root: Path, session: date) -> bool:
    """Whether a session has no bars, or has bars built from less raw
    data than is now stored."""
    if not derived_path(data_root, HOURLY, session).exists():
        return True
    recorded = sources_path(data_root, session)
    if not recorded.exists():
        return True  # built before sources were tracked: rebuild once
    return json.loads(recorded.read_text()) != raw_parts(data_root, session)


def session_bounds_for(session: date) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The real open and close of a session, from the exchange calendar -
    which knows about half days."""
    calendar = nyse_calendar()
    day = pd.Timestamp(session)
    return (
        calendar.session_open(day).tz_convert(ET),
        calendar.session_close(day).tz_convert(ET),
    )


def raw_sessions(data_root: Path) -> list[date]:
    """Every session with raw minute data stored."""
    folder = data_root / "raw" / "minute"
    return sorted(
        date.fromisoformat(p.name.removeprefix("date="))
        for p in folder.glob("date=*")
        if any(p.glob("*.manifest.json"))
    )


def build_session(data_root: Path, session: date) -> tuple[int, int]:
    """Build one session's hourly and daily bars. Returns the row counts."""
    minutes = read_partition(data_root, "minute", f"date={session.isoformat()}")
    if minutes.empty:
        raise ValueError(f"No raw minute data stored for {session}")
    before = len(minutes)
    minutes = minutes.drop_duplicates(["security_key", "timestamp_utc"])
    if len(minutes) < before:
        log.warning("%s: ignored %d duplicate minute rows", session, before - len(minutes))

    session_open, session_close = session_bounds_for(session)
    hourly = hourly_bars(minutes, session_open, session_close)
    daily = daily_bars(minutes, session_open, session_close)
    _write(derived_path(data_root, HOURLY, session), hourly)
    _write(derived_path(data_root, DAILY, session), daily)
    sources_path(data_root, session).write_text(json.dumps(raw_parts(data_root, session)))
    return len(hourly), len(daily)


def build(data_root: Path, sessions: list[date], rebuild: bool = False) -> int:
    """Build every session given, skipping ones already built unless
    `rebuild`. Returns how many sessions were built."""
    built = 0
    started = timer.monotonic()
    for n, session in enumerate(sessions, 1):
        if not rebuild and not needs_building(data_root, session):
            continue
        hourly, daily = build_session(data_root, session)
        built += 1
        if built % 20 == 0 or n == len(sessions):
            log.info(
                "  %s (%d of %d): %d hourly, %d daily bars",
                session,
                n,
                len(sessions),
                hourly,
                daily,
            )
    log.info("Built %d of %d sessions in %.0fs", built, len(sessions), timer.monotonic() - started)
    return built


def read_bars(
    data_root: Path, kind: str, sessions: list[date], as_of: date | None = None
) -> pd.DataFrame:
    """Bars for the given sessions, split-adjusted as of `as_of` if given.

    Adjustment uses only splits that had taken effect by `as_of`, so a
    later split never changes an earlier bar (CLAUDE.md rule 5).
    """
    tables = [
        pd.read_parquet(derived_path(data_root, kind, s))
        for s in sessions
        if derived_path(data_root, kind, s).exists()
    ]
    if not tables:
        return pd.DataFrame()
    bars = pd.concat(tables, ignore_index=True)
    if as_of is None:
        return bars
    return split_adjust_daily(bars, load_splits(data_root), as_of, ticker_column="requested_ticker")


def load_splits(data_root: Path) -> pd.DataFrame:
    """Splits from the raw corporate-actions table, keyed by the security's
    current ticker (`requested_ticker`), newest fetch per split kept."""
    folder = data_root / "raw" / "splits"
    parts = [
        read_partition(data_root, "splits", p.name)
        for p in sorted(folder.glob("fetched=*"))
        if any(p.glob("*.manifest.json"))
    ]
    if not parts:
        return pd.DataFrame(columns=["ticker", "execution_date", "split_from", "split_to"])
    splits = pd.concat(parts, ignore_index=True).drop_duplicates(
        ["requested_ticker", "execution_date", "split_from", "split_to"], keep="last"
    )
    return splits.assign(ticker=splits["requested_ticker"])


def _write(path: Path, table: pd.DataFrame) -> None:
    """Replace the file for this session (derived data is regenerable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    table.to_parquet(partial, index=False)
    os.replace(partial, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.bars",
        description="Build hourly and daily bars (Concept v2 Section 4) from the raw "
        "minute store. Default: every session with minute data that has no bars yet.",
    )
    parser.add_argument("--start", type=date.fromisoformat, help="First session (YYYY-MM-DD)")
    parser.add_argument("--end", type=date.fromisoformat, help="Last session (YYYY-MM-DD)")
    parser.add_argument(
        "--rebuild", action="store_true", help="Rebuild sessions that already have bars"
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args(argv)

    setup_logging(args.data_root, "build-bars")
    try:
        sessions = raw_sessions(args.data_root)
        if args.start or args.end:
            wanted = set(sessions_between(args.start or sessions[0], args.end or sessions[-1]))
            sessions = [s for s in sessions if s in wanted]
        if not sessions:
            log.error("BAR BUILD FAILED: no raw minute data for those dates")
            return 1
        log.info(
            "Sessions with minute data: %d (%s to %s)", len(sessions), sessions[0], sessions[-1]
        )
        build(args.data_root, sessions, args.rebuild)
    except Exception as exc:
        log.exception("BAR BUILD FAILED: %s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
