"""Raw data ingestion from Massive/Polygon (Concept v2 Section 3).

Downloads, for a list of tickers and a date range:

- minute bars, **unadjusted**, including pre-market and after-hours
  (stored, but excluded from features later - Section 3.2);
- the corporate-actions tables: splits and dividends, as the vendor
  reports them (Section 3.1);
- each security's ticker-change history, used to stitch history across
  renames (see `vpa.data.tickers`) and kept for audit.

Everything lands in the write-once raw store (`vpa.data.raw_store`) under
`~/vpa-data/raw/`. Minute bars are partitioned by trading date. Work is done
a calendar month at a time, in batches of securities: a batch is only
written once every security in it has downloaded successfully, so a
failure never leaves a security half-stored, and re-running skips
anything already stored.

Run it with `uv run python -m vpa.data.ingest --help`.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from vpa.data.calendar import previous_session, sessions_between, sessions_ending
from vpa.data.massive import MassiveClient, results_object, ticker_path
from vpa.data.raw_store import DEFAULT_DATA_ROOT, manifests, new_run_id, write_part
from vpa.data.secrets import DEFAULT_SECRETS_FILE, RedactSecrets, load_secret
from vpa.data.tickers import (
    STATUS_OK,
    Identity,
    Segment,
    build_identity,
    fetch_ticker_events,
    segments,
)

log = logging.getLogger("vpa.ingest")

ET = "America/New_York"
MINUTE_DATASET = "minute"
MINUTE_COLUMNS = [
    "ticker",
    "requested_ticker",
    "security_key",
    "timestamp_utc",
    "date_et",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "transactions",
]

#: The small end-to-end test run: ten tickers, the last 30 trading days.
TEST_RUN_TICKERS = ["XYZ", "WSM", "DKS", "FIVE", "EXP", "TOL", "RBC", "LSCC", "ONTO", "CROX"]
TEST_RUN_SESSIONS = 30


@dataclass
class RunStats:
    requests_at_start: int
    started: float
    bars: int = 0
    files: int = 0
    stitched_segments: int = 0


# --- identities ---------------------------------------------------------------


def resolve_identity(client: MassiveClient, ticker: str, reference_date: date) -> Identity:
    """Look up the permanent ID and ticker history of `ticker` as of
    `reference_date`."""
    overview = results_object(
        client.get(
            f"/v3/reference/tickers/{ticker_path(ticker)}", {"date": reference_date.isoformat()}
        ),
        f"ticker details for {ticker}",
    )
    figi = overview.get("composite_figi")
    events = fetch_ticker_events(client, figi) if figi else []
    identity = build_identity(ticker, reference_date, figi, overview.get("name"), events)
    if identity.status != STATUS_OK:
        log.warning(
            "COVERAGE %s (%s): %s - using %s for the whole range, no stitching",
            ticker,
            identity.name,
            identity.status,
            ticker,
        )
    return identity


# --- minute bars --------------------------------------------------------------


def fetch_minutes(client: MassiveClient, ticker: str, start: date, end: date) -> pd.DataFrame:
    """Unadjusted 1-minute bars for `ticker`, ET dates `start`..`end`."""
    rows = list(
        client.get_all(
            f"/v2/aggs/ticker/{ticker_path(ticker)}/range/1/minute/"
            f"{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "false", "sort": "asc", "limit": 50000},
        )
    )
    table = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v", "vw", "n"])
    table = table.rename(
        columns={
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
            "vw": "vwap",
            "n": "transactions",
        }
    )
    table["timestamp_utc"] = pd.to_datetime(table.pop("t"), unit="ms", utc=True)
    table["date_et"] = table["timestamp_utc"].dt.tz_convert(ET).dt.date
    table["ticker"] = ticker
    # The API is asked for exactly these dates; drop anything outside them.
    return table[(table["date_et"] >= start) & (table["date_et"] <= end)]


def _concat(tables: list[pd.DataFrame]) -> pd.DataFrame:
    """Combine tables, skipping empty ones (a security with no trades)."""
    non_empty = [t for t in tables if not t.empty]
    if not non_empty:
        return tables[0].reindex(columns=list(dict.fromkeys([*tables[0].columns, *MINUTE_COLUMNS])))
    return pd.concat(non_empty, ignore_index=True)


def covered_keys(data_root: Path, day: date) -> set[str]:
    """Securities already stored for `day` (including ones with no trades)."""
    keys: set[str] = set()
    for manifest in manifests(data_root, MINUTE_DATASET, f"date={day.isoformat()}"):
        keys.update(manifest["securities"])
    return keys


#: Decides, for one security and one calendar month (first and last trading
#: day), which symbol(s) to fetch for which dates. An empty list means
#: "skip this security this month".
Planner = Callable[[Identity, date, date], list[Segment]]


def ingest_minutes(
    client: MassiveClient,
    data_root: Path,
    identities: list[Identity],
    start: date,
    end: date,
    run_id: str,
    stats: RunStats,
    planner: Planner = segments,
    threads: int = 1,
    batch_size: int = 200,
) -> None:
    """Download minute bars a calendar month at a time.

    Within a month, securities are downloaded in batches (`threads` at a
    time); a batch is written only once every security in it has
    downloaded, so a failure never leaves a security half-stored for a
    day. Re-running skips security-days already stored.
    """
    sessions = sessions_between(start, end)
    months = sorted({(d.year, d.month) for d in sessions})
    for month_number, (year, month) in enumerate(months, 1):
        month_sessions = [d for d in sessions if (d.year, d.month) == (year, month)]
        covered = {d: covered_keys(data_root, d) for d in month_sessions}
        plans = {}
        for identity in identities:
            plan = planner(identity, month_sessions[0], month_sessions[-1])
            days = [d for d in month_sessions if any(s.start <= d <= s.end for s in plan)]
            if any(identity.security_key not in covered[d] for d in days):
                plans[identity.security_key] = (identity, plan, days)
        label = f"{year}-{month:02d}"
        if not plans:
            log.info("Month %s (%d of %d): nothing to download", label, month_number, len(months))
            continue
        log.info(
            "Month %s (%d of %d): downloading %d securities",
            label,
            month_number,
            len(months),
            len(plans),
        )
        todo = list(plans.values())
        for first in range(0, len(todo), batch_size):
            batch = todo[first : first + batch_size]
            with ThreadPoolExecutor(threads) as pool:
                fetched = list(pool.map(lambda item: _fetch_planned(client, *item[:2]), batch))
            _write_days(data_root, run_id, batch, fetched, covered, stats)
            if len(todo) <= 20:
                for (identity, _, _), bars in zip(batch, fetched, strict=True):
                    log.info("  %-6s %s: %s bars", identity.requested, label, f"{len(bars):,}")
            else:
                log.info(
                    "  %s: %d of %d securities stored (%s bars)",
                    label,
                    first + len(batch),
                    len(todo),
                    f"{sum(len(b) for b in fetched):,}",
                )
            # Only once the batch is safely stored, so the audit log never
            # lists a stitch whose data wasn't written.
            for identity, plan, _ in batch:
                for segment in plan:
                    if segment.ticker != identity.requested:
                        record_stitch(data_root, run_id, identity, segment)
                        stats.stitched_segments += 1


def _fetch_planned(client: MassiveClient, identity: Identity, plan: list[Segment]) -> pd.DataFrame:
    parts = [fetch_minutes(client, s.ticker, s.start, s.end) for s in plan]
    bars = _concat(parts)
    bars["requested_ticker"] = identity.requested
    bars["security_key"] = identity.security_key
    return bars


def _write_days(data_root, run_id, batch, fetched, covered, stats: RunStats) -> None:
    """One new part per trading day, for this batch's securities."""
    bars = _concat(fetched)[MINUTE_COLUMNS]
    for day in sorted({d for _, _, days in batch for d in days}):
        new = [i for i, _, days in batch if day in days and i.security_key not in covered[day]]
        if not new:
            continue
        keys = {i.security_key for i in new}
        day_bars = bars[(bars["date_et"] == day) & bars["security_key"].isin(keys)].sort_values(
            ["security_key", "timestamp_utc"], ignore_index=True
        )
        counts = day_bars.groupby("security_key").size()
        tickers = day_bars.groupby("security_key")["ticker"].unique()
        write_part(
            data_root,
            MINUTE_DATASET,
            f"date={day.isoformat()}",
            run_id,
            day_bars,
            {
                "dataset": MINUTE_DATASET,
                "date_et": day.isoformat(),
                "adjusted": False,
                "securities": {
                    i.security_key: {
                        "requested_ticker": i.requested,
                        "tickers_used": sorted(tickers.get(i.security_key, [])),
                        "bars": int(counts.get(i.security_key, 0)),
                        "identity_status": i.status,
                    }
                    for i in new
                },
            },
        )
        covered[day] |= keys
        stats.files += 1
        stats.bars += len(day_bars)


