"""Volume normalisation - Concept v2 Section 5.1.

The question these answer is "was this hour busy **for this stock, at
this time of day**?" - not "was it busy compared with some other stock",
and not "compared with a different hour of the day". A stock's 09:30
hour is always busier than its lunchtime hour, so each slot is compared
only with the same slot on previous sessions.

Hourly, per `slot_index`:

- `vol_pct_slot_60`: percentile of log(volume + 1) over the trailing 60
  sessions; at least 40 valid observations or the feature is null and the
  bar cannot be a candidate.
- `vol_pct_slot_20`: the same over 20 sessions.
- `vol_z_slot_60`: (log(v) - median) / (1.4826 x MAD) over the same
  window. Median and MAD rather than mean and standard deviation, so one
  spike cannot inflate the baseline it is measured against.

Daily, with no slot dimension: `vol_pct_d_120`, `vol_pct_d_20`,
`vol_z_d_120`.

Both the short and long windows are kept: disagreement between them marks
a change of regime and is itself informative (Section 5.1).

What counts as a valid observation, and why half days are left out
entirely, is LEDGER-2 decisions 2 and 3.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vpa.signal.windows import (
    baseline_valid,
    log_volume,
    measurable,
    trailing_percentile,
    trailing_robust_z,
)

#: Trailing sessions for the hourly slot baselines (Section 5.1).
SLOT_WINDOW_LONG = 60
SLOT_WINDOW_SHORT = 20

#: Trailing sessions for the daily baselines (Section 5.1).
DAILY_WINDOW_LONG = 120
DAILY_WINDOW_SHORT = 20

#: Section 5.1 requires "≥40 valid observations" for the 60-session
#: window and is silent about the others. The same two-thirds is carried
#: across: 40 of 60, 14 of 20, 80 of 120 (LEDGER-2 - flagged for review).
MIN_VALID_FRACTION = 2 / 3

HOURLY_COLUMNS = ["vol_pct_slot_60", "vol_pct_slot_20", "vol_z_slot_60"]
DAILY_COLUMNS = ["vol_pct_d_120", "vol_pct_d_20", "vol_z_d_120"]


def min_valid(window: int) -> int:
    """How many valid observations a window of this size needs."""
    return int(np.ceil(window * MIN_VALID_FRACTION))


def hourly_volume_features(bars: pd.DataFrame) -> pd.DataFrame:
    """The Section 5.1 hourly features for one security's hourly bars.

    `bars` must be one security's bars in time order, with `slot_index`,
    `volume`, `low_quality` and `is_half_day`. Each slot is measured
    against its own history. Half-day bars come back null (LEDGER-2,
    decision 2).
    """
    _require(bars, ["slot_index", "volume", "low_quality"])
    features = pd.DataFrame(
        {column: np.full(len(bars), np.nan) for column in HOURLY_COLUMNS}, index=bars.index
    )
    for _, slot_bars in bars.groupby("slot_index", sort=False):
        rows = bars.index.get_indexer(slot_bars.index)
        values = log_volume(slot_bars["volume"])
        valid = baseline_valid(slot_bars)
        features.iloc[rows, 0] = trailing_percentile(
            values, valid, SLOT_WINDOW_LONG, min_valid(SLOT_WINDOW_LONG)
        )
        features.iloc[rows, 1] = trailing_percentile(
            values, valid, SLOT_WINDOW_SHORT, min_valid(SLOT_WINDOW_SHORT)
        )
        features.iloc[rows, 2] = trailing_robust_z(
            values, valid, SLOT_WINDOW_LONG, min_valid(SLOT_WINDOW_LONG)
        )
    return features.where(np.repeat(measurable(bars)[:, None], len(HOURLY_COLUMNS), axis=1))


def daily_volume_features(bars: pd.DataFrame) -> pd.DataFrame:
    """The Section 5.1 daily features for one security's daily bars, in
    time order. No slot dimension."""
    _require(bars, ["volume", "low_quality"])
    values = log_volume(bars["volume"])
    valid = baseline_valid(bars)
    return pd.DataFrame(
        {
            "vol_pct_d_120": trailing_percentile(
                values, valid, DAILY_WINDOW_LONG, min_valid(DAILY_WINDOW_LONG)
            ),
            "vol_pct_d_20": trailing_percentile(
                values, valid, DAILY_WINDOW_SHORT, min_valid(DAILY_WINDOW_SHORT)
            ),
            "vol_z_d_120": trailing_robust_z(
                values, valid, DAILY_WINDOW_LONG, min_valid(DAILY_WINDOW_LONG)
            ),
        },
        index=bars.index,
    )


def _require(bars: pd.DataFrame, columns: list[str]) -> None:
    missing = set(columns) - set(bars.columns)
    if missing:
        raise ValueError(f"bars are missing columns: {sorted(missing)}")
