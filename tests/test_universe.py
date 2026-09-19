"""Tests for the universe rules (src/vpa/signal/universe.py) and snapshot
files (src/vpa/data/universe.py), using the FAKE stocks in
fixtures/universe/securities.csv. No network calls, no real market data."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from vpa.data.calendar import sessions_ending
from vpa.data.universe import (
    SnapshotExistsError,
    build_snapshot,
    read_snapshot,
    snapshot_in_force,
    snapshot_path,
)
from vpa.signal.universe import UniverseRules, is_rebalance_date, select_universe

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "universe" / "securities.csv"

# Tuesday 2 Sept 2025: first trading day of September (Monday 1st was
# Labor Day). Everything is measured at the close of Friday 29 August.
REBALANCE = date(2025, 9, 2)
AS_OF = date(2025, 8, 29)

NO_SPLITS = pd.DataFrame(columns=["ticker", "execution_date", "split_from", "split_to"])


def load_fixture() -> pd.DataFrame:
    return pd.read_csv(FIXTURE, parse_dates=["list_date"])


def securities_of(fixture: pd.DataFrame) -> pd.DataFrame:
    return fixture[["ticker", "type", "primary_exchange", "market_cap", "list_date"]]


def bars_for(ticker: str, close: float, volume: float, days: int = 80) -> pd.DataFrame:
    """`days` identical daily bars ending on the as-of session."""
    return pd.DataFrame(
        {"ticker": ticker, "date": sessions_ending(AS_OF, days), "close": close, "volume": volume}
    )


def fixture_bars(fixture: pd.DataFrame) -> pd.DataFrame:
    return pd.concat(
        [bars_for(r.ticker, r.close, r.volume) for r in fixture.itertuples()], ignore_index=True
    )


def one_stock(ticker: str = "FAKEX") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": [ticker],
            "type": ["CS"],
            "primary_exchange": ["XNYS"],
            "market_cap": [10e9],
            "list_date": [date(2015, 1, 2)],
        }
    )


def selected_tickers(result) -> list[str]:
    return list(result.selected["ticker"])


# --- the fixture, end to end --------------------------------------------------


def test_fixture_selects_exactly_the_rows_marked_selected():
    fixture = load_fixture()
    result = select_universe(REBALANCE, securities_of(fixture), fixture_bars(fixture), NO_SPLITS)
    expected = set(fixture.loc[fixture["selected"] == "yes", "ticker"])
    assert set(selected_tickers(result)) == expected


def test_selection_is_ranked_by_dollar_volume():
    fixture = load_fixture()
    result = select_universe(REBALANCE, securities_of(fixture), fixture_bars(fixture), NO_SPLITS)
    # 50m, 40m, 21m, 20m, 15m
    assert selected_tickers(result) == ["FAKEA", "FAKEEDGEHI", "FAKEC", "FAKEB", "FAKEEDGELO"]
    assert list(result.selected["rank"]) == [1, 2, 3, 4, 5]
    assert result.selected["median_dollar_volume_60"].iloc[0] == pytest.approx(50_000_000)
    assert (result.selected["as_of_session"] == AS_OF).all()
    assert (result.selected["rebalance_date"] == REBALANCE).all()


def test_only_the_top_n_are_kept():
    fixture = load_fixture()
    result = select_universe(
        REBALANCE,
        securities_of(fixture),
        fixture_bars(fixture),
        NO_SPLITS,
        rules=UniverseRules(top_n=2),
    )
    assert selected_tickers(result) == ["FAKEA", "FAKEEDGEHI"]
    assert result.removed_by_step["outside the top 2 by dollar volume"] == 3


def test_every_excluded_stock_is_accounted_for_by_a_step():
    fixture = load_fixture()
    result = select_universe(REBALANCE, securities_of(fixture), fixture_bars(fixture), NO_SPLITS)
    assert list(result.removed_by_step.values()) == [1, 3, 2, 3, 1, 1, 0]
    assert sum(result.removed_by_step.values()) + len(result.selected) == len(fixture)


def test_ties_are_broken_alphabetically():
    securities = pd.concat([one_stock("FAKEZ"), one_stock("FAKEY")], ignore_index=True)
    bars = pd.concat([bars_for("FAKEZ", 20, 1e6), bars_for("FAKEY", 20, 1e6)])
    result = select_universe(REBALANCE, securities, bars, NO_SPLITS)
    assert selected_tickers(result) == ["FAKEY", "FAKEZ"]


def test_defaults_are_the_concept_v2_section_2_numbers():
    rules = UniverseRules()
    assert rules.exchanges == {"XNYS", "XNAS", "XASE"}
    assert rules.security_type == "CS"
    assert rules.min_market_cap == 2e9
    assert rules.max_market_cap == 50e9
    assert rules.dollar_volume_sessions == 60
    assert rules.min_median_dollar_volume == 15e6
    assert rules.min_close == 10
    assert rules.min_history_sessions == 250
    assert rules.top_n == 400


# --- no look-ahead: nothing from the rebalance date or later is used --------


def test_bars_on_or_after_the_rebalance_date_are_ignored():
    # A thin stock with an enormous bar on the rebalance date itself, and a
    # good stock whose price collapses that day. Neither may affect the result.
    securities = pd.concat([one_stock("FAKETHIN"), one_stock("FAKEGOOD")], ignore_index=True)
    later = [
        {"ticker": "FAKETHIN", "date": REBALANCE, "close": 20, "volume": 1e12},
        {"ticker": "FAKEGOOD", "date": REBALANCE, "close": 1, "volume": 1},
        {"ticker": "FAKEGOOD", "date": date(2025, 9, 3), "close": 1, "volume": 1},
    ]
    bars = pd.concat(
        [bars_for("FAKETHIN", 20, 1000), bars_for("FAKEGOOD", 20, 1e6), pd.DataFrame(later)]
    )
    result = select_universe(REBALANCE, securities, bars, NO_SPLITS)
    assert selected_tickers(result) == ["FAKEGOOD"]
    assert result.selected["close"].iloc[0] == 20


def test_window_is_exactly_the_60_sessions_before_the_rebalance_date():
    # Huge volume on the 61st session back must not count; 30 good days
    # in the 60-day window plus 30 zero days give a median halfway between.
    window = sessions_ending(AS_OF, 60)
    bars = pd.DataFrame(
        {
            "ticker": "FAKEX",
            "date": sessions_ending(AS_OF, 61)[:1] + window[30:],
            "close": 20.0,
            "volume": [1e12] + [2e6] * 30,
        }
    )
    result = select_universe(REBALANCE, one_stock(), bars, NO_SPLITS)
    assert result.selected["median_dollar_volume_60"].iloc[0] == pytest.approx(20_000_000)


def test_days_with_no_bar_count_as_zero_dollar_volume():
    # Trades on only 29 of the 60 days: the median is zero.
    bars = bars_for("FAKEX", 50, 1e6, days=29)
    result = select_universe(REBALANCE, one_stock(), bars, NO_SPLITS)
    assert selected_tickers(result) == []


def test_no_bar_on_the_as_of_session_means_no_close_and_exclusion():
    bars = bars_for("FAKEX", 50, 1e6)
    bars = bars[bars["date"] != AS_OF]
    result = select_universe(REBALANCE, one_stock(), bars, NO_SPLITS)
    assert selected_tickers(result) == []


def test_history_needs_250_sessions_up_to_the_as_of_session():
    first_ok = sessions_ending(AS_OF, 250)[0]
    one_day_late = sessions_ending(AS_OF, 249)[0]
    securities = pd.concat([one_stock("FAKEOK"), one_stock("FAKELATE")], ignore_index=True)
    securities["list_date"] = [first_ok, one_day_late]
    bars = pd.concat([bars_for("FAKEOK", 20, 1e6), bars_for("FAKELATE", 20, 1e6)])
    result = select_universe(REBALANCE, securities, bars, NO_SPLITS)
    assert selected_tickers(result) == ["FAKEOK"]


# --- split adjustment -------------------------------------------------------


def test_a_split_inside_the_window_does_not_distort_dollar_volume_or_price():
    # 2-for-1 split on 15 Aug: $40 x 0.5m before, $20 x 1m after. Adjusted,
    # every day is $20 x 1m = 20m.
    split_day = date(2025, 8, 15)
    bars = bars_for("FAKEX", 20, 1e6)
    before = bars["date"] < split_day
    bars.loc[before, "close"] = 40.0
    bars.loc[before, "volume"] = 5e5
    splits = pd.DataFrame(
        [{"ticker": "FAKEX", "execution_date": split_day, "split_from": 1, "split_to": 2}]
    )
    result = select_universe(REBALANCE, one_stock(), bars, splits)
    assert result.selected["median_dollar_volume_60"].iloc[0] == pytest.approx(20_000_000)
    assert result.selected["close"].iloc[0] == 20


def test_price_floor_uses_splits_in_effect_on_the_rebalance_date():
    # 1-for-10 reverse split taking effect on the rebalance date: the $2
    # raw close is $20 once adjusted, so the stock passes the $10 floor.
    bars = bars_for("FAKEX", 2, 1e7)
    splits = pd.DataFrame(
        [{"ticker": "FAKEX", "execution_date": REBALANCE, "split_from": 10, "split_to": 1}]
    )
    result = select_universe(REBALANCE, one_stock(), bars, splits)
    assert result.selected["close"].iloc[0] == pytest.approx(20)


def test_splits_after_the_rebalance_date_are_not_applied():
    # The same reverse split a week later hasn't happened yet: $2 fails.
    bars = bars_for("FAKEX", 2, 1e7)
    splits = pd.DataFrame(
        [{"ticker": "FAKEX", "execution_date": date(2025, 9, 9), "split_from": 10, "split_to": 1}]
    )
    result = select_universe(REBALANCE, one_stock(), bars, splits)
    assert selected_tickers(result) == []


# --- rebalance dates and bad input ------------------------------------------


def test_rebalance_dates_are_the_first_trading_day_of_each_month():
    assert is_rebalance_date(date(2025, 9, 2))  # after Labor Day
    assert is_rebalance_date(date(2025, 1, 2))  # after New Year's Day
    assert is_rebalance_date(date(2025, 3, 3))  # 1 March was a Saturday
    assert not is_rebalance_date(date(2025, 9, 1))  # Labor Day itself
    assert not is_rebalance_date(date(2025, 9, 3))


def test_a_non_rebalance_date_is_refused():
    with pytest.raises(ValueError, match="not the first trading day"):
        select_universe(date(2025, 9, 3), one_stock(), bars_for("FAKEX", 20, 1e6), NO_SPLITS)


def test_duplicate_tickers_are_refused():
    securities = pd.concat([one_stock(), one_stock()])
    with pytest.raises(ValueError, match="more than once"):
        select_universe(REBALANCE, securities, bars_for("FAKEX", 20, 1e6), NO_SPLITS)


def test_duplicate_bars_are_refused():
    bars = pd.concat([bars_for("FAKEX", 20, 1e6)] * 2)
    with pytest.raises(ValueError, match="more than one bar"):
        select_universe(REBALANCE, one_stock(), bars, NO_SPLITS)


def test_missing_columns_are_refused():
    with pytest.raises(ValueError, match="missing columns"):
        select_universe(
            REBALANCE, one_stock().drop(columns="market_cap"), bars_for("FAKEX", 20, 1e6), NO_SPLITS
        )


def test_dates_stored_as_timestamps_still_match():
    bars = bars_for("FAKEX", 20, 1e6)
    bars["date"] = pd.to_datetime(bars["date"])
    result = select_universe(REBALANCE, one_stock(), bars, NO_SPLITS)
    assert selected_tickers(result) == ["FAKEX"]


# --- snapshot files ---------------------------------------------------------


def build_fixture_snapshot(universe_dir: Path) -> Path:
    fixture = load_fixture()
    return build_snapshot(
        REBALANCE, securities_of(fixture), fixture_bars(fixture), NO_SPLITS, universe_dir
    )


def test_snapshot_is_written_and_reads_back(tmp_path):
    path = build_fixture_snapshot(tmp_path)
    assert path == tmp_path / "2025-09-02.parquet"
    snapshot = read_snapshot(tmp_path, REBALANCE)
    assert list(snapshot["ticker"])[:2] == ["FAKEA", "FAKEEDGEHI"]
    assert snapshot["rebalance_date"].iloc[0] == REBALANCE


def test_snapshot_is_read_only_and_never_overwritten(tmp_path):
    path = build_fixture_snapshot(tmp_path)
    assert not os.access(path, os.W_OK)
    before = path.read_bytes()
    with pytest.raises(SnapshotExistsError):
        build_fixture_snapshot(tmp_path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]  # no leftover temporary file


def test_snapshot_in_force_is_the_latest_on_or_before_the_day(tmp_path):
    for day in ("2025-08-01", "2025-09-02"):
        (tmp_path / f"{day}.parquet").touch()
    assert snapshot_in_force(tmp_path, date(2025, 9, 1)) == date(2025, 8, 1)
    assert snapshot_in_force(tmp_path, date(2025, 9, 2)) == date(2025, 9, 2)
    assert snapshot_in_force(tmp_path, date(2025, 12, 31)) == date(2025, 9, 2)
    with pytest.raises(FileNotFoundError):
        snapshot_in_force(tmp_path, date(2025, 7, 31))


def test_snapshot_path_format(tmp_path):
    assert snapshot_path(tmp_path, date(2025, 9, 2)) == tmp_path / "2025-09-02.parquet"