def record_stitch(data_root: Path, run_id: str, identity: Identity, segment) -> None:
    """Log a stitch, and append it to the permanent audit log."""
    log.info(
        "STITCH %s (%s, %s): %s..%s fetched as %s",
        identity.requested,
        identity.name,
        identity.composite_figi,
        segment.start,
        segment.end,
        segment.ticker,
    )
    path = data_root / "logs" / "ticker_stitches.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.writer(handle)
        if is_new:
            writer.writerow(
                [
                    "run_id",
                    "requested_ticker",
                    "composite_figi",
                    "name",
                    "start",
                    "end",
                    "fetched_as",
                ]
            )
        writer.writerow(
            [
                run_id,
                identity.requested,
                identity.composite_figi,
                identity.name,
                segment.start,
                segment.end,
                segment.ticker,
            ]
        )


# --- corporate actions and ticker events --------------------------------------


def ingest_reference(
    client: MassiveClient,
    data_root: Path,
    identities: list[Identity],
    run_id: str,
    stats: RunStats,
    extra_tickers: dict[str, set[str]] | None = None,
    threads: int = 1,
) -> None:
    """Splits, dividends and ticker events for every security, stored
    under the date they were fetched (the vendor can revise these).

    Corporate actions are fetched under every symbol the security used:
    its ticker history, plus any in `extra_tickers` (security key ->
    symbols seen elsewhere, e.g. in the monthly ticker lists).
    """
    fetched_on = f"fetched={datetime.now(UTC).date().isoformat()}"
    extra_tickers = extra_tickers or {}
    jobs = [
        (identity, ticker)
        for identity in identities
        for ticker in dict.fromkeys(
            [*identity.all_tickers, *sorted(extra_tickers.get(identity.security_key, ()))]
        )
    ]

    def corporate_actions(job):
        identity, ticker = job
        params = {"ticker": ticker, "limit": 5000}
        tag = {"requested_ticker": identity.requested}
        splits = client.get_all("/stocks/v1/splits", params)
        dividends = client.get_all("/stocks/v1/dividends", params)
        return [{**r, **tag} for r in splits], [{**r, **tag} for r in dividends]

    with ThreadPoolExecutor(threads) as pool:
        results = list(pool.map(corporate_actions, jobs))
    splits = [row for found, _ in results for row in found]
    dividends = [row for _, found in results for row in found]
    events = []
    for identity in identities:
        for change in identity.changes:
            events.append(
                {
                    "requested_ticker": identity.requested,
                    "composite_figi": identity.composite_figi,
                    "name": identity.name,
                    "date": change.date,
                    "ticker": change.ticker,
                }
            )
    status = [
        {
            "requested_ticker": i.requested,
            "composite_figi": i.composite_figi,
            "name": i.name,
            "reference_date": i.reference_date,
            "status": i.status,
        }
        for i in identities
    ]

    split_table = pd.DataFrame(
        splits,
        columns=[
            "requested_ticker",
            "ticker",
            "id",
            "execution_date",
            "split_from",
            "split_to",
            "adjustment_type",
            "historical_adjustment_factor",
        ],
    )
    # Multiply prices before the split by this (Section 3.1). The vendor's
    # historical_adjustment_factor is cumulative up to *today*, so it is
    # kept for reference only and never used - that would be look-ahead.
    split_table["price_factor"] = split_table["split_from"] / split_table["split_to"]

    tables = {
        "splits": split_table,
        "dividends": pd.DataFrame(dividends),
        "ticker_events": pd.DataFrame(
            events, columns=["requested_ticker", "composite_figi", "name", "date", "ticker"]
        ),
        "identity_status": pd.DataFrame(status),
    }
    for dataset, table in tables.items():
        write_part(data_root, dataset, fetched_on, run_id, table, {"dataset": dataset})
        stats.files += 1
        log.info("Stored %s: %d rows", dataset, len(table))


