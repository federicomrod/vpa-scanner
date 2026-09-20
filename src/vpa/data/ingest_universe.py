"""Minute-bar download for every stock that ever enters a universe.

Owner's decision (option A): download each such stock's full listed life
within the data period, not just the months it was in a universe - it
covers every lookback and follow-up window, and nothing needs fetching
again later. SPY is added too (Concept v2 Section 5.5); sector funds wait
until the sector source (Section 16, item 2) is decided.

"Listed life" comes from the monthly lists of every listed ticker that the
universe build stores (`tickers_listed`), taken at the end of each month.
A stock is downloaded for a calendar month if it was listed at the end of
the month before or at the end of the month itself. This also bounds the
download at delisting, so a symbol later reused by another company is
not fetched as if it were the same stock. (Residual risk, accepted: a
symbol delisted and reused by another company within the same calendar
month.)

Which symbol to ask for:
- If the vendor's ticker history is usable, it decides (renames stitched
  and logged, exactly as in `vpa.data.ingest`). It must agree with the
  monthly lists; if it doesn't, the month is skipped and logged.
- Otherwise the monthly lists decide. A month in which the list shows the
  symbol changing is skipped and logged - the exact switch date is
  unknown, and it isn't guessed.

Every skipped month is logged to `logs/minute_months_skipped.csv`.

Run it with `uv run python -m vpa.data.ingest_universe --help`.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from vpa.data.calendar import previous_session
from vpa.data.ingest import (
    RunStats,
    ingest_minutes,
    ingest_reference,
    make_client,
    setup_logging,
)
from vpa.data.massive import MassiveClient
from vpa.data.raw_store import DEFAULT_DATA_ROOT, new_run_id
from vpa.data.secrets import DEFAULT_SECRETS_FILE
from vpa.data.tickers import STATUS_OK, Identity, Segment, segments
from vpa.data.universe_inputs import LOOKUP_THREADS, SecurityInfoStore, ensure_listed_tickers

log = logging.getLogger("vpa.ingest_universe")

#: The first full month the Developer plan's 10 years of history covers.
DEFAULT_FIRST_MONTH = "2016-10"
EXTRA_TICKERS = ["SPY"]


@dataclass
class Target:
    """One security to download, and the ticker it was listed under at
    each month-end (from the monthly lists)."""

    key: str
    composite_figi: str | None
    listed: dict[date, str] = field(default_factory=dict)

    @property
    def latest(self) -> tuple[date, str]:
        day = max(self.listed)
        return day, self.listed[day]


def months_in(first_month: str, last_month: str) -> list[date]:
    """The first calendar day of each month in the range."""
    month = date.fromisoformat(f"{first_month}-01")
    last = date.fromisoformat(f"{last_month}-01")
    months = []
    while month <= last:
        months.append(month)
        month = (month + timedelta(days=32)).replace(day=1)
    return months


def month_ends(first_month: str, last_month: str, today: date) -> list[date]:
    """Every month-end a ticker list is needed for: the last trading day
    before each month, and before the month after (if already past)."""
    months = months_in(first_month, last_month)
    following = (months[-1] + timedelta(days=32)).replace(day=1)
    return sorted(
        {previous_session(m) for m in [*months, following] if previous_session(m) < today}
    )


def load_targets(
    universe_dir: Path, lists: dict[date, pd.DataFrame], extra: list[str]
) -> dict[str, Target]:
    """Every security in any universe snapshot, plus `extra` tickers."""
    snapshots = [pd.read_parquet(p) for p in sorted(universe_dir.glob("????-??-??.parquet"))]
    if not snapshots:
        raise FileNotFoundError(f"No universe snapshots in {universe_dir}")
    members = pd.concat(snapshots, ignore_index=True)[["ticker", "composite_figi"]]
    latest_list = lists[max(lists)]
    for ticker in extra:
        row = latest_list[latest_list["ticker"] == ticker]
        if row.empty:
            raise ValueError(f"{ticker} is not in the latest ticker list")
        members.loc[len(members)] = [ticker, row["composite_figi"].iloc[0]]

    targets: dict[str, Target] = {}
    for ticker, figi in members.drop_duplicates().itertuples(index=False):
        figi = figi if isinstance(figi, str) and figi else None
        key = figi or f"TICKER:{ticker}"
        targets.setdefault(key, Target(key, figi))
    by_figi = {t.composite_figi: t for t in targets.values() if t.composite_figi}
    by_ticker = {t.key.removeprefix("TICKER:"): t for t in targets.values() if not t.composite_figi}
    for day, listed in lists.items():
        for ticker, figi in listed[["ticker", "composite_figi"]].itertuples(index=False):
            target = by_figi.get(figi) if isinstance(figi, str) and figi else by_ticker.get(ticker)
            if target:
                target.listed[day] = ticker
    return targets


class ListPlanner:
    """Decides which symbol to fetch for each security-month (see module
    docstring), and logs every month it has to skip."""

    def __init__(self, targets: dict[str, Target], today: date, data_root: Path, run_id: str):
        self.targets = targets
        self.today = today
        self.skips: list[dict] = []
        self._log_path = data_root / "logs" / "minute_months_skipped.csv"
        self._run_id = run_id

    def __call__(self, identity: Identity, first: date, last: date) -> list[Segment]:
        target = self.targets[identity.security_key]
        before, end = previous_session(first), last
        at_start = target.listed.get(before)
        at_end = target.listed.get(end) if end < self.today else None
        if not at_start and not at_end:
            return []  # not listed this month
        if identity.status == STATUS_OK:
            for day, listed_as in ((before, at_start), (end, at_end)):
                if listed_as and segments(identity, day, day)[0].ticker != listed_as:
                    return self._skip(identity, first, "ticker history disagrees with the list")
            first_listed = identity.changes[0].date
            return [
                Segment(s.ticker, max(s.start, first_listed), s.end)
                for s in segments(identity, first, last)
                if s.end >= first_listed
            ]
        if at_start and at_end and at_start != at_end:
            return self._skip(identity, first, f"symbol changed {at_start}->{at_end}, no history")
        return [Segment(at_start or at_end, first, last)]

    def _skip(self, identity: Identity, first: date, reason: str) -> list[Segment]:
        row = {
            "run_id": self._run_id,
            "security_key": identity.security_key,
            "requested_ticker": identity.requested,
            "month": f"{first:%Y-%m}",
            "reason": reason,
        }
        self.skips.append(row)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self._log_path.exists()
        with self._log_path.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            if is_new:
                writer.writeheader()
            writer.writerow(row)
        log.warning("  SKIPPED %s %s: %s", identity.requested, row["month"], reason)
        return []


def run(
    client: MassiveClient,
    data_root: Path,
    universe_dir: Path,
    first_month: str,
    last_month: str,
    today: date,
    threads: int = LOOKUP_THREADS,
) -> RunStats:
    run_id = new_run_id()
    stats = RunStats(requests_at_start=client.request_count, started=time.monotonic())

    list_days = month_ends(first_month, last_month, today)
    log.info("Monthly ticker lists needed: %d (stored ones are reused)", len(list_days))
    lists = {d: ensure_listed_tickers(client, data_root, d, run_id) for d in list_days}
    targets = load_targets(universe_dir, lists, EXTRA_TICKERS)
    targets = {k: t for k, t in targets.items() if t.listed}
    log.info("Securities to download: %d (every universe member, plus SPY)", len(targets))

    # Identities (ticker histories) from the stored security info, looked
    # up as of the last month-end each security was listed.
    store = SecurityInfoStore(client, data_root)
    identities = []
    by_day: dict[date, list[Target]] = {}
    for target in targets.values():
        by_day.setdefault(target.latest[0], []).append(target)
    for day, group in sorted(by_day.items()):
        rows = pd.DataFrame(
            [(t.latest[1], t.composite_figi) for t in group], columns=["ticker", "composite_figi"]
        )
        infos = store.get_many(rows, day)
        identities += [infos[t.latest[1]].identity for t in group]
    store.save(run_id)

    months = months_in(first_month, last_month)
    start = months[0]
    month_after = (months[-1] + timedelta(days=32)).replace(day=1)
    end = min(month_after - timedelta(days=1), previous_session(today))
    planner = ListPlanner(targets, today, data_root, run_id)
    ingest_minutes(
        client, data_root, identities, start, end, run_id, stats,
        planner=planner, threads=threads,
    )  # fmt: skip

    symbols = {k: set(t.listed.values()) for k, t in targets.items()}
    ingest_reference(client, data_root, identities, run_id, stats, symbols, threads=threads)

    elapsed = time.monotonic() - stats.started
    requests_made = client.request_count - stats.requests_at_start
    log.info("=" * 70)
    log.info("UNIVERSE MINUTE DOWNLOAD COMPLETE")
    log.info("  Securities:        %d", len(identities))
    log.info("  Stitched segments: %d (see logs/ticker_stitches.csv)", stats.stitched_segments)
    log.info("  Months skipped:    %d (see logs/minute_months_skipped.csv)", len(planner.skips))
    log.info("  Minute bars:       %s written in %d files", f"{stats.bars:,}", stats.files)
    log.info(
        "  API requests:      %d in %.0fs (%.1f per second)",
        requests_made,
        elapsed,
        requests_made / elapsed if elapsed else 0,
    )
    log.info("=" * 70)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.ingest_universe",
        description="Download minute bars for every universe member (plus SPY), for its "
        "whole listed life in the given months.",
    )
    parser.add_argument("--first-month", default=DEFAULT_FIRST_MONTH, help="YYYY-MM")
    parser.add_argument(
        "--last-month", default=f"{previous_session(date.today()):%Y-%m}", help="YYYY-MM"
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--universe-dir", type=Path, help="Default: <data root>/universe")
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    parser.add_argument("--requests-per-second", type=float, default=10.0)
    args = parser.parse_args(argv)

    run_log = setup_logging(args.data_root, "ingest-universe")
    try:
        client = make_client(args.secrets_file, args.requests_per_second)
        run(
            client,
            args.data_root,
            args.universe_dir or args.data_root / "universe",
            args.first_month,
            args.last_month,
            date.today(),
        )
    except Exception as exc:
        log.error("UNIVERSE MINUTE DOWNLOAD FAILED: %s: %s", type(exc).__name__, exc)
        log.error("Everything already stored is kept. Re-run to continue.")
        return 1
    log.info("Log saved to %s", run_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
