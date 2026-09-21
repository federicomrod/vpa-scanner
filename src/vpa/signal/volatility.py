"""Volatility and spread - Concept v2 Section 5.2.

Three measurements, all answering "how far did this bar travel, compared
with how far this stock usually travels?":

- `atr20`: Wilder's Average True Range over 20 bars, **computed through
  the previous bar only**. A bar's own range never contributes to the
  yardstick it is measured against.
- `spread_atr`: this bar's true range divided by that yardstick. Around
  1.0 is an ordinary bar; 0.3 is a very quiet one; 3.0 is a wild one.
- `spread_pct_slot_60` (hourly): the percentile of this bar's true range
  among the same `slot_index` over the trailing 60 sessions.

**True range** is the greater of: the bar's own high-low range, and the
distance from either end of the bar to the previous close. The last two
are what make a gap show up as a large range: a stock that opens far
below yesterday's close has travelled that distance, even if it then
trades quietly.

Readings made here, recorded in LEDGER-2 for review:

- The bar sequence is **continuous across sessions**, so the first hour
  of a day measures its range from the previous day's closing hour. This
  is what makes an overnight gap visible in `spread_atr`, which is the
  behaviour Section 5.2 is for.
- **Half-day bars still get `atr20` and `spread_atr`** - those are price
  measurements, not slot baselines. Only `spread_pct_slot_60` is null on
  a half day (LEDGER-2, decision 2).
- A bar with **no trades has no prices**, so its true range is unknown.
  It gets null features and the ATR carries across it unchanged, rather
  than the gap poisoning every later value.
- A bar whose **true range is zero** (high, low, open and close all
  equal) gets a null `spread_atr` rather than a division by zero.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vpa.signal.windows import baseline_valid, measurable, min_valid, trailing_percentile

#: Wilder's smoothing length for ATR (Section 5.2).
ATR_PERIOD = 20

#: Trailing sessions for the hourly true-range percentile (Section 5.2).
SPREAD_WINDOW = 60

COLUMNS = ["true_range", "atr20", "spread_atr"]
HOURLY_COLUMNS = [*COLUMNS, "spread_pct_slot_60"]


def true_range(bars: pd.DataFrame) -> np.ndarray:
    """Wilder's true range, using the previous bar's close.

    With no previous close to compare against - the first bar of a
    history, or a bar following one that had no trades - the true range
    is simply the bar's own high minus its low.
    """
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    previous_close = np.concatenate([[np.nan], bars["close"].to_numpy(dtype=float)[:-1]])
    own_range = high - low
    since_previous_close = np.maximum(np.abs(high - previous_close), np.abs(low - previous_close))
    return np.where(
        np.isnan(previous_close), own_range, np.maximum(own_range, since_previous_close)
    )


def wilder_atr_through_previous(ranges: np.ndarray, period: int = ATR_PERIOD) -> np.ndarray:
    """Wilder's ATR as it stood **at the end of the previous bar**.

    Seeded with the average of the first `period` true ranges, then
    smoothed: `atr = (atr x (period - 1) + true_range) / period`. Bars
    with an unknown true range leave the running value untouched.
    """
    ranges = np.asarray(ranges, dtype=float)
    atr_after_bar = np.full(len(ranges), np.nan)
    known = ~np.isnan(ranges)
    seen = 0
    running = np.nan
    for i, (value, is_known) in enumerate(zip(ranges, known, strict=True)):
        if is_known:
            seen += 1
            if seen < period:
                running = np.nan
            elif seen == period:
                running = float(np.nanmean(ranges[: i + 1]))
            else:
                running = (running * (period - 1) + value) / period
        atr_after_bar[i] = running
    # Through the previous bar only: shift by one.
    return np.concatenate([[np.nan], atr_after_bar[:-1]])


def volatility_features(bars: pd.DataFrame, hourly: bool) -> pd.DataFrame:
    """Section 5.2 for one security's bars, in time order.

    `hourly` adds `spread_pct_slot_60`, which needs `slot_index`.
    """
    needed = ["high", "low", "close", "low_quality"] + (["slot_index"] if hourly else [])
    missing = set(needed) - set(bars.columns)
    if missing:
        raise ValueError(f"bars are missing columns: {sorted(missing)}")

    ranges = true_range(bars)
    atr = wilder_atr_through_previous(ranges)
    with np.errstate(invalid="ignore", divide="ignore"):
        spread = np.where(atr > 0, ranges / atr, np.nan)
    features = pd.DataFrame(
        {"true_range": ranges, "atr20": atr, "spread_atr": spread}, index=bars.index
    )
    if not hourly:
        return features

    percentile = np.full(len(bars), np.nan)
    for _, slot_bars in bars.groupby("slot_index", sort=False):
        rows = bars.index.get_indexer(slot_bars.index)
        percentile[rows] = trailing_percentile(
            ranges[rows],
            baseline_valid(slot_bars) & ~np.isnan(ranges[rows]),
            SPREAD_WINDOW,
            min_valid(SPREAD_WINDOW),
        )
    features["spread_pct_slot_60"] = np.where(measurable(bars), percentile, np.nan)
    return features
