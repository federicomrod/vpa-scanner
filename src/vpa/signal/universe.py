"""Universe selection - Concept v2 Section 2.

Decides which stocks the scanner looks at for a month. This module is
pure logic: it is handed tables of reference data, daily bars and splits,
and returns the selected tickers. Fetching those tables and writing the
snapshot file live in `vpa.data.universe`.

Interpretations approved by the project owner (LEDGER-1):

- Everything is measured as of the **previous trading day's close** -
  the last complete data a pre-market scan on the rebalance date has.
  Nothing dated on or after the rebalance date is used.
- Screening uses the vendor's daily bars (the whole market is screened,
  not just our 400 stocks).
- Market cap is the vendor's figure, taken as of the previous trading day.
- "250 trading days of history" is counted from the vendor's listing date.
- The $10 floor and dollar volume use split-adjusted prices (adjusted as
  of the rebalance date - see `vpa.signal.adjust`).

Two further readings, recorded here so they are visible:

- Daily dollar volume = close x volume for that day.
- A trading day with no vendor bar counts as zero dollar volume (the same
  "no trades means zero volume" rule as Section 4.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
from pydantic import BaseModel, ConfigDict

from vpa.data.calendar import is_session, previous_session, sessions_ending
from vpa.signal.adjust import split_adjust_daily

SECURITY_COLUMNS = ["ticker", "type", "primary_exchange", "market_cap", "list_date"]
BAR_COLUMNS = ["ticker", "date", "close", "volume"]


class UniverseRules(BaseModel):
    """The Section 2 thresholds. Defaults are the frozen specification;
    tests may shrink `top_n` to work with a small fixture."""

    model_config = ConfigDict(frozen=True)

    #: Exchange codes (ISO MIC) for NYSE, Nasdaq and NYSE American.
    exchanges: frozenset[str] = frozenset({"XNYS", "XNAS", "XASE"})
    #: Vendor security type for common stock. ETFs, ETNs, funds, warrants,
    #: units, preferred shares and ADRs all have different type codes.
    security_type: str = "CS"
    min_market_cap: float = 2_000_000_000
    max_market_cap: float = 50_000_000_000
    dollar_volume_sessions: int = 60
    min_median_dollar_volume: float = 15_000_000
    min_close: float = 10.0
    min_history_sessions: int = 250
    top_n: int = 400


@dataclass(frozen=True)
class UniverseResult:
    """The outcome of a universe selection."""

    rebalance_date: date
    #: The trading day whose close everything was measured at.
    as_of_session: date
    #: One row per selected ticker, best (highest dollar volume) first.
    selected: pd.DataFrame
    #: Plain-language step -> how many stocks that step removed, in order.
    removed_by_step: dict[str, int]


def is_rebalance_date(day: date) -> bool:
    """Rebalance happens on the first trading day of each calendar month."""
    return is_session(day) and previous_session(day).month != day.month


def select_universe(
    rebalance_date: date,
    securities: pd.DataFrame,
    daily_bars: pd.DataFrame,
    splits: pd.DataFrame,
    rules: UniverseRules | None = None,
) -> UniverseResult:
    """Apply Section 2 to the given inputs.

    `securities`: one row per ticker, as the vendor described it as of the
    previous trading day - `ticker`, `type`, `primary_exchange`,
    `market_cap`, `list_date`. Must include since-delisted names that were
    trading then; that is what keeps the universe free of survivorship bias.

    `daily_bars`: unadjusted vendor daily bars - `ticker`, `date`,
    `close`, `volume`. Bars on or after the rebalance date are ignored.

    `splits`: see `vpa.signal.adjust.split_adjust_daily`.
    """
    rules = rules or UniverseRules()
    if not is_rebalance_date(rebalance_date):
        raise ValueError(f"{rebalance_date} is not the first trading day of a month")
    _require_columns("securities", securities, SECURITY_COLUMNS)
    _require_columns("daily_bars", daily_bars, BAR_COLUMNS)
    securities = securities.assign(list_date=_as_dates(securities["list_date"]))
    daily_bars = daily_bars.assign(date=_as_dates(daily_bars["date"]))
    splits = splits.assign(execution_date=_as_dates(splits["execution_date"]))
    if securities["ticker"].duplicated().any():
        raise ValueError("securities table lists a ticker more than once")
    if daily_bars.duplicated(["ticker", "date"]).any():
        raise ValueError("daily_bars has more than one bar for a ticker on the same day")

    as_of = previous_session(rebalance_date)
    window = sessions_ending(as_of, rules.dollar_volume_sessions)
    history_cutoff = sessions_ending(as_of, rules.min_history_sessions)[0]

    # Strictly trailing: only bars inside the window, which ends the day
    # before the rebalance date (CLAUDE.md rule 5).
    in_window = daily_bars[daily_bars["date"].isin(window)]
    adjusted = split_adjust_daily(in_window, splits, as_of=rebalance_date)
    adjusted = adjusted.assign(dollar_volume=adjusted["close"] * adjusted["volume"])

    median_dollar_volume = (
        adjusted.pivot(index="ticker", columns="date", values="dollar_volume")
        .reindex(columns=window)
        .fillna(0.0)  # no bar that day = zero dollar volume
        .median(axis=1)
    )
    last_close = adjusted[adjusted["date"] == as_of].set_index("ticker")["close"]

    candidates = securities.copy()
    candidates["median_dollar_volume_60"] = (
        candidates["ticker"].map(median_dollar_volume).fillna(0.0)
    )
    candidates["close"] = candidates["ticker"].map(last_close)

    steps: list[tuple[str, pd.Series]] = [
        (
            "not listed on NYSE, Nasdaq or NYSE American",
            candidates["primary_exchange"].isin(rules.exchanges),
        ),
        (
            "not common stock (ETF, fund, ADR, preferred, warrant, unit, ...)",
            candidates["type"] == rules.security_type,
        ),
        (
            f"fewer than {rules.min_history_sessions} trading days since listing",
            candidates["list_date"].notna() & (candidates["list_date"] <= history_cutoff),
        ),
        (
            f"market cap unknown or outside ${rules.min_market_cap / 1e9:g}bn"
            f"-${rules.max_market_cap / 1e9:g}bn",
            candidates["market_cap"].between(rules.min_market_cap, rules.max_market_cap),
        ),
        (
            f"no close on {as_of} or close below ${rules.min_close:g}",
            candidates["close"] >= rules.min_close,
        ),
        (
            f"median daily dollar volume below ${rules.min_median_dollar_volume:,.0f}",
            candidates["median_dollar_volume_60"] >= rules.min_median_dollar_volume,
        ),
    ]
    removed_by_step: dict[str, int] = {}
    keep = pd.Series(True, index=candidates.index)
    for label, passes in steps:
        removed_by_step[label] = int((keep & ~passes).sum())
        keep &= passes

    qualifying = candidates[keep].sort_values(
        ["median_dollar_volume_60", "ticker"], ascending=[False, True]
    )
    selected = qualifying.head(rules.top_n).reset_index(drop=True)
    removed_by_step[f"outside the top {rules.top_n} by dollar volume"] = len(qualifying) - len(
        selected
    )
    selected.insert(0, "rank", range(1, len(selected) + 1))
    selected.insert(0, "as_of_session", as_of)
    selected.insert(0, "rebalance_date", rebalance_date)

    return UniverseResult(
        rebalance_date=rebalance_date,
        as_of_session=as_of,
        selected=selected,
        removed_by_step=removed_by_step,
    )


def _as_dates(column: pd.Series) -> pd.Series:
    """Normalise a date column to plain `datetime.date` values, so that
    comparisons with calendar dates can't silently match nothing."""
    return pd.to_datetime(column).dt.date


def _require_columns(name: str, table: pd.DataFrame, columns: list[str]) -> None:
    missing = set(columns) - set(table.columns)
    if missing:
        raise ValueError(f"{name} table is missing columns: {sorted(missing)}")
