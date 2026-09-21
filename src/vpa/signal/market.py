"""Market-relative features - Concept v2 Section 5.5.

On a day when the index falls 2%, hundreds of stocks close weakly on
heavy volume. Without this section the shortlist would return twelve
tickers that are economically one position. These features remove the
market's move and ask what the stock did on its own:

- `beta_60`: how much this stock usually moves when the market moves,
  from an ordinary least-squares fit of 60 trailing **daily** returns
  against SPY, refreshed weekly.
- `resid_ret_atr`: the part of this bar's move the market does not
  explain, in ATR units. Near zero means the stock simply went with the
  market; a large value means it moved on its own account.
- `market_ret_day`: SPY's return that day, kept as context.
- `sector_resid_ret_atr`, `sector_ret_day`: **left empty** until the
  sector classification source is chosen (Section 16, open item 2;
  LEDGER-2 decision 5). Filling them in later changes nothing else.

Readings recorded in LEDGER-2:

- **Units.** Section 5.5 writes `(ret - beta x ret_SPY) / atr20`, but a
  stock's move in dollars cannot be subtracted from the market's move in
  percent. SPY's return is therefore converted into this stock's own
  money first - `beta x ret_SPY x previous close` - and the leftover
  dollars are divided by `atr20`. This is the only reading where the
  arithmetic is coherent, and it leaves `resid_ret_atr` on the same
  scale as `ret_atr` (Section 5.4), which is what Section 7 compares.
- **The daily beta is used for hourly bars too** (decision 4): it is the
  only beta the specification defines.
- **"Refreshed weekly"** means recomputed on the first trading day of
  each week and held for the rest of it. The fit always uses returns
  through the previous session, so the number in force on a Monday knows
  nothing of that Monday.
- A beta needs 40 of the 60 trailing daily returns, the same floor as
  Section 5.1. Below that, `beta_60` and `resid_ret_atr` are null.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from vpa.signal.windows import min_valid

#: Trailing daily returns in the beta fit (Section 5.5).
BETA_WINDOW = 60

COLUMNS = [
    "beta_60",
    "resid_ret_atr",
    "market_ret_day",
    "sector_resid_ret_atr",
    "sector_ret_day",
]


def daily_returns(bars: pd.DataFrame) -> pd.Series:
    """Close-to-close returns as fractions, indexed by session date."""
    closes = bars.set_index("date")["close"].astype(float)
    return closes.pct_change()


def rolling_beta(stock: pd.Series, market: pd.Series) -> pd.Series:
    """OLS beta of `stock` on `market` over the trailing 60 returns,
    through the previous session only.

    Beta is `covariance / variance` over the window, which is the slope
    of the least-squares line.
    """
    aligned = pd.DataFrame({"stock": stock, "market": market}).dropna()
    if len(aligned) <= BETA_WINDOW:
        return pd.Series(np.nan, index=stock.index)

    windows = {
        name: sliding_window_view(aligned[name].to_numpy(dtype=float), BETA_WINDOW)[:-1]
        for name in ("stock", "market")
    }
    stock_window, market_window = windows["stock"], windows["market"]
    stock_centred = stock_window - stock_window.mean(axis=1, keepdims=True)
    market_centred = market_window - market_window.mean(axis=1, keepdims=True)
    variance = (market_centred**2).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = (stock_centred * market_centred).sum(axis=1) / variance
    slope = np.where(variance > 0, slope, np.nan)

    # Window k ends at position k + BETA_WINDOW - 1, so it is the beta
    # known to the session after that: position k + BETA_WINDOW.
    betas = pd.Series(np.nan, index=aligned.index)
    betas.iloc[BETA_WINDOW:] = slope
    return betas.reindex(stock.index)


def refreshed_weekly(betas: pd.Series) -> pd.Series:
    """Hold each week's beta from its first trading day (Section 5.5).

    The value in force on a Monday was fitted through the previous
    Friday, so no part of the week being measured is in it.
    """
    weeks = pd.Series(pd.to_datetime(betas.index).isocalendar().week.to_numpy(), index=betas.index)
    years = pd.Series(pd.to_datetime(betas.index).isocalendar().year.to_numpy(), index=betas.index)
    first_of_week = (weeks != weeks.shift()) | (years != years.shift())
    return betas.where(first_of_week).ffill()


def beta_by_session(stock_daily: pd.DataFrame, market_daily: pd.DataFrame) -> pd.Series:
    """The beta in force on each session (fitted trailing, held weekly)."""
    stock = daily_returns(stock_daily)
    market = daily_returns(market_daily)
    fitted = rolling_beta(stock, market.reindex(stock.index))
    enough = (
        pd.DataFrame({"s": stock, "m": market.reindex(stock.index)})
        .notna()
        .all(axis=1)
        .rolling(BETA_WINDOW, min_periods=1)
        .sum()
        .shift(1)
    )
    return refreshed_weekly(fitted.where(enough >= min_valid(BETA_WINDOW)))


def market_features(
    bars: pd.DataFrame,
    market_bars: pd.DataFrame,
    betas: pd.Series,
    atr: np.ndarray,
) -> pd.DataFrame:
    """Section 5.5 for one security's bars (hourly or daily), in time order.

    `market_bars` are SPY's bars of the same kind; `betas` comes from
    `beta_by_session`; `atr` is Section 5.2's `atr20`.
    """
    missing = {"date", "close"} - set(bars.columns)
    if missing:
        raise ValueError(f"bars are missing columns: {sorted(missing)}")

    close = bars["close"].to_numpy(dtype=float)
    previous_close = np.concatenate([[np.nan], close[:-1]])
    stock_move = close - previous_close

    market_move = _market_return_per_bar(bars, market_bars)
    beta = bars["date"].map(betas).to_numpy(dtype=float)
    usable_atr = np.where(np.asarray(atr, dtype=float) > 0, atr, np.nan)

    with np.errstate(invalid="ignore", divide="ignore"):
        explained = beta * market_move * previous_close
        residual = (stock_move - explained) / usable_atr

    return pd.DataFrame(
        {
            "beta_60": beta,
            "resid_ret_atr": residual,
            "market_ret_day": bars["date"].map(_daily_market_return(market_bars)).to_numpy(float),
            "sector_resid_ret_atr": np.nan,  # LEDGER-2 decision 5
            "sector_ret_day": np.nan,
        },
        index=bars.index,
    )


def _market_return_per_bar(bars: pd.DataFrame, market_bars: pd.DataFrame) -> np.ndarray:
    """SPY's return over the same bar, matched by date (and slot, hourly)."""
    keys = ["date", "slot_index"] if "slot_index" in bars.columns else ["date"]
    market = market_bars.sort_values(keys).copy()
    market["market_return"] = market["close"].astype(float).pct_change()
    matched = bars[keys].merge(market[[*keys, "market_return"]], on=keys, how="left")
    return matched["market_return"].to_numpy(dtype=float)


def _daily_market_return(market_bars: pd.DataFrame) -> pd.Series:
    """SPY's return for each session, from its closing bar."""
    closes = market_bars.sort_values("date").groupby("date")["close"].last().astype(float)
    return closes.pct_change()
