"""The look-ahead test: does a feature ever see the future?

Every Section 5 feature must be checked with this. A feature computed for
Tuesday must not change when Wednesday's data changes - and if it does,
a backtest built on it will look wonderful and mean nothing, while the
numbers themselves look perfectly reasonable (CLAUDE.md rule 5).

Usage in a test:

    assert_ignores_the_future(lambda bars: my_feature(bars), example_bars)

It walks through the bars, rewrites everything after each point with
wildly different numbers, recomputes, and fails if any earlier answer
moved.

**What this catches:** a feature reading later bars - directly, or
through a statistic computed over the whole history at once.

**What it cannot catch,** because the current bar's own data is not
future data: a baseline window that includes the bar being measured.
That is the other half of CLAUDE.md rule 5, and it is checked separately
against the window machinery itself (see `tests/test_windows.py`:
a window of 2 must hold 0, 1, 2, 2 observations, never 1, 2, 2, 2).
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

#: Columns rewritten in the tampered future, and how.
FUTURE_TAMPERING = {
    "open": lambda s: s * 3.0 + 7.0,
    "high": lambda s: s * 4.0 + 9.0,
    "low": lambda s: s * 0.1,
    "close": lambda s: s * 3.5 + 8.0,
    "volume": lambda s: s * 1000.0 + 5.0,
    "transactions": lambda s: s * 17 + 3,
    "minutes_with_trades": lambda s: np.minimum(s * 0 + 60, 60),
    "low_quality": lambda s: ~s.astype(bool),
    "is_half_day": lambda s: ~s.astype(bool),
}


def tamper_with_the_future(bars: pd.DataFrame, after: int) -> pd.DataFrame:
    """A copy of `bars` where every row after position `after` is changed."""
    tampered = bars.copy().reset_index(drop=True)
    future = tampered.index > after
    for column, change in FUTURE_TAMPERING.items():
        if column in tampered.columns:
            tampered.loc[future, column] = change(tampered.loc[future, column])
    return tampered


def assert_ignores_the_future(
    compute: Callable[[pd.DataFrame], np.ndarray | pd.Series],
    bars: pd.DataFrame,
    cut_points: list[int] | None = None,
) -> None:
    """Fail if any value changes when later bars change."""
    bars = bars.reset_index(drop=True)
    baseline = np.asarray(compute(bars), dtype=float)
    assert len(baseline) == len(bars), "one value per bar expected"
    points = cut_points if cut_points is not None else range(len(bars) - 1)

    for cut in points:
        after = np.asarray(compute(tamper_with_the_future(bars, cut)), dtype=float)
        before_and_including = slice(0, cut + 1)
        if not _same(baseline[before_and_including], after[before_and_including]):
            differing = np.flatnonzero(
                ~_elementwise_same(baseline[before_and_including], after[before_and_including])
            )
            raise AssertionError(
                f"Feature saw the future: changing bars after position {cut} changed the "
                f"value(s) at position(s) {list(differing)}. "
                f"Was {baseline[differing[0]]!r}, became {after[differing[0]]!r}."
            )


def _elementwise_same(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (np.isnan(a) & np.isnan(b)) | (a == b)


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    return bool(_elementwise_same(a, b).all())
