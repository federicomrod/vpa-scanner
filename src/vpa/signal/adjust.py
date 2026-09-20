"""Split adjustment, applied at read time (Concept v2 Section 3.1).

Raw prices are stored exactly as traded and never changed. When a
calculation needs prices that are comparable across a stock split, it
calls this module to adjust them on the fly.

Point-in-time rule: adjustment is always "as of" a given date, using only
splits that had taken effect by that date. A split that happened later
must not change earlier numbers - that would leak future information
into a past decision.

Dividends are never applied to prices (Section 3.1).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

SPLIT_COLUMNS = ["ticker", "execution_date", "split_from", "split_to"]


def split_adjust_daily(
    bars: pd.DataFrame, splits: pd.DataFrame, as_of: date, ticker_column: str = "ticker"
) -> pd.DataFrame:
    """Return a split-adjusted copy of bars (daily or hourly), as of `as_of`.

    `bars` needs `date`, `close`, `volume` and a ticker column (any of
    `open`, `high`, `low`, `vwap` are adjusted too if present). `splits` needs
    `ticker`, `execution_date` (the first day trading at the new share
    count), `split_from`, `split_to` - a 2-for-1 split is from=1, to=2.

    A bar dated before a split's execution date, where the split took
    effect on or before `as_of`, has its prices multiplied by
    split_from/split_to and its volume divided by the same factor. So
    price x volume (dollar volume) is unchanged by adjustment.
    """
    missing = set(SPLIT_COLUMNS) - set(splits.columns)
    if missing:
        raise ValueError(f"splits table is missing columns: {sorted(missing)}")
    if ((splits["split_from"] <= 0) | (splits["split_to"] <= 0)).any():
        raise ValueError("splits table has a non-positive split_from/split_to")

    adjusted = bars.copy()
    factor = pd.Series(1.0, index=adjusted.index)
    # Dates can arrive as text (from storage) or as dates; make them dates
    # so the comparison can't silently match nothing.
    execution = pd.to_datetime(splits["execution_date"]).dt.date
    known = splits.assign(execution_date=execution)[execution <= as_of]
    for split in known.itertuples(index=False):
        before_split = (adjusted[ticker_column] == split.ticker) & (
            adjusted["date"] < split.execution_date
        )
        factor[before_split] *= split.split_from / split.split_to

    for column in ("open", "high", "low", "close", "vwap"):
        if column in adjusted.columns:
            adjusted[column] = adjusted[column] * factor
    adjusted["volume"] = adjusted["volume"] / factor
    return adjusted
