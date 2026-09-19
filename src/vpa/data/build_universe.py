"""Building the monthly universe snapshots from real data.

For each month: download (or reuse) the whole-market inputs, join up
renamed stocks' history inside the 60-day window, apply the Section 2
rules (`vpa.signal.universe`), and write `universe/YYYY-MM-DD.parquet`
once. Months whose snapshot already exists are skipped, so an
interrupted run can simply be started again.

Audit logs, under `<data root>/logs/` (appended to, never rewritten):

- `ticker_stitches.csv`: every rename joined up (shared with the minute
  download).
- `listing_date_discrepancies.csv`: every shortlisted stock whose vendor
  listing date differs from its first ticker event (LEDGER-1 amendment 2).
- `share_count_problems.csv`: every shortlisted stock whose share count
  couldn't be found - it can't qualify, and is listed here rather than
  guessed at.
- `market_cap_audit.csv`: for each selected stock, our market cap next
  to the vendor's own figure for the same day. The vendor's figure is
  never used for selection (LEDGER-1 amendment 1); a big gap flags a
  possible share-count problem for review.

Run it with `uv run python -m vpa.data.build_universe --help`.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from vpa.data.calendar import previous_session, sessions_between, sessions_ending
from vpa.data.ingest import make_client, record_stitch, setup_logging
from vpa.data.massive import MassiveClient, MassiveError
from vpa.data.raw_store import DEFAULT_DATA_ROOT, new_run_id
from vpa.data.secrets import DEFAULT_SECRETS_FILE
from vpa.data.tickers import STATUS_OK, segments
from vpa.data.universe import build_snapshot, read_snapshot, snapshot_path
from vpa.data.universe_inputs import (
    LOOKUP_THREADS,
    SHARES_FOUND,
    SecurityInfo,
    SecurityInfoStore,
    ensure_grouped_daily,
    ensure_listed_tickers,
    fetch_market_splits,
    fetch_share_counts,
    load_grouped_daily,
)
from vpa.signal.universe import (
    UniverseRules,
    is_rebalance_date,
    prescreen,
    share_count_date,
)

log = logging.getLogger("vpa.build_universe")

#: Our market cap and the vendor's differing by more than this fraction is
#: logged as a warning. Audit only - it never changes what is selected.
MARKET_CAP_AUDIT_WARN = 0.25


def rebalance_dates(first_month: str, last_month: str) -> list[date]:
    """First trading day of each month from YYYY-MM to YYYY-MM inclusive."""
    start = date.fromisoformat(f"{first_month}-01")
    end = date.fromisoformat(f"{last_month}-01")
    days = sessions_between(start, end + timedelta(days=40))
    return [d for d in days if is_rebalance_date(d) and start <= d.replace(day=1) <= end]


def build_month(
    client: MassiveClient,
    data_root: Path,
    universe_dir: Path,
    rebalance_date: date,
    splits: pd.DataFrame,
    store: SecurityInfoStore,
    run_id: str,
) -> Path | None:
    if snapshot_path(universe_dir, rebalance_date).exists():
        log.info("Universe %s: already built, skipping", rebalance_date)
        return None
    rules = UniverseRules()
    as_of = previous_session(rebalance_date)
    window = sessions_ending(as_of, rules.dollar_volume_sessions)
    log.info("Universe %s (measured at the %s close)", rebalance_date, as_of)

    ensure_grouped_daily(client, data_root, window, run_id)
    listed = ensure_listed_tickers(client, data_root, as_of, run_id)
    bars = load_grouped_daily(data_root, window)
    securities = listed[["ticker", "type", "primary_exchange", "composite_figi", "name"]].copy()

    bars, splits = stitch_window(
        bars, splits, listed, store, window, as_of, rules, data_root, run_id
    )

    shortlist = prescreen(rebalance_date, securities, bars, splits)
    log.info("  %d of %d listed stocks pass the cheap checks", len(shortlist), len(listed))
    infos = store.get_many(listed[listed["ticker"].isin(shortlist)], as_of)
    shares_date = share_count_date(rebalance_date)
    shares = fetch_share_counts(client, data_root, list(infos.values()), shares_date, run_id)
    store.save(run_id)

    by_key = shares.drop_duplicates("key").set_index("key")
    securities["list_date"] = securities["ticker"].map(lambda t: _attr(infos, t, "list_date"))
    securities["first_event_date"] = securities["ticker"].map(
        lambda t: _attr(infos, t, "first_event_date")
    )
    securities["weighted_shares"] = securities["ticker"].map(
        lambda t: by_key["weighted_shares"].get(infos[t].key) if t in infos else None
    )

    audit_listing_dates(data_root, rebalance_date, infos)
    audit_share_counts(data_root, rebalance_date, shares, shares_date)
    path = build_snapshot(rebalance_date, securities, bars, splits, universe_dir, rules)
    audit_market_cap(client, data_root, read_snapshot(universe_dir, rebalance_date), as_of)
    return path


def _attr(infos: dict[str, SecurityInfo], ticker: str, name: str):
    return getattr(infos[ticker], name) if ticker in infos else None


# --- renames inside the dollar-volume window ----------------------------------


def stitch_window(
    bars: pd.DataFrame,
    splits: pd.DataFrame,
    listed: pd.DataFrame,
    store: SecurityInfoStore,
    window: list[date],
    as_of: date,
    rules: UniverseRules,
    data_root: Path,
    run_id: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Relabel a renamed stock's bars (and splits) under its old symbol so
    its 60-day history is continuous.

    Only checked for stocks that are missing days in the window but would
    pass the dollar-volume check on the days they do have - a rename can
    only change the outcome for those, and it keeps lookups few.
    """
    present = bars[bars["ticker"].isin(listed["ticker"])]
    days_with_bars = present.groupby("ticker")["date"].nunique()
    upper_bound = (present["close"] * present["volume"]).groupby(present["ticker"]).median()
    suspects = [
        t
        for t in days_with_bars.index
        if days_with_bars[t] < len(window) and upper_bound[t] >= rules.min_median_dollar_volume
    ]
    if not suspects:
        return bars, splits
    infos = store.get_many(listed[listed["ticker"].isin(suspects)], as_of)
    bars, splits = bars.copy(), splits.copy()
    for ticker, info in infos.items():
        if info.identity.status != STATUS_OK:
            continue
        for segment in segments(info.identity, window[0], window[-1]):
            if segment.ticker == ticker:
                continue
            rows = (
                (bars["ticker"] == segment.ticker)
                & (bars["date"] >= segment.start)
                & (bars["date"] <= segment.end)
            )
            clash = bars.loc[bars["ticker"] == ticker, "date"].isin(bars.loc[rows, "date"])
            if clash.any():
                log.warning(
                    "  NOT stitching %s: both %s and %s have bars on the same day(s)",
                    ticker, segment.ticker, ticker,
                )  # fmt: skip
                continue
            bars.loc[rows, "ticker"] = ticker
            record_stitch(data_root, run_id, info.identity, segment)
        # Splits recorded under an old symbol, while this company held it.
        for held in _symbol_periods(info):
            if held[0] == ticker:
                continue
            mask = (
                (splits["ticker"] == held[0])
                & (pd.to_datetime(splits["execution_date"]).dt.date >= held[1])
                & (pd.to_datetime(splits["execution_date"]).dt.date <= held[2])
            )
            splits.loc[mask, "ticker"] = ticker
    return bars, splits


