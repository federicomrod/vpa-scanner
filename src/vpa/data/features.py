"""Computing and storing the Section 5 features.

Features need each security's history **in order**, but bars are stored
by date. Twenty million hourly bars will not fit in memory alongside
everything else, so this works through the securities in batches: for
each batch it reads the bars it needs, computes every feature, and writes
one file per trading day. Memory stays flat however long the history is.

Output, under the data root, mirroring the bars:

    derived/features/hourly/date=YYYY-MM-DD/part-<batch>.parquet
    derived/features/daily/date=YYYY-MM-DD/part-<batch>.parquet

Like the bars, features are **regenerable**: wrong feature code is fixed
by correcting it and rebuilding, never by patching stored numbers.

### Splits, and why the whole history can be adjusted at once

Section 3.1 says prices are adjusted at read time, as of the date being
measured. Adjusting a ten-year history separately for every bar would be
ruinous, and it turns out to be unnecessary: adjusting as of today and
adjusting as of an earlier date differ only by **one constant factor
applied to every bar in the window**, and every Section 5 feature is
blind to that.

Percentiles compare ranks, so a constant scale cannot change them.
Ratios - `spread_atr`, `ret_atr`, every distance in ATR units - scale
their top and bottom together. A split *inside* a window is applied
identically either way.

**One feature is only approximately blind to it.** Section 5.1 measures
volume as `log(volume + 1)`, and that "+1" means doubling volume does
not simply add a constant: `log(2v+1)` is not `log(v+1) + log 2`. So
`vol_z_slot_60` shifts very slightly. Measured: about 1e-5 for a stock
trading 50,000 shares an hour, 1e-3 at 500 an hour, and 1e-2 at 50 -
and the universe's $15m-a-day floor keeps every member far above that.
No Section 7 filter uses the z-score (they use `vol_pct_slot_60`, which
is exact), so this cannot change which bars become candidates.

**One feature is not blind to it at all:** `dist_round_number` asks how close
price is to a whole or half dollar, which is a fact about the price as
traded. A stock at $10.40 before a 2-for-1 split is $5.20 in today's
money, and the nearest round number is a different one. That feature is
therefore computed from unadjusted prices, with an unadjusted ATR, while
everything else uses the adjusted series. Recorded in LEDGER-2.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

from vpa.data.bars import DAILY, HOURLY, derived_path, load_splits, read_bars
from vpa.data.ingest import setup_logging
from vpa.data.raw_store import DEFAULT_DATA_ROOT
from vpa.signal.adjust import split_adjust_daily
from vpa.signal.geometry import geometry_features
from vpa.signal.levels import (
    anchor_prices,
    anchor_set_positions,
    nearest_level_features,
    touch_counts,
)
from vpa.signal.market import beta_by_session, market_features
from vpa.signal.pivots import pivot_and_node_features, pivots_by_session, volume_nodes
from vpa.signal.progress import progress_features
from vpa.signal.sequence import sequence_features
from vpa.signal.structure import daily_atr, nearest_round_number, session_levels
from vpa.signal.structure import structure_features as level_distances
from vpa.signal.volatility import volatility_features
from vpa.signal.volume import daily_volume_features, hourly_volume_features

log = logging.getLogger("vpa.features")

#: The market every stock is measured against (Section 5.5).
MARKET_TICKER = "SPY"

#: Securities computed at a time. Keeps memory flat; the only cost of a
#: smaller batch is more, smaller output files.
BATCH_SIZE = 100

IDENTITY = ["security_key", "requested_ticker", "date"]


def features_path(data_root: Path, kind: str, session: date, batch: str) -> Path:
    return data_root / "derived" / "features" / kind / f"date={session}" / f"part-{batch}.parquet"


def hourly_features(
    bars: pd.DataFrame,
    raw_bars: pd.DataFrame,
    daily: pd.DataFrame,
    market_hourly: pd.DataFrame,
    market_daily: pd.DataFrame,
) -> pd.DataFrame:
    """Every Section 5 feature for one security's hourly bars.

    `bars` are split-adjusted; `raw_bars` are the same bars as traded,
    used only for `dist_round_number` (see the module docstring).
    """
    volatility = volatility_features(bars, hourly=True)
    geometry = geometry_features(bars, ranges=volatility["true_range"].to_numpy())
    volume = hourly_volume_features(bars)
    progress = progress_features(bars, hourly=True, atr=volatility["atr20"].to_numpy())

    atr = daily_atr(daily)
    levels = session_levels(daily)
    pivots = pivots_by_session(daily, atr.to_numpy())
    nodes = volume_nodes(bars, list(daily["date"]))

    distances = level_distances(bars, levels, atr)
    distances["dist_round_number"] = _round_number_distance(raw_bars, daily)
    pivot_distances = pivot_and_node_features(bars, pivots, nodes, atr)
    nearest = nearest_level_features(
        bars,
        anchor_prices(levels, pivots),
        anchor_set_positions(daily, levels, pivots),
        touch_counts(daily, anchor_prices(levels, pivots)),
        pd.Series(range(len(daily)), index=pd.Index(daily["date"], name="date")),
    )
    market = market_features(
        bars, market_hourly, beta_by_session(daily, market_daily), volatility["atr20"].to_numpy()
    )
    sequence = sequence_features(
        bars,
        volume["vol_pct_slot_60"].to_numpy(),
        volatility["spread_atr"].to_numpy(),
        geometry["upper_wick_frac"].to_numpy(),
    )
    return pd.concat(
        [
            bars[[*IDENTITY, "slot_index", "timestamp_utc", "is_half_day", "low_quality"]],
            volume, volatility, geometry, progress, market,
            distances, pivot_distances, nearest, sequence,
        ],
        axis=1,
    )  # fmt: skip


def daily_features(
    bars: pd.DataFrame, raw_bars: pd.DataFrame, market_daily: pd.DataFrame
) -> pd.DataFrame:
    """The Section 5 features that apply to daily bars.

    Section 5.7's sequence features and the slot-wise measures are hourly
    by definition and are not computed here.
    """
    volatility = volatility_features(bars, hourly=False)
    geometry = geometry_features(bars, ranges=volatility["true_range"].to_numpy())
    volume = daily_volume_features(bars)
    progress = progress_features(bars, hourly=False, atr=volatility["atr20"].to_numpy())
    atr = daily_atr(bars)
    levels = session_levels(bars)
    distances = level_distances(bars, levels, atr)
    distances["dist_round_number"] = _round_number_distance(raw_bars, raw_bars)
    market = market_features(
        bars, market_daily, beta_by_session(bars, market_daily), volatility["atr20"].to_numpy()
    )
    return pd.concat(
        [
            bars[[*IDENTITY, "timestamp_utc", "is_half_day", "low_quality"]],
            volume, volatility, geometry, progress, market, distances,
        ],
        axis=1,
    )  # fmt: skip


def _round_number_distance(raw_bars: pd.DataFrame, raw_daily: pd.DataFrame) -> np.ndarray:
    """Distance to the nearest whole or half dollar, from prices as
    traded - the one feature a split adjustment would change."""
    close = raw_bars["close"].to_numpy(dtype=float)
    atr = raw_bars["date"].map(daily_atr(raw_daily)).to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(atr > 0, (close - nearest_round_number(close)) / atr, np.nan)


def securities_in(data_root: Path, sessions: list[date]) -> pd.DataFrame:
    """Every security with bars, and the sessions it has them on."""
    rows = []
    for session in sessions:
        path = derived_path(data_root, DAILY, session)
        if path.exists():
            rows.append(pd.read_parquet(path, columns=["security_key", "requested_ticker"]))
    if not rows:
        return pd.DataFrame(columns=["security_key", "requested_ticker"])
    return pd.concat(rows, ignore_index=True).drop_duplicates("security_key")


def build(
    data_root: Path, sessions: list[date], batch_size: int = BATCH_SIZE, rebuild: bool = False
) -> int:
    """Compute and store features for every security over `sessions`."""
    started = time.monotonic()
    securities = securities_in(data_root, sessions)
    keys = sorted(securities["security_key"])
    log.info("Securities with bars: %d over %d sessions", len(keys), len(sessions))

    market = {kind: _read_market(data_root, kind, sessions) for kind in (HOURLY, DAILY)}
    if market[DAILY].empty:
        raise ValueError(f"No {MARKET_TICKER} bars stored - Section 5.5 needs them")

    written = 0
    for number, start in enumerate(range(0, len(keys), batch_size), 1):
        batch = keys[start : start + batch_size]
        label = f"{number:04d}"
        if not rebuild and features_path(data_root, HOURLY, sessions[-1], label).exists():
            log.info("Batch %s: already built, skipping", label)
            continue
        written += _build_batch(data_root, sessions, batch, label, market)
        log.info(
            "Batch %s of %d done (%d securities, %.0fs so far)",
            label,
            (len(keys) + batch_size - 1) // batch_size,
            len(batch),
            time.monotonic() - started,
        )
    log.info("Wrote %d feature files in %.0fs", written, time.monotonic() - started)
    return written


def _read_market(data_root: Path, kind: str, sessions: list[date]) -> pd.DataFrame:
    bars = read_bars(data_root, kind, sessions, as_of=date.today())
    if bars.empty:
        return bars
    market = bars[bars["requested_ticker"] == MARKET_TICKER]
    order = ["date", "slot_index"] if kind == HOURLY else ["date"]
    return market.sort_values(order).reset_index(drop=True)


def _build_batch(
    data_root: Path, sessions: list[date], batch: list[str], label: str, market: dict
) -> int:
    adjusted = {
        kind: _read_batch(data_root, kind, sessions, batch, adjust=True) for kind in (HOURLY, DAILY)
    }
    raw = {
        kind: _read_batch(data_root, kind, sessions, batch, adjust=False)
        for kind in (HOURLY, DAILY)
    }
    hourly_rows, daily_rows = [], []
    for key in batch:
        bars = _one(adjusted[HOURLY], key)
        daily = _one(adjusted[DAILY], key)
        if bars.empty or daily.empty:
            continue
        hourly_rows.append(
            hourly_features(bars, _one(raw[HOURLY], key), daily, market[HOURLY], market[DAILY])
        )
        daily_rows.append(daily_features(daily, _one(raw[DAILY], key), market[DAILY]))

    written = 0
    for kind, rows in ((HOURLY, hourly_rows), (DAILY, daily_rows)):
        if not rows:
            continue
        everything = pd.concat(rows, ignore_index=True)
        for session, day_features in everything.groupby("date", sort=True):
            path = features_path(data_root, kind, session, label)
            path.parent.mkdir(parents=True, exist_ok=True)
            day_features.reset_index(drop=True).to_parquet(path, index=False)
            written += 1
    return written


def _read_batch(
    data_root: Path, kind: str, sessions: list[date], batch: list[str], adjust: bool
) -> pd.DataFrame:
    """One batch of securities' bars, read straight from the day files.

    Only this batch's rows are read, and only they are split-adjusted -
    reading and adjusting the whole market once per batch is what made an
    early version of this unusably slow.
    """
    files = [
        str(derived_path(data_root, kind, session))
        for session in sessions
        if derived_path(data_root, kind, session).exists()
    ]
    if not files:
        return pd.DataFrame()
    wanted = ds.field("security_key").isin(batch)
    bars = ds.dataset(files, format="parquet").to_table(filter=wanted).to_pandas()
    if bars.empty:
        return bars
    if adjust:
        bars = split_adjust_daily(
            bars, load_splits(data_root), date.today(), ticker_column="requested_ticker"
        )
    order = ["security_key", "date", "slot_index"] if kind == HOURLY else ["security_key", "date"]
    return bars.sort_values(order).reset_index(drop=True)


def _one(bars: pd.DataFrame, key: str) -> pd.DataFrame:
    if bars.empty:
        return bars
    return bars[bars["security_key"] == key].reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.features",
        description="Compute and store the Concept v2 Section 5 features from the stored bars.",
    )
    parser.add_argument("--start", type=date.fromisoformat, help="First session (YYYY-MM-DD)")
    parser.add_argument("--end", type=date.fromisoformat, help="Last session (YYYY-MM-DD)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--rebuild", action="store_true", help="Recompute batches already stored")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    args = parser.parse_args(argv)

    setup_logging(args.data_root, "build-features")
    try:
        from vpa.data.bars import raw_sessions

        sessions = [
            s
            for s in raw_sessions(args.data_root)
            if (args.start is None or s >= args.start) and (args.end is None or s <= args.end)
        ]
        sessions = [s for s in sessions if derived_path(args.data_root, HOURLY, s).exists()]
        if not sessions:
            log.error("FEATURE BUILD FAILED: no bars built for those dates")
            return 1
        build(args.data_root, sessions, args.batch_size, args.rebuild)
    except Exception as exc:
        log.exception("FEATURE BUILD FAILED: %s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
