"""Swing pivots and volume-at-price - Concept v2 Section 5.6 (part 2).

Two landmarks that need a rule rather than a lookup:

**Swing pivots.** The turning points a chart reader would mark. A high is
only a pivot once price has come back down from it by 1.5 x ATR - one
parameter, scale-invariant, and no fitted trendlines (Section 5.6). The
confirmation requirement is what makes it honest: at the moment a high is
made, nobody knows it is a top. A pivot therefore only exists from the
session where the counter-move completes, never from the session that
made the extreme.

**Volume at price.** Where the shares actually changed hands over the
last 60 sessions. A price level where a great deal traded tends to matter
later - Section 5.6 calls it a high-volume node.

Readings recorded in LEDGER-2 (the histogram's shape is the owner's
decision 6):

- Pivots are found on **daily** bars: these are chart-level landmarks,
  and Section 7 measures them in daily ATR.
- The 1.5 x ATR counter-move is measured against the **daily ATR in
  force when the counter-move happens**, so the filter scales with the
  stock's own volatility as Section 5.6 intends.
- `dist_nearest_swing_pivot` is the distance to the nearer of the last
  confirmed pivot high and pivot low. The pivot high is kept separately
  as well, because Section 7's Family B names it specifically.
- The histogram spans the trailing 60 sessions' lowest low to highest
  high in 50 equal bins, fed by hourly bars, each bar's whole volume
  going to the bin holding its close (decision 6). The node is the
  heaviest bin and the distance is measured to its centre.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: Counter-move required before an extreme becomes a pivot (Section 5.6).
PIVOT_COUNTER_MOVE_ATR = 1.5

#: Sessions and bins in the volume-at-price histogram (LEDGER-2, 6).
HISTOGRAM_SESSIONS = 60
HISTOGRAM_BINS = 50

COLUMNS = ["dist_nearest_swing_pivot", "dist_nearest_hvn"]


@dataclass
class _Swing:
    """The extreme being watched, and which way price is running."""

    rising: bool
    price: float
    at: int


def confirmed_pivots(daily: pd.DataFrame, atr: np.ndarray) -> pd.DataFrame:
    """The last confirmed pivot high and low as known at each session's
    close.

    Returns a frame aligned to `daily` with `pivot_high`, `pivot_high_at`,
    `pivot_low` and `pivot_low_at` (prices, and the position of the
    session that made each extreme).
    """
    high = daily["high"].to_numpy(dtype=float)
    low = daily["low"].to_numpy(dtype=float)
    thresholds = PIVOT_COUNTER_MOVE_ATR * np.asarray(atr, dtype=float)

    last = {"pivot_high": np.nan, "pivot_high_at": np.nan,
            "pivot_low": np.nan, "pivot_low_at": np.nan}  # fmt: skip
    history = {name: np.full(len(daily), np.nan) for name in last}

    swing = _Swing(rising=True, price=high[0] if len(high) else np.nan, at=0)
    for i in range(len(daily)):
        threshold = thresholds[i]
        if swing.rising:
            if high[i] >= swing.price or np.isnan(swing.price):
                swing = _Swing(True, high[i], i)
            elif threshold > 0 and swing.price - low[i] >= threshold:
                last["pivot_high"], last["pivot_high_at"] = swing.price, swing.at
                swing = _Swing(False, low[i], i)
        else:
            if low[i] <= swing.price or np.isnan(swing.price):
                swing = _Swing(False, low[i], i)
            elif threshold > 0 and high[i] - swing.price >= threshold:
                last["pivot_low"], last["pivot_low_at"] = swing.price, swing.at
                swing = _Swing(True, high[i], i)
        for name, value in last.items():
            history[name][i] = value
    return pd.DataFrame(history, index=daily.index)


def pivots_by_session(daily: pd.DataFrame, atr: np.ndarray) -> pd.DataFrame:
    """Confirmed pivots as they stood at the **previous** session's close,
    which is what a feature for this session may use."""
    known = confirmed_pivots(daily, atr)
    trailing = known.shift(1)
    trailing["date"] = daily["date"].to_numpy()
    return trailing


def volume_nodes(hourly: pd.DataFrame, sessions: list) -> pd.Series:
    """The heaviest-traded price over the trailing 60 sessions, per session.

    Indexed by session date. Null until 60 sessions of history exist.
    """
    if hourly.empty:
        return pd.Series(dtype=float)
    by_session = {
        day: (group["close"].to_numpy(dtype=float), group["volume"].to_numpy(dtype=float),
              float(group["low"].min()), float(group["high"].max()))
        for day, group in hourly.groupby("date", sort=True)
    }  # fmt: skip
    nodes = {}
    for position, day in enumerate(sessions):
        window = [by_session[d] for d in sessions[max(0, position - HISTOGRAM_SESSIONS) : position]
                  if d in by_session]  # fmt: skip
        nodes[day] = _heaviest_price(window) if len(window) >= HISTOGRAM_SESSIONS else np.nan
    return pd.Series(nodes, name="hvn")


def _heaviest_price(window: list[tuple]) -> float:
    """The centre of the busiest of 50 equal price bins."""
    lowest = min(low for _, _, low, _ in window)
    highest = max(high for _, _, _, high in window)
    if not np.isfinite(lowest) or not np.isfinite(highest) or highest <= lowest:
        return np.nan
    edges = np.linspace(lowest, highest, HISTOGRAM_BINS + 1)
    traded = np.zeros(HISTOGRAM_BINS)
    for closes, volumes, _, _ in window:
        usable = np.isfinite(closes) & np.isfinite(volumes)
        if not usable.any():
            continue
        bins = np.clip(np.digitize(closes[usable], edges) - 1, 0, HISTOGRAM_BINS - 1)
        np.add.at(traded, bins, volumes[usable])
    if traded.sum() <= 0:
        return np.nan
    busiest = int(np.argmax(traded))
    return float((edges[busiest] + edges[busiest + 1]) / 2)


def pivot_and_node_features(
    bars: pd.DataFrame,
    pivots: pd.DataFrame,
    nodes: pd.Series,
    atr_by_session: pd.Series,
) -> pd.DataFrame:
    """Section 5.6's pivot and volume-node distances, in daily ATR units."""
    missing = {"date", "close"} - set(bars.columns)
    if missing:
        raise ValueError(f"bars are missing columns: {sorted(missing)}")

    close = bars["close"].to_numpy(dtype=float)
    atr = bars["date"].map(atr_by_session).to_numpy(dtype=float)
    usable_atr = np.where(atr > 0, atr, np.nan)
    by_date = pivots.set_index("date")

    high_pivot = bars["date"].map(by_date["pivot_high"]).to_numpy(dtype=float)
    low_pivot = bars["date"].map(by_date["pivot_low"]).to_numpy(dtype=float)
    node = bars["date"].map(nodes).to_numpy(dtype=float)

    to_high = (close - high_pivot) / usable_atr
    to_low = (close - low_pivot) / usable_atr
    nearer = np.where(_closer(to_high, to_low), to_high, to_low)
    return pd.DataFrame(
        {"dist_nearest_swing_pivot": nearer, "dist_nearest_hvn": (close - node) / usable_atr},
        index=bars.index,
    )


def _closer(first: np.ndarray, second: np.ndarray) -> np.ndarray:
    """True where `first` is the nearer of the two (a missing one loses)."""
    return np.where(np.isnan(second), True, np.abs(first) <= np.abs(second)) & ~np.isnan(first)