def _symbol_periods(info: SecurityInfo) -> list[tuple[str, date, date]]:
    """(symbol, first day, last day) for each symbol the security used."""
    changes = info.identity.changes
    ends = [c.date - timedelta(days=1) for c in changes[1:]] + [date.max]
    return [(c.ticker, c.date, end) for c, end in zip(changes, ends, strict=True)]


# --- audit logs ---------------------------------------------------------------


def _append_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def audit_listing_dates(data_root: Path, rebalance_date: date, infos: dict) -> None:
    rows = [
        {
            "rebalance_date": rebalance_date,
            "ticker": t,
            "composite_figi": i.composite_figi,
            "name": i.name,
            "vendor_list_date": i.list_date,
            "first_ticker_event": i.first_event_date,
            "listing_date_used": min(d for d in (i.list_date, i.first_event_date) if d)
            if (i.list_date or i.first_event_date)
            else None,
        }
        for t, i in sorted(infos.items())
        if i.list_date != i.first_event_date
    ]
    _append_csv(data_root / "logs" / "listing_date_discrepancies.csv", rows)
    log.info("  listing date differs from first ticker event for %d stocks (logged)", len(rows))


def audit_share_counts(
    data_root: Path, rebalance_date: date, shares: pd.DataFrame, shares_date: date
) -> None:
    problems = shares[shares["status"] != SHARES_FOUND]
    rows = [
        {"rebalance_date": rebalance_date, "shares_date": shares_date, **r}
        for r in problems.to_dict("records")
    ]
    _append_csv(data_root / "logs" / "share_count_problems.csv", rows)
    log.info(
        "  share counts as of %s: %s",
        shares_date,
        dict(Counter(shares["status"])),
    )