# --- the whole run ------------------------------------------------------------


def run(
    client: MassiveClient,
    data_root: Path,
    tickers: list[str],
    start: date,
    end: date,
    reference_date: date,
) -> RunStats:
    run_id = new_run_id()
    stats = RunStats(requests_at_start=client.request_count, started=time.monotonic())
    log.info(
        "Ingest run %s: %d tickers, %s to %s, into %s", run_id, len(tickers), start, end, data_root
    )

    identities = []
    for ticker in tickers:
        identity = resolve_identity(client, ticker, reference_date)
        log.info(
            "Identified %-6s = %s (%s), %d ticker changes on record, status %s",
            ticker,
            identity.name,
            identity.composite_figi,
            len(identity.changes),
            identity.status,
        )
        identities.append(identity)
    if len({i.security_key for i in identities}) != len(identities):
        raise ValueError("Two requested tickers are the same security - check the ticker list")

    ingest_minutes(client, data_root, identities, start, end, run_id, stats)
    ingest_reference(client, data_root, identities, run_id, stats)
    summarise(data_root, identities, start, end, stats, client)
    return stats


def summarise(data_root, identities, start, end, stats: RunStats, client) -> None:
    elapsed = time.monotonic() - stats.started
    requests_made = client.request_count - stats.requests_at_start
    statuses = Counter(i.status for i in identities)
    log.info("=" * 70)
    log.info("INGEST COMPLETE")
    log.info("  Securities:        %d", len(identities))
    log.info("  Identity status:   %s", dict(statuses))
    log.info("  Stitched segments: %d (see logs/ticker_stitches.csv)", stats.stitched_segments)
    log.info("  Minute bars:       %s written in %d files", f"{stats.bars:,}", stats.files)
    log.info(
        "  API requests:      %d in %.0fs (%.1f per second)",
        requests_made,
        elapsed,
        requests_made / elapsed if elapsed else 0,
    )
    traded: dict[date, set[str]] = {}
    for day in sessions_between(start, end):
        traded[day] = {
            key
            for m in manifests(data_root, MINUTE_DATASET, f"date={day.isoformat()}")
            for key, info in m["securities"].items()
            if info["bars"]
        }
    for identity in identities:
        empty = [d for d, keys in traded.items() if identity.security_key not in keys]
        if empty:
            log.warning(
                "  %s has NO bars on %d trading day(s), first %s - possible halt, delisting "
                "or a rename the vendor doesn't record",
                identity.requested,
                len(empty),
                empty[0],
            )
    log.info("=" * 70)


