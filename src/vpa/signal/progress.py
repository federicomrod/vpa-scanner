"""Price progress - Concept v2 Section 5.4.

Where Section 5.2 asks how far a bar travelled, these ask how far the
price actually **got**, and at what cost in trading:

- `ret_atr`: this bar's move (close to close) in ATR units. The bar's own
  range is irrelevant here - only where it ended up.
- `progress_3`, `progress_5`, `progress_10`: the net move over the last
  3, 5 and 10 bars, again in ATR units. A stock can have ten busy bars
  and no progress at all, which is exactly the effort-without-result
  Pattern A looks for.
- `cum_vol_pct_3`, `cum_vol_pct_5`: how that stretch's total volume ranks
  against the same stretch on previous sessions - the effort side of the
  same comparison.
- `efficiency`: movement achieved per unit of participation,
  `|ret_atr| / max(volume / median slot volume, 0.1)`. Low means a lot of
  trading moved the price very little.

Readings recorded in LEDGER-2:

- A span of N bars means this bar and the N-1 before it. Bars run
  continuously across sessions, as in Section 5.2, so a span can reach
  back into yesterday.
- `median slot volume` is the median over the same trailing 60 sessions
  and the same slot as Section 5.1 uses, with the same validity rules.
  The 0.1 floor in the specification then does its job: without it, a
  stock that barely traded would show enormous efficiency.
- Half-day bars get the price measurements (`ret_atr`, `progress_*`) but
  not the volume-baseline ones (`cum_vol_pct_*`, `efficiency`), for the
  same reason as Section 5.2 (LEDGER-2, decision 2).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vpa.signal.volatility import volatility_features
from vpa.signal.windows import (
    baseline_valid,
    measurable,
    min_valid,
    trailing_median,
    trailing_percentile,
)

#: Spans for the net-move features (Section 5.4).
PROGRESS_SPANS = (3, 5, 10)

#: Spans for the cumulative-volume percentiles (Section 5.4).
CUMULATIVE_VOLUME_SPANS = (3, 5)

#: Trailing sessions for the volume baselines, as Section 5.1.
VOLUME_WINDOW = 60

#: Floor on participation in `efficiency`, from Section 5.4 itself.
MIN_PARTICIPATION = 0.1

COLUMNS = (
    ["ret_atr"]
    + [f"progress_{span}" for span in PROGRESS_SPANS]
    + [f"cum_vol_pct_{span}" for span in CUMULATIVE_VOLUME_SPANS]
    + ["efficiency"]
)


def progress_features(
    bars: pd.DataFrame, hourly: bool, atr: np.ndarray | None = None
) -> pd.DataFrame:
    """Section 5.4 for one security's bars, in time order.

    `atr` may be passed in when Section 5.2 has already computed it.
    """
    needed = ["close", "volume", "low_quality"] + (["slot_index"] if hourly else [])
    missing = set(needed) - set(bars.columns)
    if missing:
        raise ValueError(f"bars are missing columns: {sorted(missing)}")
    if atr is None:
        atr = volatility_features(bars, hourly=hourly)["atr20"].to_numpy()

    close = bars["close"].to_numpy(dtype=float)
    volume = bars["volume"].to_numpy(dtype=float)
    usable_atr = np.where(np.asarray(atr, dtype=float) > 0, atr, np.nan)

    features = pd.DataFrame(index=bars.index)
    features["ret_atr"] = (close - _shifted(close, 1)) / usable_atr
    for span in PROGRESS_SPANS:
        features[f"progress_{span}"] = (close - _shifted(close, span)) / usable_atr

    for span in CUMULATIVE_VOLUME_SPANS:
        features[f"cum_vol_pct_{span}"] = _cumulative_volume_percentile(bars, volume, span, hourly)

    features["efficiency"] = _efficiency(bars, features["ret_atr"].to_numpy(), volume, hourly)
    if hourly:
        cannot = ~measurable(bars)
        features.loc[cannot, [f"cum_vol_pct_{s}" for s in CUMULATIVE_VOLUME_SPANS]] = np.nan
        features.loc[cannot, "efficiency"] = np.nan
    return features


def _shifted(values: np.ndarray, by: int) -> np.ndarray:
    """`values` from `by` bars ago; unknown at the start of a history,
    and unknown everywhere when the history is shorter than the span."""
    if by >= len(values):
        return np.full(len(values), np.nan)
    return np.concatenate([np.full(by, np.nan), values[:-by]])


def _rolling_sum(values: np.ndarray, span: int) -> np.ndarray:
    """The total over this bar and the `span - 1` before it."""
    totals = pd.Series(values).rolling(span, min_periods=span).sum()
    return totals.to_numpy()


def _rolling_all(flags: np.ndarray, span: int) -> np.ndarray:
    """True where this bar and the `span - 1` before it are all true."""
    counted = pd.Series(flags.astype(float)).rolling(span, min_periods=span).sum()
    return (counted == span).to_numpy()


def _by_slot(bars: pd.DataFrame, hourly: bool):
    """Each slot's rows, or all rows at once for daily bars."""
    if not hourly:
        yield np.arange(len(bars)), bars
        return
    for _, slot_bars in bars.groupby("slot_index", sort=False):
        yield bars.index.get_indexer(slot_bars.index), slot_bars


def _cumulative_volume_percentile(
    bars: pd.DataFrame, volume: np.ndarray, span: int, hourly: bool
) -> np.ndarray:
    """Where this stretch's total volume ranks among the same stretch,
    ending at the same slot, over the trailing 60 sessions.

    A stretch counts only if **every** bar in it is a valid observation;
    a total that includes a dead or half-day hour is neither compared
    against others nor offered as one (LEDGER-2, decision 3).
    """
    result = np.full(len(bars), np.nan)
    totals = _rolling_sum(volume, span)
    whole_span_valid = _rolling_all(baseline_valid(bars), span)
    for rows, _ in _by_slot(bars, hourly):
        usable = whole_span_valid[rows] & ~np.isnan(totals[rows])
        percentile = trailing_percentile(
            totals[rows], usable, VOLUME_WINDOW, min_valid(VOLUME_WINDOW)
        )
        result[rows] = np.where(usable, percentile, np.nan)
    return result


def _efficiency(
    bars: pd.DataFrame, ret_atr: np.ndarray, volume: np.ndarray, hourly: bool
) -> np.ndarray:
    """Movement achieved per unit of participation (Section 5.4)."""
    usual = np.full(len(bars), np.nan)
    for rows, slot_bars in _by_slot(bars, hourly):
        usual[rows] = trailing_median(
            volume[rows], baseline_valid(slot_bars), VOLUME_WINDOW, min_valid(VOLUME_WINDOW)
        )
    with np.errstate(invalid="ignore", divide="ignore"):
        participation = np.where(usual > 0, volume / usual, np.nan)
        return np.abs(ret_atr) / np.maximum(participation, MIN_PARTICIPATION)
