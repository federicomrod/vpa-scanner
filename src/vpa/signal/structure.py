"""Structure: distance to price levels - Concept v2 Section 5.6 (part 1).

Where is the price, relative to the landmarks a chart reader would
notice? Section 5.6 is emphatic that these are **continuous distances**,
never "at resistance" flags and never fitted trendlines - a stock 0.2 ATR
below its 20-day high and one 1.4 ATR below it are different situations,
and a yes/no answer throws that away.

This part covers the levels that need no interpretation:

- `dist_prior_day_high/low`: yesterday's extremes.
- `dist_prior_week_high/low`, `dist_prior_month_high/low`: the last
  **completed** calendar week's and month's extremes.
- `dist_20d_high/low`, `dist_60d_high/low`: the extremes of the trailing
  20 and 60 sessions.
- `dist_round_number`: distance to the nearest whole or half dollar.

Swing pivots, the volume-at-price histogram and the touch counts follow
in their own change.

Readings recorded in LEDGER-2:

- **Every distance is in daily ATR units**, for hourly bars as well as
  daily ones. Section 7's location tests say "daily ATR" explicitly, so
  an hourly bar's distances are measured against its session's daily ATR.
- **Distances are signed**: `(close - level) / daily ATR`, positive above
  the level and negative below. Section 7 compares the magnitude; the
  sign is kept because "just above the 20-day high" and "just below it"
  are not the same situation.
- **Levels are strictly trailing.** The 20-day high is the highest high
  of the 20 sessions **before** this one, and "prior week" means the last
  completed week, never the one in progress.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vpa.signal.volatility import volatility_features

#: Trailing sessions for the two extreme windows (Section 5.6).
EXTREME_WINDOWS = (20, 60)

#: Round numbers a chart reader notices (Section 5.6).
ROUND_NUMBER_STEP = 0.5

LEVEL_COLUMNS = [
    "prior_day_high",
    "prior_day_low",
    "prior_week_high",
    "prior_week_low",
    "prior_month_high",
    "prior_month_low",
    "high_20",
    "low_20",
    "high_60",
    "low_60",
]

COLUMNS = [f"dist_{name}" for name in LEVEL_COLUMNS] + ["dist_round_number"]


def session_levels(daily: pd.DataFrame) -> pd.DataFrame:
    """The levels in force on each session, from that session's history.

    `daily` is one security's daily bars in time order, with `date`,
    `high` and `low`. Every value looks strictly backwards.
    """
    missing = {"date", "high", "low"} - set(daily.columns)
    if missing:
        raise ValueError(f"daily bars are missing columns: {sorted(missing)}")

    dates = pd.to_datetime(daily["date"])
    high = daily["high"].astype(float)
    low = daily["low"].astype(float)

    levels = pd.DataFrame(index=daily.index)
    levels["prior_day_high"] = high.shift(1)
    levels["prior_day_low"] = low.shift(1)

    calendar = dates.dt.isocalendar()
    week = list(zip(calendar["year"], calendar["week"], strict=True))
    month = list(zip(dates.dt.year, dates.dt.month, strict=True))
    levels["prior_week_high"], levels["prior_week_low"] = _previous_period(high, low, week)
    levels["prior_month_high"], levels["prior_month_low"] = _previous_period(high, low, month)

    for window in EXTREME_WINDOWS:
        levels[f"high_{window}"] = high.shift(1).rolling(window, min_periods=window).max()
        levels[f"low_{window}"] = low.shift(1).rolling(window, min_periods=window).min()

    levels["date"] = daily["date"].to_numpy()
    return levels


def _previous_period(high: pd.Series, low: pd.Series, periods: list) -> tuple[pd.Series, pd.Series]:
    """Each session's last **completed** week or month extremes."""
    frame = pd.DataFrame({"period": periods, "high": high.to_numpy(), "low": low.to_numpy()})
    by_period = frame.groupby("period", sort=False).agg(high=("high", "max"), low=("low", "min"))
    previous = by_period.shift(1)
    mapped = frame["period"].map(previous["high"]), frame["period"].map(previous["low"])
    return (
        pd.Series(mapped[0].to_numpy(), index=high.index),
        pd.Series(mapped[1].to_numpy(), index=high.index),
    )


def daily_atr(daily: pd.DataFrame) -> pd.Series:
    """Each session's daily ATR(20), through the previous session."""
    atr = volatility_features(daily, hourly=False)["atr20"]
    return pd.Series(atr.to_numpy(), index=pd.Index(daily["date"], name="date"))


def nearest_round_number(prices: np.ndarray) -> np.ndarray:
    """The nearest whole or half dollar to each price."""
    return np.round(np.asarray(prices, dtype=float) / ROUND_NUMBER_STEP) * ROUND_NUMBER_STEP


def structure_features(
    bars: pd.DataFrame, levels: pd.DataFrame, atr_by_session: pd.Series
) -> pd.DataFrame:
    """Section 5.6 distances for one security's bars (hourly or daily).

    `levels` comes from `session_levels` and `atr_by_session` from
    `daily_atr`, both built from the same security's daily bars.
    """
    missing = {"date", "close"} - set(bars.columns)
    if missing:
        raise ValueError(f"bars are missing columns: {sorted(missing)}")

    close = bars["close"].to_numpy(dtype=float)
    atr = bars["date"].map(atr_by_session).to_numpy(dtype=float)
    usable_atr = np.where(atr > 0, atr, np.nan)
    by_date = levels.set_index("date")

    features = pd.DataFrame(index=bars.index)
    for name in LEVEL_COLUMNS:
        level = bars["date"].map(by_date[name]).to_numpy(dtype=float)
        features[f"dist_{name}"] = (close - level) / usable_atr
    features["dist_round_number"] = (close - nearest_round_number(close)) / usable_atr
    return features
