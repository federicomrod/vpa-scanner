"""Candle geometry - Concept v2 Section 5.3.

The shape of a single bar, in four numbers. These describe the bar alone
and use no history at all, so there is no window to get wrong here.

- `body_frac`: how much of the bar's travel ended up as net movement.
  Near 1 means it went one way and stayed; near 0 means it went out and
  came back.
- `upper_wick_frac`: the share of the bar above its body - price pushed
  up and was sold back down. This is the shape Pattern B is built on.
- `lower_wick_frac`: the mirror image below the body.
- `close_loc`: where the close sat within the high-low range. 0 is a
  close at the very low of the bar, 1 at the very high.

The first three are measured against the bar's **true range**, which
includes any gap from the previous close (Section 5.3), so a bar that
gapped can have small fractions even if its own high-low range was
decisive. `close_loc` uses the high-low range alone, as specified.

Readings recorded in LEDGER-2:

- A bar with **no trades** has no prices; all four are null.
- A bar whose **true range is zero** - or, for `close_loc`, whose high
  equals its low - has no shape to describe, so those are null rather
  than a division by zero.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vpa.signal.volatility import true_range

COLUMNS = ["body_frac", "upper_wick_frac", "lower_wick_frac", "close_loc"]


def geometry_features(bars: pd.DataFrame, ranges: np.ndarray | None = None) -> pd.DataFrame:
    """Section 5.3 for one security's bars, in time order.

    `ranges` may be passed in when the true range has already been
    computed for Section 5.2, so it isn't calculated twice.
    """
    missing = {"open", "high", "low", "close"} - set(bars.columns)
    if missing:
        raise ValueError(f"bars are missing columns: {sorted(missing)}")

    open_ = bars["open"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    span = true_range(bars) if ranges is None else np.asarray(ranges, dtype=float)
    high_low = high - low

    with np.errstate(invalid="ignore", divide="ignore"):
        return pd.DataFrame(
            {
                "body_frac": _divide(np.abs(close - open_), span),
                "upper_wick_frac": _divide(high - np.maximum(open_, close), span),
                "lower_wick_frac": _divide(np.minimum(open_, close) - low, span),
                "close_loc": _divide(close - low, high_low),
            },
            index=bars.index,
        )


def _divide(part: np.ndarray, whole: np.ndarray) -> np.ndarray:
    """`part / whole`, or null where the whole is zero or unknown - a bar
    that did not move has no shape to describe."""
    return np.where(whole > 0, part / np.where(whole > 0, whole, 1.0), np.nan)
