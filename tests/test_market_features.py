"""Tests for market-relative features (src/vpa/signal/market.py, Section 5.5).

Expected values worked out by hand; the features go through the
look-ahead check. FAKE data only, no network."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from tests.lookahead import assert_ignores_the_future
from vpa.signal.market import (
    BETA_WINDOW,
    COLUMNS,
    beta_by_session,
    market_features,
    refreshed_weekly,
    rolling_beta,
)

START = date(2025, 1, 6)  # a Monday


def weekdays(count: int, start: date = START) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def daily(closes: list[float], days: list[date] | None = None) -> pd.DataFrame:
    days = days or weekdays(len(closes))
    return pd.DataFrame({"date": days, "close": closes})


def from_returns(returns: list[float], start_price: float = 100.0) -> list[float]:
    closes = [start_price]
    for r in returns:
        closes.append(closes[-1] * (1 + r))
    return closes


# --- the beta fit ------------------------------------------------------------


def test_a_stock_that_moves_twice_the_market_has_beta_two():
    market_returns = [0.01 if n % 2 else -0.005 for n in range(BETA_WINDOW + 5)]
    market = daily(from_returns(market_returns))
    stock = daily(from_returns([2 * r for r in market_returns]))
    betas = beta_by_session(stock, market)
    assert betas.dropna().iloc[-1] == pytest.approx(2.0, rel=1e-6)


def test_a_stock_that_ignores_the_market_has_beta_near_zero():
    market_returns = [0.01 if n % 2 else -0.01 for n in range(BETA_WINDOW + 5)]
    stock_returns = [0.002 if n % 3 else -0.001 for n in range(BETA_WINDOW + 5)]
    betas = beta_by_session(daily(from_returns(stock_returns)), daily(from_returns(market_returns)))
    assert abs(betas.dropna().iloc[-1]) < 0.3


def test_beta_needs_forty_of_the_sixty_returns():
    market_returns = [0.01, -0.01] * 15  # only 30 returns
    betas = beta_by_session(
        daily(from_returns([2 * r for r in market_returns])), daily(from_returns(market_returns))
    )
    assert betas.isna().all()


def test_beta_never_includes_the_session_it_applies_to():
    market_returns = [0.01 if n % 2 else -0.01 for n in range(BETA_WINDOW + 10)]
    stock_returns = [2 * r for r in market_returns]
    market = daily(from_returns(market_returns))
    stock = daily(from_returns(stock_returns))

    # A wild final session cannot change the beta already in force on it.
    wild = stock.copy()
    wild.loc[wild.index[-1], "close"] = 10_000.0
    calm_beta = beta_by_session(stock, market).iloc[-1]
    assert not np.isnan(calm_beta)
    assert calm_beta == pytest.approx(beta_by_session(wild, market).iloc[-1], rel=1e-9)


def test_the_fitted_beta_uses_exactly_the_sixty_returns_before_the_session():
    # Pinned against an independent least-squares fit of the window that
    # should have been used. The weekly hold can mask an off-by-one here,
    # so this checks the fit itself rather than the value in force.
    generator = np.random.default_rng(0)
    market_returns = generator.normal(0, 0.01, BETA_WINDOW + 20)
    stock_returns = 1.7 * market_returns + generator.normal(0, 0.0005, BETA_WINDOW + 20)
    days = weekdays(len(market_returns) + 1)
    market = daily(from_returns(list(market_returns)), days).set_index("date")["close"]
    stock = daily(from_returns(list(stock_returns), 50.0), days).set_index("date")["close"]

    fitted = rolling_beta(stock.pct_change(), market.pct_change())
    position = BETA_WINDOW + 8  # a session with a full window behind it
    window = slice(position - BETA_WINDOW - 1, position - 1)  # returns before it
    expected = np.polyfit(market_returns[window], stock_returns[window], 1)[0]
    assert fitted.iloc[position] == pytest.approx(expected, rel=1e-9)


# --- weekly refresh ----------------------------------------------------------


def test_beta_is_held_for_the_week_and_changes_on_the_first_trading_day():
    days = weekdays(10)  # two full weeks
    betas = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0], index=days)
    held = refreshed_weekly(betas)
    assert list(held[:5]) == [1.0] * 5  # Monday's value all week
    assert list(held[5:]) == [6.0] * 5  # then the next Monday's


def test_a_week_starting_on_a_tuesday_refreshes_on_the_tuesday():
    days = [d for d in weekdays(12) if d != date(2025, 1, 13)]  # holiday Monday
    betas = pd.Series(range(1, len(days) + 1), index=days, dtype=float)
    held = refreshed_weekly(betas)
    assert held[date(2025, 1, 14)] == held[date(2025, 1, 17)]  # Tuesday's value holds


# --- the residual ------------------------------------------------------------


def hourly(closes: list[float], days: list[date], slots: int = 2) -> pd.DataFrame:
    rows = []
    for n, close in enumerate(closes):
        rows.append({"date": days[n // slots], "slot_index": n % slots, "close": close})
    return pd.DataFrame(rows)


def test_a_stock_moving_exactly_with_the_market_has_no_residual():
    days = weekdays(3)
    market_closes = [100.0, 101.0, 102.01, 103.0301, 104.060401, 105.10100501]
    market = hourly(market_closes, days)
    # Exactly half the market's price, so exactly the same returns, and
    # with beta 1 the market explains every bit of the stock's move.
    stock = hourly([c / 2 for c in market_closes], days)
    features = market_features(stock, market, pd.Series(1.0, index=days), np.full(6, 1.0))
    assert features["resid_ret_atr"].iloc[1:].abs().max() < 1e-12


def test_a_stock_moving_against_the_market_shows_a_residual():
    days = weekdays(2)
    market = hourly([100.0, 101.0, 102.0, 103.0], days)  # market rising
    stock = hourly([50.0, 49.0, 48.0, 47.0], days)  # stock falling
    betas = pd.Series(1.0, index=days)
    features = market_features(stock, market, betas, atr=np.full(4, 1.0))
    # Bar 1: stock fell 1.00; the market rose 1%, so 0.50 was expected.
    assert features["resid_ret_atr"].iloc[1] == pytest.approx(-1.0 - 0.5)


def test_the_residual_is_in_atr_units_like_ret_atr():
    days = weekdays(2)
    market = hourly([100.0, 100.0, 100.0, 100.0], days)  # market flat
    stock = hourly([50.0, 54.0, 54.0, 54.0], days)
    features = market_features(stock, market, pd.Series(1.0, index=days), atr=np.full(4, 2.0))
    assert features["resid_ret_atr"].iloc[1] == pytest.approx(4.0 / 2.0)


def test_a_missing_beta_or_market_bar_gives_no_residual():
    days = weekdays(2)
    market = hourly([100.0, 101.0, 102.0, 103.0], days)
    stock = hourly([50.0, 51.0, 52.0, 53.0], days)
    features = market_features(stock, market, pd.Series(np.nan, index=days), atr=np.full(4, 1.0))
    assert features["resid_ret_atr"].isna().all()
    assert features["beta_60"].isna().all()


def test_market_return_of_the_day_is_recorded():
    days = weekdays(2)
    market = hourly([100.0, 100.0, 110.0, 110.0], days)  # +10% on day two
    stock = hourly([50.0, 50.0, 50.0, 50.0], days)
    features = market_features(stock, market, pd.Series(1.0, index=days), atr=np.full(4, 1.0))
    assert features["market_ret_day"].iloc[-1] == pytest.approx(0.1)


def test_sector_columns_are_left_empty_for_now():
    days = weekdays(2)
    market = hourly([100.0, 101.0, 102.0, 103.0], days)
    stock = hourly([50.0, 51.0, 52.0, 53.0], days)
    features = market_features(stock, market, pd.Series(1.0, index=days), atr=np.full(4, 1.0))
    assert features["sector_resid_ret_atr"].isna().all()
    assert features["sector_ret_day"].isna().all()
    assert list(features.columns) == COLUMNS


# --- look-ahead --------------------------------------------------------------


def test_the_residual_never_sees_the_future():
    days = weekdays(30)
    market = daily(from_returns([0.01 if n % 3 else -0.008 for n in range(29)]), days)
    stock_closes = from_returns([0.012 if n % 3 else -0.01 for n in range(29)], 50.0)

    def compute(bars):
        return market_features(
            bars, market, pd.Series(1.1, index=days), atr=np.full(len(bars), 1.0)
        )["resid_ret_atr"].to_numpy()

    assert_ignores_the_future(compute, daily(stock_closes, days), cut_points=[20, 25, 28])


def test_the_rolling_beta_never_sees_the_future():
    days = weekdays(BETA_WINDOW + 10)
    market_returns = [0.01 if n % 2 else -0.006 for n in range(len(days) - 1)]
    market = daily(from_returns(market_returns), days)
    stock = daily(from_returns([1.5 * r for r in market_returns]), days)
    betas = rolling_beta(
        stock.set_index("date")["close"].pct_change(),
        market.set_index("date")["close"].pct_change(),
    )
    # Rewriting the last session cannot change any earlier beta.
    tampered = stock.copy()
    tampered.loc[tampered.index[-1], "close"] *= 5
    after = rolling_beta(
        tampered.set_index("date")["close"].pct_change(),
        market.set_index("date")["close"].pct_change(),
    )
    assert betas.iloc[:-1].equals(after.iloc[:-1])


def test_missing_columns_are_refused():
    with pytest.raises(ValueError, match="missing columns"):
        market_features(pd.DataFrame({"close": [1.0]}), pd.DataFrame(), pd.Series(), np.array([1]))
