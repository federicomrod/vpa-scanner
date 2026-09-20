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
- Market cap (amended twice - see LEDGER-1): the vendor's **share-class**
  shares outstanding as of `share_count_lag_days` (105) calendar days
  before the previous trading day, times our previous-day close, with the
  share count brought forward through any split since. The vendor's own
  market cap is not used, and neither is its *weighted* share count: that
  one is frozen before 2022 (the same value repeated for every earlier
  date, and nothing at all for companies delisted before then), so it
  would put future share counts into past decisions. The share-class
  count moves with the date asked for. 105 days = the longest filing
  deadline (90 days for an annual report) plus the 15-day extension, so
  the filing behind the count was public by the rebalance date.
  Limitation: for a company with more than one share class this is that
  class's market cap, not the whole company's; such companies are
  detected and logged by the universe build.
- "250 trading days of history" is counted from the listing date: the
  earlier of the vendor's listing date and the security's first recorded
  ticker event, because the vendor's listing date may reset when a
  company changes ticker.
- The $10 floor and dollar volume use split-adjusted prices (adjusted as
  of the rebalance date - see `vpa.signal.adjust`).

Two further readings, recorded here so they are visible:

- Daily dollar volume = close x volume for that day.
- A trading day with no vendor bar counts as zero dollar volume (the same
  "no trades means zero volume" rule as Section 4.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from pydantic import BaseModel, ConfigDict

from vpa.data.calendar import is_session, previous_session, sessions_ending
from vpa.signal.adjust import split_adjust_daily

#: Needed for every security screened.
SCREEN_COLUMNS = ["ticker", "type", "primary_exchange"]
#: Also needed, but only for securities that pass `prescreen`. The
#: optional `first_event_date` column is used for the listing date too.
SECURITY_COLUMNS = [*SCREEN_COLUMNS, "list_date", "share_class_shares"]
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
    #: Calendar days between the share-count date and the as-of session.
    share_count_lag_days: int = 105


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


def share_count_date(rebalance_date: date, rules: UniverseRules | None = None) -> date:
    """The date to ask the vendor for share counts, for this rebalance."""
    rules = rules or UniverseRules()
    return previous_session(rebalance_date) - timedelta(days=rules.share_count_lag_days)


def prescreen(
    rebalance_date: date,
    securities: pd.DataFrame,
    daily_bars: pd.DataFrame,
    splits: pd.DataFrame,
    rules: UniverseRules | None = None,
) -> list[str]:
    """Tickers passing the cheap checks (exchange, type, price, dollar
    volume). Only these need listing dates and share counts looked up -
    every other security is excluded whatever those turn out to be."""
    rules = rules or UniverseRules()
    candidates, as_of = _measure(
        rebalance_date, securities, daily_bars, splits, rules, SCREEN_COLUMNS
    )
    keep = pd.Series(True, index=candidates.index)
    for _, passes in _cheap_steps(candidates, rules, as_of):
        keep &= passes
    return sorted(candidates.loc[keep, "ticker"])


def select_universe(
    rebalance_date: date,
    securities: pd.DataFrame,
    daily_bars: pd.DataFrame,
    splits: pd.DataFrame,
    rules: UniverseRules | None = None,
) -> UniverseResult:
    """Apply Section 2 to the given inputs.

    `securities`: one row per ticker, as the vendor described it as of the
    previous trading day - `ticker`, `type`, `primary_exchange`, plus
    `list_date`, optional `first_event_date` and `share_class_shares` (the
    vendor's share-class shares outstanding as of `share_count_date`), which
    may be missing for tickers that fail `prescreen`. Must include
    since-delisted names that were trading then; that is what keeps the
    universe free of survivorship bias.

    `daily_bars`: unadjusted vendor daily bars - `ticker`, `date`,
    `close`, `volume`. Bars on or after the rebalance date are ignored.

    `splits`: see `vpa.signal.adjust.split_adjust_daily`.
    """
    rules = rules or UniverseRules()
    candidates, as_of = _measure(
        rebalance_date, securities, daily_bars, splits, rules, SECURITY_COLUMNS
    )
    history_cutoff = sessions_ending(as_of, rules.min_history_sessions)[0]
    shares_date = share_count_date(rebalance_date, rules)
    listed_on = _listing_date(candidates)
    candidates["listing_date"] = listed_on.dt.date
    candidates["share_count_date"] = shares_date
    candidates["market_cap"] = point_in_time_market_cap(
        candidates, _dated_splits(splits), shares_date, rebalance_date
    )

    # Cheap checks first (so `prescreen` can skip lookups for the rest);
    # the order changes only which step gets the credit, not the result.
    steps = [
        *_cheap_steps(candidates, rules, as_of),
        (
            f"fewer than {rules.min_history_sessions} trading days since listing",
            listed_on <= pd.Timestamp(history_cutoff),  # unknown (NaT) fails
        ),
        (
            f"market cap unknown or outside ${rules.min_market_cap / 1e9:g}bn"
            f"-${rules.max_market_cap / 1e9:g}bn",
            candidates["market_cap"].between(rules.min_market_cap, rules.max_market_cap),
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


def point_in_time_market_cap(
    candidates: pd.DataFrame, splits: pd.DataFrame, shares_date: date, rebalance_date: date
) -> pd.Series:
    """share_class_shares x close, with the share count brought forward
    through any split taking effect after `shares_date` and up to the
    rebalance date - the same basis as `close`, which is split-adjusted
    as of the rebalance date."""
    factor = pd.Series(1.0, index=candidates.index)
    later = splits[
        (splits["execution_date"] > shares_date) & (splits["execution_date"] <= rebalance_date)
    ]
    for split in later.itertuples(index=False):
        factor[candidates["ticker"] == split.ticker] *= split.split_to / split.split_from
    return candidates["share_class_shares"] * factor * candidates["close"]


def _measure(
    rebalance_date: date,
    securities: pd.DataFrame,
    daily_bars: pd.DataFrame,
    splits: pd.DataFrame,
    rules: UniverseRules,
    required: list[str],
) -> tuple[pd.DataFrame, date]:
    """Validate inputs and add each security's close and median dollar
    volume as of the previous trading day."""
    if not is_rebalance_date(rebalance_date):
        raise ValueError(f"{rebalance_date} is not the first trading day of a month")
    _require_columns("securities", securities, required)
    _require_columns("daily_bars", daily_bars, BAR_COLUMNS)
    securities = securities.copy()
    for column in ("list_date", "first_event_date"):
        if column in securities.columns:
            securities[column] = _as_dates(securities[column])
    daily_bars = daily_bars.assign(date=_as_dates(daily_bars["date"]))
    splits = _dated_splits(splits)
    if securities["ticker"].duplicated().any():
        raise ValueError("securities table lists a ticker more than once")
    if daily_bars.duplicated(["ticker", "date"]).any():
        raise ValueError("daily_bars has more than one bar for a ticker on the same day")

    as_of = previous_session(rebalance_date)
    window = sessions_ending(as_of, rules.dollar_volume_sessions)

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

    securities["median_dollar_volume_60"] = (
        securities["ticker"].map(median_dollar_volume).fillna(0.0)
    )
    securities["close"] = securities["ticker"].map(last_close)
    return securities, as_of


def _cheap_steps(candidates: pd.DataFrame, rules: UniverseRules, as_of: date):
    return [
        (
            "not listed on NYSE, Nasdaq or NYSE American",
            candidates["primary_exchange"].isin(rules.exchanges),
        ),
        (
            "not common stock (ETF, fund, ADR, preferred, warrant, unit, ...)",
            candidates["type"] == rules.security_type,
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


def _listing_date(candidates: pd.DataFrame) -> pd.Series:
    """The earlier of the vendor's listing date and the first ticker event
    (either may be missing)."""
    columns = [c for c in ("list_date", "first_event_date") if c in candidates.columns]
    return candidates[columns].apply(pd.to_datetime).min(axis=1)


def _dated_splits(splits: pd.DataFrame) -> pd.DataFrame:
    return splits.assign(execution_date=_as_dates(splits["execution_date"]))


def _as_dates(column: pd.Series) -> pd.Series:
    """Normalise a date column to plain `datetime.date` values, so that
    comparisons with calendar dates can't silently match nothing."""
    return pd.to_datetime(column).dt.date


def _require_columns(name: str, table: pd.DataFrame, columns: list[str]) -> None:
    missing = set(columns) - set(table.columns)
    if missing:
        raise ValueError(f"{name} table is missing columns: {sorted(missing)}")
