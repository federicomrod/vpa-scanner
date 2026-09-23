"""Section 7 against the real store.

Three things the unit tests cannot do, because they run on invented
numbers:

- **`--day`** walks one session's bars for one ticker, showing every
  Section 7 threshold and whether it passed. The default case is ANF on
  13 January 2025 - the day it gapped 15% - where exactly one bar of
  seven is a candidate, and the reasons the other six are not are worth
  reading.
- **`--yield`** measures the candidate rate across the store, against
  Section 7.3's "roughly 30-60 per day" and LEDGER-3's measurement.
- **`--location-reading`** measures the one place Section 7's wording is
  genuinely ambiguous: whether "within 2.0 x daily ATR of a resistance
  reference" counts a stock that is *above* the level (LEDGER-5).

Run it with `uv run python scripts/check_candidates.py --help`.
"""

from __future__ import annotations

import argparse
import glob
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from vpa.data.events import events_path, stored_sessions
from vpa.data.raw_store import DEFAULT_DATA_ROOT
from vpa.signal.candidates import (
    A_LOCATION_ATR,
    A_LOCATIONS,
    A_MAX_RESID_RET_ATR,
    A_MAX_RET_ATR,
    A_MAX_SPREAD_ATR,
    B_LOCATION_ATR,
    B_LOCATIONS,
    B_MAX_CLOSE_LOC,
    B_MIN_UPPER_WICK_FRAC,
    DAILY_CAP,
    MIN_VOL_PCT,
    apply_cap,
    candidates,
    summarise,
    unflagged,
)

ANF_GAP_DAY = date(2025, 1, 13)


def universe_in_force(data_root: Path, session: date) -> set[str]:
    """The 400 securities selected at the last rebalance on or before
    `session`.

    Section 7.3's budget is "across 400 tickers", and Section 2 makes the
    universe the thing that gets scanned. The store holds bars for every
    security that has *ever* been a member - about 970 on a given day -
    so scanning it unfiltered measures something else entirely.
    """
    snapshots = sorted((data_root / "universe").glob("????-??-??.parquet"))
    in_force = [p for p in snapshots if date.fromisoformat(p.stem) <= session]
    if not in_force:
        raise FileNotFoundError(f"No universe snapshot on or before {session}")
    members = pd.read_parquet(in_force[-1], columns=["composite_figi", "ticker"])
    return {
        figi if isinstance(figi, str) and figi else f"TICKER:{ticker}"
        for ticker, figi in zip(members["ticker"], members["composite_figi"], strict=True)
    }


def read_features(data_root: Path, session: date, members: set[str] | None = None) -> pd.DataFrame:
    folder = data_root / "derived/features/hourly" / f"date={session}"
    parts = glob.glob(str(folder / "shard-*.parquet"))
    if not parts:
        raise FileNotFoundError(f"No features stored for {session}")
    features = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    if members is None:
        return features
    return features[features["security_key"].isin(members)].reset_index(drop=True)


# --- one ticker, one day ------------------------------------------------------


def show_day(data_root: Path, session: date, ticker: str) -> None:
    features = read_features(data_root, session)
    bars = features[features["requested_ticker"] == ticker].sort_values("slot_index")
    if bars.empty:
        raise SystemExit(f"{ticker} has no bars on {session}")
    marked = candidates(bars.reset_index(drop=True))

    print(f"\n{ticker}, {session}: every Section 7 threshold, slot by slot")
    print("=" * 96)
    print(
        f"{'slot':>4} {'vol pct':>8} {'spread':>8} {'|ret|':>8} {'|resid|':>8} "
        f"{'wick':>6} {'close':>6} {'failed':>7} {'A near':>7} {'B near':>7} | {'A':>5} {'B':>5}"
    )
    print(
        f"{'':>4} {'>= 90':>8} {'<= .60':>8} {'<= .25':>8} {'<= .25':>8} "
        f"{'>=.50':>6} {'<=.35':>6} {'high':>7} {'<= 1.5':>7} {'<= 2.0':>7} |"
    )
    print("-" * 96)
    for position, row in marked.iterrows():
        near_a = bars.iloc[position][A_LOCATIONS].abs().min()
        near_b = bars.iloc[position][B_LOCATIONS].abs().min()
        print(f"{int(row.slot_index):>4} {row.vol_pct_slot_60:>8.1f} {row.spread_atr:>8.3f} "
              f"{abs(row.ret_atr):>8.3f} {abs(row.resid_ret_atr):>8.3f} "
              f"{row.upper_wick_frac:>6.2f} {row.close_loc:>6.2f} "
              f"{str(bool(row.failed_new_high)):>7} {near_a:>7.3f} {near_b:>7.3f} | "
              f"{_mark(row.family_a):>5} {_mark(row.family_b):>5}")  # fmt: skip

    firing = marked[marked["is_candidate"]]
    print(f"\ncandidates: {len(firing)} of {len(marked)} bars", end="")
    print(f" -> slot {list(firing['slot_index'])}" if len(firing) else "")
    for position, row in firing.iterrows():
        _explain(bars.iloc[position], row)
    _show_events(data_root, session, ticker)