def audit_market_cap(
    client: MassiveClient, data_root: Path, selected: pd.DataFrame, as_of: date
) -> None:
    def vendor_cap(ticker: str) -> float | None:
        try:
            response = client.get(f"/v3/reference/tickers/{ticker}", {"date": as_of.isoformat()})
        except MassiveError as exc:
            if exc.status_code != 404:
                raise
            return None
        return response.get("results", {}).get("market_cap")

    with ThreadPoolExecutor(LOOKUP_THREADS) as pool:
        vendor_caps = list(pool.map(vendor_cap, selected["ticker"]))
    rows = [
        {
            "rebalance_date": r.rebalance_date,
            "ticker": r.ticker,
            "our_market_cap": r.market_cap,
            "vendor_market_cap": cap,
            "ratio": r.market_cap / cap if cap else None,
        }
        for r, cap in zip(selected.itertuples(), vendor_caps, strict=True)
    ]
    _append_csv(data_root / "logs" / "market_cap_audit.csv", rows)
    far = [r for r in rows if r["ratio"] is None or abs(r["ratio"] - 1) > MARKET_CAP_AUDIT_WARN]
    for r in far:
        log.warning(
            "  market cap check: %s ours %.2fbn vs vendor %s - review",
            r["ticker"],
            r["our_market_cap"] / 1e9,
            f"{r['vendor_market_cap'] / 1e9:.2f}bn" if r["vendor_market_cap"] else "none",
        )
    log.info(
        "  selected %d; market cap within %d%% of vendor's for %d (see market_cap_audit.csv)",
        len(rows),
        round(MARKET_CAP_AUDIT_WARN * 100),
        len(rows) - len(far),
    )


# --- the whole run --------------------------------------------------------------


def run(
    client: MassiveClient, data_root: Path, universe_dir: Path, months: list[date]
) -> list[Path]:
    run_id = new_run_id()
    started = time.monotonic()
    first_shares = min(share_count_date(d) for d in months)
    # Through the last rebalance date itself: a split taking effect that
    # morning is known before the open and is applied (LEDGER-1).
    splits = fetch_market_splits(client, data_root, first_shares, max(months), run_id)
    store = SecurityInfoStore(client, data_root)
    built = []
    for n, rebalance_date in enumerate(months, 1):
        log.info("[%d/%d]", n, len(months))
        path = build_month(client, data_root, universe_dir, rebalance_date, splits, store, run_id)
        if path:
            built.append(path)
    elapsed = time.monotonic() - started
    log.info("=" * 70)
    log.info("UNIVERSE BUILD COMPLETE: %d snapshot(s) written to %s", len(built), universe_dir)
    log.info(
        "  %d API requests in %.0fs (%.1f per second)",
        client.request_count,
        elapsed,
        client.request_count / elapsed if elapsed else 0,
    )
    log.info("=" * 70)
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.build_universe",
        description="Build monthly universe snapshots (Concept v2 Section 2) from Massive data.",
    )
    parser.add_argument("--first-month", required=True, help="YYYY-MM")
    parser.add_argument("--last-month", required=True, help="YYYY-MM")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--universe-dir", type=Path, help="Default: <data root>/universe")
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    parser.add_argument("--requests-per-second", type=float, default=10.0)
    args = parser.parse_args(argv)

    months = rebalance_dates(args.first_month, args.last_month)
    if not months:
        parser.error("no rebalance dates in that range")
    if months[-1] > date.today():
        parser.error(f"{months[-1]} is in the future")

    run_log = setup_logging(args.data_root, "build-universe")
    try:
        client = make_client(args.secrets_file, args.requests_per_second)
        run(client, args.data_root, args.universe_dir or args.data_root / "universe", months)
    except Exception as exc:
        log.error("UNIVERSE BUILD FAILED: %s: %s", type(exc).__name__, exc)
        log.error("Months already built are kept. Re-run to continue.")
        return 1
    log.info("Log saved to %s", run_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
