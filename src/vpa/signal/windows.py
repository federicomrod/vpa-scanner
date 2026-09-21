"""Trailing-window machinery for the Section 5 features.

Every measurement in Section 5 compares a bar with what came **before**
it. This module is the one place that decides what "before" means, so the
rule is implemented once and tested once instead of being re-derived in
forty places:

    for the bar at position i, the window is positions i-window .. i-1

The bar being measured is never in its own window. A window that includes
the current bar is look-ahead bias, the most damaging bug available to
this project (CLAUDE.md rule 5), and it is invisible by eye: the numbers
look entirely reasonable.

Decisions recorded in LEDGER-2:

- **Percentile** = the share of trailing values **strictly below** the
  current one. Ties do not count as below, so the highest of 60 scores
  98.3 rather than 100 (decision 1).
- A trailing bar is a **valid observation** only if it exists, is not
  `low_quality`, and comes from a full session - never a half day
  (decisions 2 and 3).
- Below `min_valid` valid observations the answer is null, never a number
  computed from a handful of bars.
- A **robust z-score with zero spread is null**, not infinite.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

#: Section 5.1 requires "≥40 valid observations" for a 60-session window
#: and is silent about the others; the same two-thirds is carried across
#: (14 of 20, 80 of 120) - LEDGER-2, flagged for review.
MIN_VALID_FRACTION = 2 / 3


def min_valid(window: int) -> int:
    """How many valid observations a window of this size needs."""
    return int(np.ceil(window * MIN_VALID_FRACTION))


#: Scales the median absolute deviation to match a standard deviation
#: for normally distributed data (Concept v2 Section 5.1).
MAD_TO_SIGMA = 1.4826


def log_volume(volume: pd.Series | np.ndarray) -> np.ndarray:
    """`log(volume + 1)`, the scale volume is compared on (Section 5.1).

    The +1 keeps an hour with no trades at zero instead of minus
    infinity.
    """
    return np.log(np.asarray(volume, dtype=float) + 1.0)


def baseline_valid(bars: pd.DataFrame) -> np.ndarray:
    """Which bars may be used as trailing observations (LEDGER-2, 3).

    Needs `low_quality`; uses `is_half_day` when present.
    """
    valid = ~bars["low_quality"].to_numpy(dtype=bool)
    if "is_half_day" in bars.columns:
        valid &= ~bars["is_half_day"].to_numpy(dtype=bool)
    return valid


def measurable(bars: pd.DataFrame) -> np.ndarray:
    """Which bars may have volume and spread percentiles at all.

    Half-day bars may not (LEDGER-2, decision 2): their slots are not
    comparable with a normal day's. They are still stored and reported -
    they simply cannot fire.
    """
    if "is_half_day" not in bars.columns:
        return np.ones(len(bars), dtype=bool)
    return ~bars["is_half_day"].to_numpy(dtype=bool)


def _windows(values: np.ndarray, valid: np.ndarray, window: int) -> np.ndarray:
    """A (rows x window) view where row i holds positions i-window..i-1,
    with invalid observations blanked out. Padded with blanks at the
    start, so early rows simply have fewer observations."""
    blanked = np.where(valid, np.asarray(values, dtype=float), np.nan)
    padded = np.concatenate([np.full(window, np.nan), blanked])
    return sliding_window_view(padded, window)[: len(blanked)]


def trailing_count(valid: np.ndarray, window: int) -> np.ndarray:
    """How many valid observations each bar's window holds."""
    counts = _windows(np.ones(len(valid)), valid, window)
    return np.count_nonzero(~np.isnan(counts), axis=1)


def trailing_percentile(
    values: pd.Series | np.ndarray,
    valid: np.ndarray,
    window: int,
    min_valid: int,
) -> np.ndarray:
    """Percentile rank of each value among its trailing window.

    The share of trailing valid observations **strictly below** the
    value, as a percentage (LEDGER-2, decision 1). Null where the window
    holds fewer than `min_valid` observations.
    """
    current = np.asarray(values, dtype=float)
    past = _windows(current, valid, window)
    counts = np.count_nonzero(~np.isnan(past), axis=1)
    below = np.count_nonzero(past < current[:, None], axis=1)  # NaN compares False
    with np.errstate(invalid="ignore", divide="ignore"):
        percentile = 100.0 * below / counts
    return np.where(counts >= min_valid, percentile, np.nan)


def trailing_robust_z(
    values: pd.Series | np.ndarray,
    valid: np.ndarray,
    window: int,
    min_valid: int,
) -> np.ndarray:
    """`(value - median) / (1.4826 x MAD)` over the trailing window.

    Median and MAD rather than mean and standard deviation, because one
    spike would otherwise inflate the baseline it is measured against
    (Section 5.1). Null where there are too few observations, or where
    the MAD is zero and the score would be undefined.
    """
    current = np.asarray(values, dtype=float)
    past = _windows(current, valid, window)
    counts = np.count_nonzero(~np.isnan(past), axis=1)
    with warnings_suppressed():
        median = np.nanmedian(past, axis=1)
        mad = np.nanmedian(np.abs(past - median[:, None]), axis=1)
    spread = MAD_TO_SIGMA * mad
    with np.errstate(invalid="ignore", divide="ignore"):
        score = (current - median) / spread
    return np.where((counts >= min_valid) & (spread > 0), score, np.nan)


def trailing_median(
    values: pd.Series | np.ndarray,
    valid: np.ndarray,
    window: int,
    min_valid: int,
) -> np.ndarray:
    """The median of each bar's trailing window - the "usual" level to
    compare the bar against. Null where there are too few observations."""
    past = _windows(np.asarray(values, dtype=float), valid, window)
    counts = np.count_nonzero(~np.isnan(past), axis=1)
    with warnings_suppressed():
        median = np.nanmedian(past, axis=1)
    return np.where(counts >= min_valid, median, np.nan)


class warnings_suppressed:
    """Quietens numpy's "all-NaN slice" warning: an empty window is an
    expected state at the start of a history, not a problem."""

    def __enter__(self):
        import warnings

        self._context = warnings.catch_warnings()
        self._context.__enter__()
        warnings.filterwarnings("ignore", message="All-NaN slice encountered")
        warnings.filterwarnings("ignore", message="Mean of empty slice")
        return self

    def __exit__(self, *exc):
        return self._context.__exit__(*exc)