def _mark(value) -> str:
    return "YES" if bool(value) else "-"


def _explain(bar: pd.Series, row: pd.Series) -> None:
    family = "7.1 (A)" if row.family_a else "7.2 (B)"
    print(f"\n  Slot {int(row.slot_index)} against Section {family}:")
    tests = (
        [
            ("1. vol_pct_slot_60 >= 90", row.vol_pct_slot_60, MIN_VOL_PCT, "min"),
            ("2. spread_atr <= 0.60", row.spread_atr, A_MAX_SPREAD_ATR, "max"),
            ("3. |ret_atr| <= 0.25", abs(row.ret_atr), A_MAX_RET_ATR, "max"),
            ("4. |resid_ret_atr| <= 0.25", abs(row.resid_ret_atr), A_MAX_RESID_RET_ATR, "max"),
        ]
        if row.family_a
        else [
            ("1. vol_pct_slot_60 >= 90", row.vol_pct_slot_60, MIN_VOL_PCT, "min"),
            ("2. upper_wick_frac >= 0.50", row.upper_wick_frac, B_MIN_UPPER_WICK_FRAC, "min"),
            ("3. close_loc <= 0.35", row.close_loc, B_MAX_CLOSE_LOC, "max"),
        ]
    )
    for label, value, threshold, sense in tests:
        passed = value >= threshold if sense == "min" else value <= threshold
        print(f"    {label:<32} {value:>10.4f}   {'PASS' if passed else 'FAIL'}")
    if row.family_b:
        print(f"    {'4. failed_new_high is true':<32} {str(bool(row.failed_new_high)):>10}   PASS")
    print(f"    {'5. low_quality is false':<32} {str(bool(row.low_quality)):>10}   "
          f"{'PASS' if not row.low_quality else 'FAIL'}")  # fmt: skip

    references = A_LOCATIONS if row.family_a else B_LOCATIONS
    limit = A_LOCATION_ATR if row.family_a else B_LOCATION_ATR
    print(f"    7. location <= {limit} daily ATR")
    for column in references:
        print(f"         {column:<28} {bar[column]:>10.4f}")
    print(f"         {'-> nearest':<28} {bar[references].abs().min():>10.4f}   PASS")


def _show_events(data_root: Path, session: date, ticker: str) -> None:
    path = events_path(data_root, session)
    if not path.exists():
        return
    rows = pd.read_parquet(path)
    row = rows[rows["requested_ticker"] == ticker]
    if row.empty:
        return
    flags = row.drop(columns=["security_key", "requested_ticker", "date"]).iloc[0]
    firing = [name for name, value in flags.items() if value is True and name != "any_event"]
    unknown = [name for name, value in flags.items() if pd.isna(value)]
    print(f"\n  Section 7 item 6, the event condition for {session}:")
    print(f"    flags true:    {', '.join(firing) if firing else 'none'}")
    print(f"    flags unknown: {', '.join(unknown) if unknown else 'none'}")
    print(f"    any_event:     {flags['any_event']}")
    print(f"    -> usable in validation: strict {bool(unflagged(row, True).iloc[0])}, "
          f"lenient {bool(unflagged(row, False).iloc[0])}")  # fmt: skip


# --- yield across the store ---------------------------------------------------


