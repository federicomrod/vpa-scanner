"""Sequence - Concept v2 Section 5.7.

These are the only features that read other features rather than bars.
They describe what has been building up:

- `repeat_hv_narrow_5`: how many of the **previous five** bars were busy
  (volume percentile ≥ 90) but went nowhere (spread ≤ 0.6 ATR). One such
  bar is noise; four in a row is a pattern.
- `repeat_upper_reject_5`: how many of the previous five were busy and
  closed with a long upper wick - pushed up and sold back.
- `failed_new_high`: this bar's high beat the previous ten bars' high,
  but its close did not beat their highest close. It reached higher and
  could not hold it.
- `failed_new_low`: the mirror image.

Section 7 uses the repeat counts as **features, not filters** - the
filters stay deliberately permissive and the counts are passed forward
for the judgement layer.

Readings recorded in LEDGER-2:

- **"The trailing 5 bars" excludes the bar being measured**, as
  everywhere else in Section 5. A bar's own busy-and-narrow shape is
  already in its own features; the count says what led up to it.
- **"The prior 10-bar high close" is the highest close among those ten
  bars**, not the close of whichever bar made the high.
- These are **hourly features**: Section 5.7 names `vol_pct_slot_60` and
  `spread_atr`, which are hourly measures, and Section 7 applies them to
  hourly candidates.
- With fewer than the required bars behind it, a count or flag is null
  rather than partial.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Thresholds, frozen in Section 5.7.
HIGH_VOLUME_PERCENTILE = 90.0
NARROW_SPREAD_ATR = 0.6
LONG_UPPER_WICK = 0.5

#: Bars looked back over (Section 5.7).
REPEAT_WINDOW = 5
NEW_EXTREME_WINDOW = 10

COLUMNS = ["repeat_hv_narrow_5", "repeat_upper_reject_5", "failed_new_high", "failed_new_low"]


def sequence_features(
    bars: pd.DataFrame,
    volume_percentile: np.ndarray,
    spread_atr: np.ndarray,
    upper_wick_frac: np.ndarray,
) -> pd.DataFrame:
    """Section 5.7 for one security's hourly bars, in time order.

    The three arrays are `vol_pct_slot_60` (5.1), `spread_atr` and
    `upper_wick_frac` (5.2, 5.3) for the same bars.
    """
    missing = {"high", "low", "close"} - set(bars.columns)
    if missing:
        raise ValueError(f"bars are missing columns: {sorted(missing)}")

    busy = np.asarray(volume_percentile, dtype=float) >= HIGH_VOLUME_PERCENTILE
    narrow = np.asarray(spread_atr, dtype=float) <= NARROW_SPREAD_ATR
    rejected = np.asarray(upper_wick_frac, dtype=float) >= LONG_UPPER_WICK

    features = pd.DataFrame(index=bars.index)
    features["repeat_hv_narrow_5"] = _count_before(busy & narrow, REPEAT_WINDOW)
    features["repeat_upper_reject_5"] = _count_before(busy & rejected, REPEAT_WINDOW)

    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    previous_high = _extreme_before(high, NEW_EXTREME_WINDOW, np.max)
    previous_low = _extreme_before(low, NEW_EXTREME_WINDOW, np.min)
    highest_close = _extreme_before(close, NEW_EXTREME_WINDOW, np.max)
    lowest_close = _extreme_before(close, NEW_EXTREME_WINDOW, np.min)

    features["failed_new_high"] = _flag(
        (high > previous_high) & (close <= highest_close),
        unknown=_any_unknown(high, close, previous_high, highest_close),
    )
    features["failed_new_low"] = _flag(
        (low < previous_low) & (close >= lowest_close),
        unknown=_any_unknown(low, close, previous_low, lowest_close),
    )
    return features


def _any_unknown(*values: np.ndarray) -> np.ndarray:
    """True where any input is missing, so the answer is "cannot tell"
    rather than "did not happen"."""
    unknown = np.zeros(len(values[0]), dtype=bool)
    for value in values:
        unknown |= np.isnan(value)
    return unknown


def _count_before(flags: np.ndarray, window: int) -> np.ndarray:
    """How many of the `window` bars **before** each bar are true.

    Null until a full window exists, so a count is never partial.
    """
    counted = pd.Series(flags.astype(float)).shift(1).rolling(window, min_periods=window).sum()
    return counted.to_numpy()


def _extreme_before(values: np.ndarray, window: int, extreme) -> np.ndarray:
    """The highest or lowest of the `window` bars before each bar."""
    series = pd.Series(values).shift(1).rolling(window, min_periods=window)
    return (series.max() if extreme is np.max else series.min()).to_numpy()


def _flag(condition: np.ndarray, unknown: np.ndarray) -> pd.array:
    """A true/false flag, null where the comparison could not be made."""
    return pd.array(np.where(unknown, None, condition), dtype="boolean")