def setup_logging(data_root: Path, name: str) -> Path:
    """Log to the terminal and to a file under `<data_root>/logs/`."""
    run_log = data_root / "logs" / f"{name}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.log"
    run_log.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(run_log)],
    )
    return run_log


def make_client(secrets_file: Path, requests_per_second: float) -> MassiveClient:
    """Load the API key (never logged) and build a client."""
    api_key = load_secret("MASSIVE_API_KEY", secrets_file)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecrets(api_key))
    return MassiveClient(api_key, requests_per_second=requests_per_second)


def default_test_window(today: date) -> tuple[date, date]:
    """The last 30 completed trading days before `today`."""
    days = sessions_ending(previous_session(today), TEST_RUN_SESSIONS)
    return days[0], days[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.ingest",
        description="Download raw minute bars, splits, dividends and ticker changes "
        "from Massive into the write-once raw store. Reads MASSIVE_API_KEY from the "
        "secrets file at run time.",
    )
    parser.add_argument(
        "--tickers",
        help="Comma-separated tickers. Default: the 10-ticker test set.",
        default=",".join(TEST_RUN_TICKERS),
    )
    parser.add_argument("--start", type=date.fromisoformat, help="First date (YYYY-MM-DD)")
    parser.add_argument("--end", type=date.fromisoformat, help="Last date (YYYY-MM-DD)")
    parser.add_argument(
        "--reference-date",
        type=date.fromisoformat,
        help="Date on which the tickers are valid symbols. Default: --end.",
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    parser.add_argument(
        "--requests-per-second", type=float, default=10.0, help="API pacing (default 10)"
    )
    args = parser.parse_args(argv)

    if (args.start is None) != (args.end is None):
        parser.error("give both --start and --end, or neither for the 30-day test window")
    start, end = (args.start, args.end) if args.start else default_test_window(date.today())
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    run_log = setup_logging(args.data_root, "ingest")
    try:
        client = make_client(args.secrets_file, args.requests_per_second)
        run(client, args.data_root, tickers, start, end, args.reference_date or end)
    except Exception as exc:
        log.exception("INGEST FAILED: %s: %s", type(exc).__name__, exc)
        log.error("Nothing from an unfinished month was written. Re-run to resume.")
        return 1
    log.info("Log saved to %s", run_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