def measure_yield(data_root: Path, every: int) -> None:
    sessions = stored_sessions(data_root)[::every]
    rows, capped = [], []
    for session in sessions:
        try:
            features = read_features(data_root, session, universe_in_force(data_root, session))
        except FileNotFoundError:
            continue
        marked = candidates(features)
        counts = summarise(marked)
        kept, dropped = apply_cap(marked)
        counts.update(date=session, dropped=dropped)
        rows.append(counts)
        if dropped:
            capped.append((session, counts["candidates"], dropped))

    found = pd.DataFrame(rows)
    print(f"\nSection 7 candidate yield over {len(found)} sampled sessions")
    print("=" * 70)
    print(f"  universe members scanned:   {found['bars'].mean() / 7:>8.0f}")
    print(f"  bars examined per session:  {found['bars'].mean():>8.0f}")
    for name in ("family_a", "family_b", "both", "candidates", "securities"):
        values = found[name]
        print(f"  {name:<26} mean {values.mean():>6.1f}   median {values.median():>5.0f}   "
              f"p90 {values.quantile(0.9):>5.0f}   max {values.max():>5.0f}")  # fmt: skip
    print()
    print(f"  Section 7.3 expects roughly 30-60 a day, hard cap {DAILY_CAP}.")
    print(f"  sessions over the cap: {len(capped)} of {len(found)} "
          f"({100 * len(capped) / max(len(found), 1):.0f}%)")  # fmt: skip
    print(f"  candidates dropped in total: {found['dropped'].sum():,}")
    print(f"  sessions with no candidate at all: {int((found['candidates'] == 0).sum())}")
    if capped:
        print("\n  busiest sessions:")
        for session, total, dropped in sorted(capped, key=lambda c: -c[1])[:5]:
            print(f"    {session}  {total:>4} candidates, {dropped:>4} dropped")

    print("\n  A candidate is a bar (Section 8.1). LEDGER-3 counted distinct")
    print("  stocks, so its figures compare with the 'securities' row above.")


# --- the one genuinely ambiguous reading --------------------------------------


def location_reading(data_root: Path, every: int) -> None:
    """Absolute distance, or only below the level?

    Section 7.2 says "within 2.0 x daily ATR of a resistance reference".
    Read literally that is a distance, and a stock a little above its
    20-day high is near it. Read directionally, a stock above the level
    has broken through and is no longer being resisted by it.
    """
    sessions = stored_sessions(data_root)[::every]
    absolute = directional = both = 0
    for session in sessions:
        try:
            features = read_features(data_root, session, universe_in_force(data_root, session))
        except FileNotFoundError:
            continue
        marked = candidates(features)
        firing = marked[marked["family_b"]]
        if firing.empty:
            continue
        distances = firing[B_LOCATIONS]
        near_abs = distances.abs().min(axis=1) <= B_LOCATION_ATR
        # Directional: price at or below the level, within the limit.
        below = distances.where(distances <= 0)
        near_below = below.abs().min(axis=1) <= B_LOCATION_ATR
        absolute += int(near_abs.sum())
        directional += int((near_abs & near_below).sum())
        both += len(firing)

    print(f"\nSection 7.2's location wording, over {len(sessions)} sampled sessions")
    print("=" * 70)
    print(f"  Family B candidates on the literal reading (any side):  {absolute:,}")
    print(f"  ...also at or below the level (directional reading):    {directional:,}")
    lost = absolute - directional
    print(f"  difference:                                             {lost:,} "
          f"({100 * lost / max(absolute, 1):.1f}%)")  # fmt: skip
    print("\n  The literal reading is implemented. This is the number the")
    print("  directional reading would remove, recorded in LEDGER-5.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--day", type=date.fromisoformat, default=ANF_GAP_DAY)
    parser.add_argument("--ticker", default="ANF")
    parser.add_argument("--every", type=int, default=25,
                        help="Sample every Nth session for the measurements")  # fmt: skip
    parser.add_argument("--yield", dest="measure", action="store_true")
    parser.add_argument("--location-reading", action="store_true")
    args = parser.parse_args(argv)

    if args.measure:
        measure_yield(args.data_root, args.every)
    elif args.location_reading:
        location_reading(args.data_root, args.every)
    else:
        show_day(args.data_root, args.day, args.ticker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
