"""Diagnostic: why is the vendor's share count missing for some stocks?

The universe uses the vendor's **weighted** shares outstanding (LEDGER-1
amendment 1). For roughly 80 stocks a month it comes back empty, and those
stocks are excluded from that month's list (recorded in
`logs/share_count_problems.csv`).

This asks the vendor, for a sample of those cases, which share-count
fields it does have:

- `weighted_shares_outstanding`: what the universe uses - assumes every
  other share class converted into this one.
- `share_class_shares_outstanding`: the count for this share class alone.
  For a company with a single share class the two are the same number.
- `market_cap`: if present, dividing by the close gives the share count
  the vendor itself used.

It also samples stocks where the weighted count *was* present, and
compares the two fields, to show how often they agree.

`--history TICKER[,TICKER]` instead follows both counts for named
companies across the whole period. That shows whether a count actually
changes with the date asked for, or is the same number repeated - which
would mean it is not point-in-time at all.

**It only reports.** It changes no data and no rules - swapping in a
fallback would change how market cap is defined, which is the project
owner's decision and a ledger amendment.

Run it with `uv run python -m vpa.data.check_share_counts --help`.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from vpa.data.check_market_cap import daily_close
from vpa.data.ingest import make_client, setup_logging
from vpa.data.massive import MassiveClient, MassiveError, results_object, ticker_path
from vpa.data.raw_store import DEFAULT_DATA_ROOT, read_partition
from vpa.data.secrets import DEFAULT_SECRETS_FILE

log = logging.getLogger("vpa.check_share_counts")

MISSING_STATUS = "missing"


def share_fields(client: MassiveClient, ticker: str, day: date) -> dict:
    """Every share-count-related field the vendor has for `ticker` on `day`."""
    row = {"ticker": ticker, "date": day}
    try:
        response = client.get(
            f"/v3/reference/tickers/{ticker_path(ticker)}", {"date": day.isoformat()}
        )
    except MassiveError as exc:
        if exc.status_code != 404:
            raise
        return {**row, "found": False}
    overview = results_object(response, f"ticker details for {ticker}")
    close = daily_close(client, ticker, day)
    market_cap = overview.get("market_cap")
    weighted = overview.get("weighted_shares_outstanding")
    share_class = overview.get("share_class_shares_outstanding")
    return {
        **row,
        "found": True,
        "type": overview.get("type"),
        "weighted_shares": weighted,
        "share_class_shares": share_class,
        "market_cap": market_cap,
        "close": close,
        "shares_from_market_cap": market_cap / close if market_cap and close else None,
        "share_class_over_weighted": share_class / weighted if weighted and share_class else None,
    }


def sample_rows(rows: list[dict], sample: int) -> list[dict]:
    """An even spread through `rows` (they are in date order)."""
    if len(rows) <= sample:
        return rows
    step = len(rows) / sample
    return [rows[int(i * step)] for i in range(sample)]


def missing_cases(data_root: Path, sample: int) -> list[tuple[str, date]]:
    """Cases where the weighted share count came back empty."""
    path = data_root / "logs" / "share_count_problems.csv"
    if not path.exists():
        raise FileNotFoundError(f"No share-count log yet: {path}")
    with path.open() as handle:
        rows = [r for r in csv.DictReader(handle) if r["status"] == MISSING_STATUS]
    return [
        (r["ticker_on_date"], date.fromisoformat(r["shares_date"]))
        for r in sample_rows(rows, sample)
    ]


def working_cases(data_root: Path, sample: int, seed: int = 0) -> list[tuple[str, date]]:
    """Cases where the weighted share count was there, for comparison.

    A different company each time: taking the first row of each file would
    pick the same alphabetically-first ticker over and over.
    """
    folders = sorted((data_root / "raw" / "share_counts").glob("date=*"))
    cases: list[tuple[str, date]] = []
    seen: set[str] = set()
    for n, folder in enumerate(folders[:: max(1, len(folders) // sample)]):
        day = date.fromisoformat(folder.name.removeprefix("date="))
        stored = read_partition(data_root, "share_counts", folder.name)
        found = stored[(stored["status"] == "found") & ~stored["ticker_on_date"].isin(seen)]
        if not found.empty:
            ticker = found["ticker_on_date"].sample(1, random_state=seed + n).iloc[0]
            seen.add(ticker)
            cases.append((ticker, day))
    return cases[:sample]


def history_cases(data_root: Path, tickers: list[str], points: int) -> list[tuple[str, date]]:
    """The same companies asked about on dates spread across the period."""
    folders = sorted((data_root / "raw" / "share_counts").glob("date=*"))
    days = [date.fromisoformat(f.name.removeprefix("date=")) for f in folders]
    chosen = sample_rows(days, points)
    return [(t, d) for t in tickers for d in chosen]


def report(client: MassiveClient, cases: list[tuple[str, date]], title: str) -> pd.DataFrame:
    table = pd.DataFrame([share_fields(client, t, d) for t, d in cases])
    log.info("--- %s (%d cases)", title, len(table))
    with pd.option_context("display.width", 200, "display.max_columns", None):
        log.info("\n%s", table)
    return table


def _save(tables: list[pd.DataFrame], data_root: Path) -> Path:
    out = data_root / "logs" / f"share-count-check-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.csv"
    filled = [t for t in tables if not t.empty]
    pd.concat(filled, ignore_index=True).to_csv(out, index=False)
    return out


def _history_summary(history: pd.DataFrame, data_root: Path) -> int:
    """How much each count moves over the period, per company."""
    log.info("=" * 70)
    log.info("DOES EACH COUNT CHANGE WITH THE DATE ASKED FOR?")
    for ticker, rows in history.groupby("ticker"):
        for column in ("weighted_shares", "share_class_shares"):
            values = rows[column].dropna()
            distinct = values.nunique()
            spread = (values.max() / values.min() - 1) * 100 if len(values) and values.min() else 0
            log.info(
                "  %-6s %-18s %2d distinct value(s) over %d dates, spread %.1f%%",
                ticker,
                column,
                distinct,
                len(values),
                spread,
            )
    log.info("Full results: %s", _save([history], data_root))
    log.info("A count that never changes is not point-in-time.")
    log.info("=" * 70)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.check_share_counts",
        description="Report which share-count fields the vendor has where the universe "
        "found none. Read-only: changes no data and no rules.",
    )
    parser.add_argument("--sample", type=int, default=15, help="Cases of each kind (default 15)")
    parser.add_argument(
        "--history",
        help="Instead: follow these tickers (comma separated) across the whole period, to see "
        "whether each count actually changes with the date asked for.",
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    args = parser.parse_args(argv)

    run_log = setup_logging(args.data_root, "check-share-counts")
    try:
        client = make_client(args.secrets_file, requests_per_second=10.0)
        if args.history:
            tickers = [t.strip().upper() for t in args.history.split(",") if t.strip()]
            history = report(
                client,
                history_cases(args.data_root, tickers, args.sample),
                "SHARE COUNTS OVER TIME",
            )
            return _history_summary(history, args.data_root)
        missing = report(client, missing_cases(args.data_root, args.sample), "NO WEIGHTED COUNT")
        working = report(client, working_cases(args.data_root, args.sample), "WEIGHTED COUNT FOUND")
    except Exception as exc:
        log.exception("SHARE COUNT CHECK FAILED: %s: %s", type(exc).__name__, exc)
        return 1

    out = _save(
        [missing.assign(sample="no_weighted_count"), working.assign(sample="weighted_count_found")],
        args.data_root,
    )

    log.info("=" * 70)
    log.info("WHERE THE UNIVERSE FOUND NO WEIGHTED SHARE COUNT (%d sampled):", len(missing))
    if not missing.empty:
        log.info("  vendor has no record at all:      %d", int((~missing["found"]).sum()))
    if not missing.empty and missing["found"].any():
        present = missing[missing["found"]]
        log.info(
            "  has a share-class count instead:  %d",
            int(present["share_class_shares"].notna().sum()),
        )
        log.info("  has a market cap instead:         %d", int(present["market_cap"].notna().sum()))
        log.info(
            "  has neither:                      %d",
            int((present["share_class_shares"].isna() & present["market_cap"].isna()).sum()),
        )
    ratios = working["share_class_over_weighted"].dropna()
    if not ratios.empty:
        log.info("WHERE BOTH COUNTS EXIST (%d sampled):", len(ratios))
        log.info(
            "  share-class / weighted: identical in %d of %d", int((ratios == 1).sum()), len(ratios)
        )
        log.info("  range: %.4f to %.4f", ratios.min(), ratios.max())
    log.info("Full results: %s   Log: %s", out, run_log)
    log.info("This is a report only - no rule has changed.")
    log.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
