"""The nearest level - Concept v2 Section 5.6 (part 3).

Section 5.6 asks for two things about "the nearest level": how often
price has touched it, and how long ago it was set. It lists those two
beside its distance features without saying which level they describe,
so the project owner ruled (LEDGER-2, decision 7) that only the levels
Section 7 already uses as pattern anchors are eligible - no new ones:

| Level | Where it comes from |
|---|---|
| 20-day high, 20-day low | Family A's location test |
| Prior-week high, prior-week low | Family A's location test |
| Prior-month high | Family B's resistance references |
| Nearest swing pivot high | Family B's resistance references |

The set is asymmetric - highs without their matching lows - because
Family B is short-biased and names only the highs. That is inherited
from the frozen specification and deliberately not evened out.

Readings recorded in LEDGER-2:

- **Age is counted in daily sessions**, since every eligible level is a
  daily one, and dates from the session that **set** the level: the
  session that made the 20-day high, or that made the prior week's high,
  or the swing pivot's own extreme.
- **A touch is a session whose high-low range contains the level**, and
  they are counted over the trailing 60 sessions - the same window
  Section 5.6 uses elsewhere.
- **The nearest level is chosen per bar**, so two hours of the same
  session can be measured against different levels if the price moved
  between them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Sessions over which touches are counted (LEDGER-2).
TOUCH_WINDOW = 60

#: The eligible levels, and whether each is a high or a low - stated
#: rather than inferred from the name, because "high_20" does not end in
#: "high" and guessing it wrong searches the lows for a high.
ANCHOR_SIDE = {
    "high_20": "high",
    "low_20": "low",
    "prior_week_high": "high",
    "prior_week_low": "low",
    "prior_month_high": "high",
    "pivot_high": "high",
}
ANCHORS = tuple(ANCHOR_SIDE)

COLUMNS = ["level_touch_count", "level_age_bars"]


def anchor_prices(levels: pd.DataFrame, pivots: pd.DataFrame) -> pd.DataFrame:
    """The eligible levels' prices per session, from Sections 5.6 part 1
    and part 2."""
    prices = pd.DataFrame({"date": levels["date"].to_numpy()})
    for name in ANCHORS:
        if name == "pivot_high":
            prices[name] = pivots["pivot_high"].to_numpy()
        else:
            prices[name] = levels[name].to_numpy()
    return prices


def anchor_set_positions(
    daily: pd.DataFrame, levels: pd.DataFrame, pivots: pd.DataFrame
) -> pd.DataFrame:
    """Which session set each level, as a position in `daily`.

    For a rolling extreme that is the session holding it; for a prior
    week or month, the session inside that period that made it; for a
    pivot, the extreme itself.
    """
    high = daily["high"].to_numpy(dtype=float)
    low = daily["low"].to_numpy(dtype=float)
    positions = pd.DataFrame(index=daily.index)
    for name in ANCHORS:
        if name == "pivot_high":
            positions[name] = pivots["pivot_high_at"].to_numpy(dtype=float)
            continue
        level = levels[name].to_numpy(dtype=float)
        source = high if ANCHOR_SIDE[name] == "high" else low
        positions[name] = _last_position_at(source, level)
    return positions


def _last_position_at(source: np.ndarray, level: np.ndarray) -> np.ndarray:
    """The most recent earlier session whose high (or low) is the level."""
    found = np.full(len(level), np.nan)
    for i, wanted in enumerate(level):
        if np.isnan(wanted) or i == 0:
            continue
        matches = np.flatnonzero(source[:i] == wanted)
        if len(matches):
            found[i] = matches[-1]
    return found


def touch_counts(daily: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """How many of the trailing 60 sessions' ranges contain each level."""
    high = daily["high"].to_numpy(dtype=float)
    low = daily["low"].to_numpy(dtype=float)
    counts = pd.DataFrame(index=daily.index)
    for name in ANCHORS:
        level = prices[name].to_numpy(dtype=float)
        totals = np.full(len(daily), np.nan)
        for i, wanted in enumerate(level):
            if np.isnan(wanted) or i == 0:
                continue
            window = slice(max(0, i - TOUCH_WINDOW), i)
            totals[i] = np.count_nonzero((low[window] <= wanted) & (high[window] >= wanted))
        counts[name] = totals
    return counts


def nearest_level_features(
    bars: pd.DataFrame,
    prices: pd.DataFrame,
    positions: pd.DataFrame,
    counts: pd.DataFrame,
    session_position: pd.Series,
) -> pd.DataFrame:
    """Section 5.6's `level_touch_count` and `level_age_bars`.

    `session_position` maps each session date to its position in the
    daily bars, so an age can be counted in sessions.
    """
    missing = {"date", "close"} - set(bars.columns)
    if missing:
        raise ValueError(f"bars are missing columns: {sorted(missing)}")

    rows = np.arange(len(bars))
    close = bars["close"].to_numpy(dtype=float)

    def per_anchor(table: pd.DataFrame, source: pd.DataFrame) -> np.ndarray:
        """One row per eligible level, one column per bar."""
        lookup = table.set_index(source["date"].to_numpy())
        return np.vstack([bars["date"].map(lookup[name]).to_numpy(dtype=float) for name in ANCHORS])

    level_prices = per_anchor(prices.drop(columns="date"), prices)
    gaps = np.abs(level_prices - close[None, :])
    # A level that does not exist yet must never win, so its gap is infinite.
    comparable = np.where(np.isnan(gaps), np.inf, gaps)
    nearest = np.argmin(comparable, axis=0)
    nothing_near = np.isinf(comparable[nearest, rows])

    touch = np.where(nothing_near, np.nan, per_anchor(counts, prices)[nearest, rows])
    set_at = np.where(nothing_near, np.nan, per_anchor(positions, prices)[nearest, rows])
    here = bars["date"].map(session_position).to_numpy(dtype=float)
    return pd.DataFrame(
        {"level_touch_count": touch, "level_age_bars": here - set_at}, index=bars.index
    )
