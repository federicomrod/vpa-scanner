"""Are the announcements our earnings flag misses signal, or noise?

LEDGER-4 amendment 2 found 88,431 8-K filings under items 7.01 and 8.01
with no results filing near them. Counting them as earnings would double
the flag's footprint, from 4.6% of company-days to 9.1%, so the question
is whether those days behave like earnings days or like ordinary ones.

The test is market reaction: the size of the day's move in daily ATR,
and its volume against the stock's own trailing median. Both baselines
are strictly trailing, so nothing here sees the day it is measuring.

Run it with `uv run python scripts/check_announcements.py`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from vpa.data.bars import DAILY, derived_path
from vpa.data.edgar import EARNINGS_ITEM, EIGHT_K_DATASET, VOLUNTARY_ITEMS, read_dataset
from vpa.data.events import stored_sessions
from vpa.data.raw_store import DEFAULT_DATA_ROOT
from vpa.signal.events import reacting_sessions

#: A move this far, in daily ATR, is the size earnings days routinely
#: produce. Used only to describe the tail, never to define a flag.
EVENT_SIZED_MOVE = 2.0

BASELINE_SESSIONS = 20


def daily_reaction(data_root: Path, sessions: list[date]) -> pd.DataFrame:
    """Each security-session's move and volume, against its own trailing
    baseline."""
    frames = [
        pd.read_parquet(
            derived_path(data_root, DAILY, session),
            columns=["security_key", "date", "close", "high", "low", "volume", "low_quality"],
        )
        for session in sessions
        if derived_path(data_root, DAILY, session).exists()
    ]
    daily = pd.concat(frames, ignore_index=True).sort_values(["security_key", "date"])
    daily = daily[~daily["low_quality"].eq(True)].copy()
    by_security = daily.groupby("security_key", sort=False)
    previous_close = by_security["close"].shift()
    true_range = pd.concat(
        [
            daily["high"] - daily["low"],
            (daily["high"] - previous_close).abs(),
            (daily["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    daily["atr20"] = true_range.groupby(daily["security_key"]).transform(
        lambda s: s.shift().rolling(BASELINE_SESSIONS, min_periods=15).mean()
    )
    daily["move_atr"] = by_security["close"].diff().abs() / daily["atr20"]
    daily["vol_ratio"] = daily["volume"] / by_security["volume"].transform(
        lambda s: s.shift().rolling(BASELINE_SESSIONS, min_periods=15).median()
    )
    daily["key"] = list(zip(daily["security_key"], daily["date"], strict=True))
    return daily.dropna(subset=["move_atr", "vol_ratio"])


def classify(data_root: Path, sessions: list[date]) -> tuple[set, set]:
    """(security, session) keys for results filings and for the voluntary
    announcements that no results filing covers."""
    filings = read_dataset(data_root, EIGHT_K_DATASET)
    items = filings["items"].fillna("").astype(str).apply(lambda i: {x for x in i.split(",") if x})
    filings["day"] = pd.to_datetime(filings["filing_date"]).dt.date
    filings["session"] = reacting_sessions(filings["accepted_utc"], sessions)

    results = items.apply(lambda s: EARNINGS_ITEM in s)
    voluntary = items.apply(lambda s: bool(s & set(VOLUNTARY_ITEMS))) & ~results
    covered = set(
        zip(filings.loc[results, "security_key"], filings.loc[results, "day"], strict=True)
    )
    uncovered = filings.loc[voluntary].apply(
        lambda row: not any(
            (row["security_key"], row["day"] + timedelta(days=offset)) in covered
            for offset in (-1, 0, 1)
        ),
        axis=1,
    )
    return (
        set(
            zip(filings.loc[results, "security_key"], filings.loc[results, "session"], strict=True)
        ),
        set(
            zip(
                filings.loc[voluntary][uncovered]["security_key"],
                filings.loc[voluntary][uncovered]["session"],
                strict=True,
            )
        ),
    )


def report(data_root: Path) -> None:
    sessions = stored_sessions(data_root)
    daily = daily_reaction(data_root, sessions)
    results, voluntary = classify(data_root, sessions)

    groups = {
        "item 2.02 - known earnings": daily[daily["key"].isin(results)],
        "7.01/8.01, no results near": daily[daily["key"].isin(voluntary)],
        "every other day": daily[~daily["key"].isin(results | voluntary)],
    }
    print(f"\nHow these days actually behaved, over {len(daily):,} security-sessions")
    print("=" * 78)
    print(f"{'group':<30} {'days':>9} {'move in ATR':>18} {'volume vs median':>19}")
    print(f"{'':<30} {'':>9} {'median':>8} {'p90':>9} {'median':>9} {'p90':>9}")
    print("-" * 78)
    for name, rows in groups.items():
        print(f"{name:<30} {len(rows):>9,} {rows['move_atr'].median():>8.2f} "
              f"{rows['move_atr'].quantile(0.9):>9.2f} {rows['vol_ratio'].median():>9.2f} "
              f"{rows['vol_ratio'].quantile(0.9):>9.2f}")  # fmt: skip

    print(
        f"\nShare of each group that moved {EVENT_SIZED_MOVE}+ ATR - the size earnings days make:"
    )
    for name, rows in groups.items():
        big = rows["move_atr"] >= EVENT_SIZED_MOVE
        print(f"  {name:<30} {100 * big.mean():>5.1f}%   ({int(big.sum()):,} days)")

    print("\nReading: a voluntary-item day sits far closer to an ordinary day")
    print("than to an earnings day. The tail is real but small - the ANF case")
    print("of 13 January 2025 lives in it (LEDGER-4, amendment 3).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args(argv)
    report(args.data_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
