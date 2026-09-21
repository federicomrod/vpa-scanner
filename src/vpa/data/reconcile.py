"""Reconciliation report - Concept v2 Section 3.3.

Prints our hourly and daily bars for chosen ticker/date pairs, side by
side with the vendor's own daily bar, so they can be compared by eye
against TradingView before any feature code is trusted.

Read-only: it uses what is already stored and never contacts the vendor.
If something it needs is missing, it says exactly which command to run.

What each case shows:

- every 1-hour bar of the session, with its slot, window, length, and how
  many minutes actually traded;
- our 1-day bar (regular hours only) against the vendor's daily bar
  (which includes pre-market and after-hours) - the difference Section
  4.2 says to document;
- any split or dividend near that date, from the stored corporate-actions
  tables;
- where a split falls after the date, a second table adjusted to today's
  share basis, which is what a TradingView chart shows.

Run it with `uv run python scripts/reconcile.py --help`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from vpa.data.bars import DAILY, HOURLY, derived_path, read_bars, session_bounds_for
from vpa.data.raw_store import DEFAULT_DATA_ROOT, read_partition
from vpa.signal.bars import session_slots

log = logging.getLogger("vpa.reconcile")

#: Corporate actions this many days either side of the date are shown.
EVENT_WINDOW_DAYS = 30


@dataclass(frozen=True)
class Case:
    ticker: str
    day: date
    why: str


#: The cases chosen by the project owner (Section 3.3 asks for a normal
#: day, a split, an ex-dividend day, an earnings gap and a half day).
DEFAULT_CASES = [
    Case("G", date(2025, 12, 2), "Normal trading day - boring control day"),
    Case("NVDA", date(2024, 6, 10), "Stock split - first day trading after the 10-for-1 split"),
    Case("AAPL", date(2025, 8, 11), "Ex-dividend - clean regular $0.26 ex-dividend date"),
    Case("ANF", date(2025, 1, 13), "Earnings/news gap - holiday-sales update at 07:00 ET, ICR day"),
    Case("NVDA", date(2025, 11, 28), "Half day - day after Thanksgiving, closes 13:00 ET"),
]


def parse_cases(text: str) -> list[Case]:
    """`TICKER:YYYY-MM-DD[:why]`, separated by semicolons (so the `why`
    text can contain commas)."""
    cases = []
    for item in text.split(";"):
        parts = item.strip().split(":")
        if len(parts) < 2:
            raise ValueError(f"Expected TICKER:YYYY-MM-DD, got {item!r}")
        ticker, day, *why = parts
        cases.append(Case(ticker.strip().upper(), date.fromisoformat(day.strip()),
                          ":".join(why).strip()))  # fmt: skip
    return cases


def for_ticker(bars: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Rows for this ticker, whether it is the current name or the one it
    traded under at the time."""
    if bars.empty:
        return bars
    return bars[(bars["requested_ticker"] == ticker) | (bars["ticker"] == ticker)]


def vendor_daily(data_root: Path, ticker: str, day: date) -> pd.Series | None:
    """The vendor's own daily bar, if that day's whole-market file is stored."""
    folder = data_root / "raw" / "daily_grouped" / f"date={day.isoformat()}"
    files = sorted(folder.glob("part-*.parquet"))
    if not files:
        return None
    table = pd.read_parquet(files[0])
    match = table[table["ticker"] == ticker]
    return match.iloc[0] if len(match) else None


def corporate_actions(data_root: Path, ticker: str, day: date) -> dict[str, pd.DataFrame]:
    """Splits and dividends near `day`, from the stored tables."""
    found = {}
    for dataset, date_column in (("splits", "execution_date"), ("dividends", "ex_dividend_date")):
        empty = pd.DataFrame(columns=["ticker", "requested_ticker", date_column])
        folder = data_root / "raw" / dataset
        parts = [
            read_partition(data_root, dataset, p.name)
            for p in sorted(folder.glob("fetched=*"))
            if any(p.glob("*.manifest.json"))
        ]
        if not parts:
            found[dataset] = empty
            continue
        rows = pd.concat(parts, ignore_index=True)
        rows = rows[(rows.get("requested_ticker") == ticker) | (rows["ticker"] == ticker)]
        if rows.empty:
            found[dataset] = empty
            continue
        when = pd.to_datetime(rows[date_column]).dt.date
        near = (when >= day - timedelta(days=EVENT_WINDOW_DAYS)) & (
            when <= day + timedelta(days=EVENT_WINDOW_DAYS)
        )
        found[dataset] = rows[near].assign(**{date_column: when[near]})
    return found


def _price(value) -> str:
    return "-" if pd.isna(value) else f"{value:>10,.4f}"


def _volume(value) -> str:
    return "-" if pd.isna(value) else f"{value:>14,.0f}"


def hourly_table(bars: pd.DataFrame, day: date) -> list[str]:
    session_open, session_close = session_bounds_for(day)
    windows = {
        s.index: f"{s.start:%H:%M}-{s.end:%H:%M}" + (f" ({s.minutes}m)" if s.minutes != 60 else "")
        for s in session_slots(session_open, session_close)
    }
    lines = [
        f"  {'slot':<4} {'window ET':<16} {'open':>10} {'high':>10} {'low':>10} {'close':>10}"
        f" {'volume':>14} {'mins':>5}  flags"
    ]
    for row in bars.sort_values("slot_index").itertuples():
        flags = "LOW QUALITY" if row.low_quality else ""
        lines.append(
            f"  {row.slot_index:<4} {windows.get(row.slot_index, '?'):<16} {_price(row.open)}"
            f" {_price(row.high)} {_price(row.low)} {_price(row.close)} {_volume(row.volume)}"
            f" {row.minutes_with_trades:>5}  {flags}"
        )
    return lines


def daily_table(ours: pd.Series, vendor: pd.Series | None) -> list[str]:
    lines = [
        f"  {'':<8} {'open':>10} {'high':>10} {'low':>10} {'close':>10} {'volume':>14} {'mins':>5}",
        f"  {'ours':<8} {_price(ours['open'])} {_price(ours['high'])} {_price(ours['low'])}"
        f" {_price(ours['close'])} {_volume(ours['volume'])} {ours['minutes_with_trades']:>5}",
    ]
    if vendor is None:
        lines.append("  vendor   (that day's whole-market file is not stored)")
        return lines
    lines.append(
        f"  {'vendor':<8} {_price(vendor['open'])} {_price(vendor['high'])} {_price(vendor['low'])}"
        f" {_price(vendor['close'])} {_volume(vendor['volume'])}"
    )
    differences = " ".join(
        f"{(ours[c] / vendor[c] - 1) * 100:>+10.3f}%" if vendor[c] else f"{'-':>11}"
        for c in ("open", "high", "low", "close")
    )
    volume_difference = (
        f"{(ours['volume'] / vendor['volume'] - 1) * 100:>+14.2f}%" if vendor["volume"] else "-"
    )
    lines.append(f"  {'diff':<8} {differences} {volume_difference}")
    lines.append("  (the vendor's daily bar includes pre-market and after-hours - Section 4.2)")
    return lines


def report_case(data_root: Path, case: Case, today: date | None = None) -> str:
    """The whole report for one ticker/date pair."""
    today = today or date.today()
    out = [
        "=" * 100,
        f"{case.ticker}  {case.day}  |  {case.why}",
    ]
    if not derived_path(data_root, HOURLY, case.day).exists():
        out.append(f"  NO BARS BUILT for {case.day}. Run:")
        out.append(f"    uv run python -m vpa.data.bars --start {case.day} --end {case.day}")
        return "\n".join(out)

    hourly = for_ticker(read_bars(data_root, HOURLY, [case.day]), case.ticker)
    daily = for_ticker(read_bars(data_root, DAILY, [case.day]), case.ticker)
    if hourly.empty or daily.empty:
        out.append(f"  NO MINUTE DATA STORED for {case.ticker} on {case.day}. Run:")
        out.append(
            f"    uv run python -m vpa.data.ingest --tickers {case.ticker}"
            f" --start {case.day.replace(day=1)} --end {case.day}"
        )
        out.append(f"    uv run python -m vpa.data.bars --start {case.day} --end {case.day}")
        return "\n".join(out)

    session_open, session_close = session_bounds_for(case.day)
    kind = "half day" if daily.iloc[0]["is_half_day"] else "normal day"
    out.append(f"Session {session_open:%H:%M}-{session_close:%H:%M} ET ({kind})")

    events = corporate_actions(data_root, case.ticker, case.day)
    for row in events["splits"].itertuples():
        out.append(
            f"SPLIT {row.execution_date}: {row.split_from:g}-for-{row.split_to:g}"
            " (prices before this date are on the old share basis)"
        )
    for row in events["dividends"].itertuples():
        out.append(
            f"DIVIDEND ex-date {row.ex_dividend_date}: {row.cash_amount} per share"
            " (never applied to prices - Section 3.1)"
        )
    if events["splits"].empty and events["dividends"].empty:
        out.append(f"No split or dividend within {EVENT_WINDOW_DAYS} days.")

    out += ["", "1-HOUR BARS, as traded (no adjustment)"]
    out += hourly_table(hourly, case.day)
    out += ["", "1-DAY BAR (regular hours only)"]
    out += daily_table(daily.iloc[0], vendor_daily(data_root, daily.iloc[0]["ticker"], case.day))

    splits = events["splits"]
    later_splits = (
        splits[pd.to_datetime(splits["execution_date"]).dt.date > case.day]
        if not splits.empty
        else splits
    )
    if not later_splits.empty:
        adjusted = for_ticker(read_bars(data_root, HOURLY, [case.day], as_of=today), case.ticker)
        out += [
            "",
            "1-HOUR BARS, adjusted to today's share basis (what a TradingView chart shows)",
        ]
        out += hourly_table(adjusted, case.day)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="uv run python scripts/reconcile.py",
        description="Print our hourly and daily bars for chosen ticker/date pairs, to compare "
        "against TradingView by eye (Concept v2 Section 3.3). Reads stored data only.",
    )
    parser.add_argument(
        "--cases",
        help="TICKER:YYYY-MM-DD[:why], separated by semicolons. " "Default: the five agreed cases.",
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--out", type=Path, help="Also write the report to this file")
    args = parser.parse_args(argv)

    cases = parse_cases(args.cases) if args.cases else DEFAULT_CASES
    report = "\n\n".join(report_case(args.data_root, case) for case in cases)
    report += "\n" + "=" * 100
    print(report)
    if args.out:
        args.out.write_text(report)
        print(f"\nSaved to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
