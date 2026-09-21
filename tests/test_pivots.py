"""Tests for swing pivots and volume-at-price (src/vpa/signal/pivots.py,
Section 5.6 part 2). Expected values worked out by hand; the features go
through the look-ahead check. FAKE data only, no network."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from tests.lookahead import assert_ignores_the_future
from vpa.signal.pivots import (
    COLUMNS,
    HISTOGRAM_BINS,
    HISTOGRAM_SESSIONS,
    confirmed_pivots,
    pivot_and_node_features,
    pivots_by_session,
    volume_nodes,
)

START = date(2025, 1, 6)


def weekdays(count: int, start: date = START) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def daily_from(prices: list[float], days=None) -> pd.DataFrame:
    """Daily bars whose high and low sit half a point either side."""
    days = days or weekdays(len(prices))
    return pd.DataFrame(
        {
            "date": days,
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "close": prices,
        }
    )


def flat_atr(count: int, value: float = 1.0) -> np.ndarray:
    return np.full(count, value)


# --- when an extreme becomes a pivot -----------------------------------------


def test_a_high_is_not_a_pivot_until_price_falls_far_enough():
    # High of 20.5. ATR 1, so the low must reach 19.0 to confirm.
    prices = [10.0, 15.0, 20.0, 19.8, 18.0]
    pivots = confirmed_pivots(daily_from(prices), flat_atr(len(prices)))
    assert np.isnan(pivots["pivot_high"].iloc[3])  # low 19.3: a 1.2 fall, not enough
    assert pivots["pivot_high"].iloc[4] == 20.5  # low 17.5: a 3.0 fall, confirmed


def test_a_counter_move_of_exactly_the_threshold_confirms():
    # High 20.5, ATR 1.0, low exactly 19.0: a fall of exactly 1.5.
    prices = [10.0, 20.0, 19.5]
    pivots = confirmed_pivots(daily_from(prices), flat_atr(3))
    assert pivots["pivot_high"].iloc[2] == 20.5


def test_the_pivot_is_the_extreme_not_the_bar_that_confirms_it():
    prices = [10.0, 20.0, 19.0, 18.0]
    pivots = confirmed_pivots(daily_from(prices), flat_atr(4))
    assert pivots["pivot_high"].iloc[3] == 20.5  # the high itself
    assert pivots["pivot_high_at"].iloc[3] == 1  # the session that made it


def test_a_bigger_atr_needs_a_bigger_counter_move():
    prices = [10.0, 20.0, 18.5, 18.0]
    quiet = confirmed_pivots(daily_from(prices), flat_atr(4, 1.0))
    wild = confirmed_pivots(daily_from(prices), flat_atr(4, 10.0))
    assert quiet["pivot_high"].iloc[-1] == 20.5  # 1.5 x 1.0 is easily cleared
    assert np.isnan(wild["pivot_high"].iloc[-1])  # 1.5 x 10 is not


def test_lows_become_pivots_the_same_way():
    prices = [20.0, 10.0, 11.0, 12.0]
    pivots = confirmed_pivots(daily_from(prices), flat_atr(4))
    assert pivots["pivot_low"].iloc[-1] == 9.5
    assert pivots["pivot_low_at"].iloc[-1] == 1


def test_a_new_extreme_replaces_the_one_being_watched():
    # Runs to 20, pulls back a little, then runs to 30 before falling.
    prices = [10.0, 20.0, 19.5, 30.0, 26.0]
    pivots = confirmed_pivots(daily_from(prices), flat_atr(5))
    assert pivots["pivot_high"].iloc[-1] == 30.5  # not 20.5


def test_successive_swings_are_all_recorded():
    prices = [10.0, 20.0, 12.0, 25.0, 15.0]
    pivots = confirmed_pivots(daily_from(prices), flat_atr(5))
    assert pivots["pivot_high"].iloc[2] == 20.5
    assert pivots["pivot_low"].iloc[3] == 11.5
    assert pivots["pivot_high"].iloc[4] == 25.5


def test_a_session_only_sees_pivots_confirmed_before_it():
    prices = [10.0, 20.0, 18.0, 17.0]
    known_now = confirmed_pivots(daily_from(prices), flat_atr(4))
    trailing = pivots_by_session(daily_from(prices), flat_atr(4))
    assert known_now["pivot_high"].iloc[2] == 20.5  # confirmed during session 2
    assert np.isnan(trailing["pivot_high"].iloc[2])  # not usable until session 3
    assert trailing["pivot_high"].iloc[3] == 20.5


# --- volume at price ---------------------------------------------------------


def hourly_for(days: list[date], closes_by_session, volumes_by_session) -> pd.DataFrame:
    rows = []
    for day, closes, volumes in zip(days, closes_by_session, volumes_by_session, strict=True):
        for slot, (close, volume) in enumerate(zip(closes, volumes, strict=True)):
            rows.append(
                {
                    "date": day,
                    "slot_index": slot,
                    "close": close,
                    "high": close + 0.1,
                    "low": close - 0.1,
                    "volume": volume,
                }
            )
    return pd.DataFrame(rows)


def test_the_node_is_the_price_where_most_volume_traded():
    days = weekdays(HISTOGRAM_SESSIONS + 1)
    # Every session trades twice at 10 and once at 50, with the heavy
    # volume at 10.
    closes = [[10.0, 10.0, 50.0]] * len(days)
    volumes = [[1000.0, 1000.0, 1.0]] * len(days)
    nodes = volume_nodes(hourly_for(days, closes, volumes), days)
    assert nodes.iloc[:HISTOGRAM_SESSIONS].isna().all()  # needs 60 sessions
    node = nodes.iloc[HISTOGRAM_SESSIONS]
    assert 9.5 < node < 11.0  # the busy bin, not the midpoint of 10 and 50


def test_the_node_follows_the_volume_not_the_price_range():
    days = weekdays(HISTOGRAM_SESSIONS + 1)
    closes = [[10.0, 30.0, 50.0]] * len(days)
    volumes = [[1.0, 5000.0, 1.0]] * len(days)  # everything trades at 30
    nodes = volume_nodes(hourly_for(days, closes, volumes), days)
    assert 29.0 < nodes.iloc[HISTOGRAM_SESSIONS] < 31.0


def test_the_node_uses_only_the_trailing_sixty_sessions():
    days = weekdays(HISTOGRAM_SESSIONS + 2)
    closes = [[10.0]] * len(days)
    volumes = [[100.0]] * len(days)
    closes[0] = [90.0]  # an old, heavy session that drops out of the window
    volumes[0] = [1_000_000.0]
    nodes = volume_nodes(hourly_for(days, closes, volumes), days)
    assert nodes.iloc[HISTOGRAM_SESSIONS] > 80.0  # still in the window
    assert nodes.iloc[HISTOGRAM_SESSIONS + 1] < 20.0  # now dropped out


def test_the_histogram_has_the_specified_number_of_bins():
    assert HISTOGRAM_BINS == 50
    assert HISTOGRAM_SESSIONS == 60


# --- distances ---------------------------------------------------------------


def test_distances_are_signed_and_in_atr_units():
    days = weekdays(4)
    daily = daily_from([10.0, 20.0, 18.0, 17.0], days)
    pivots = pivots_by_session(daily, flat_atr(4, 2.0))
    bars = pd.DataFrame({"date": [days[3]], "close": [16.5]})
    features = pivot_and_node_features(
        bars, pivots, pd.Series(dtype=float), pd.Series(2.0, index=pd.Index(days, name="date"))
    )
    # Pivot high 20.5, close 16.5, ATR 2 -> -2.0. No node yet.
    assert features["dist_nearest_swing_pivot"].iloc[0] == pytest.approx(-2.0)
    assert np.isnan(features["dist_nearest_hvn"].iloc[0])


def test_the_nearer_of_the_two_pivots_wins():
    days = weekdays(6)
    daily = daily_from([10.0, 20.0, 12.0, 13.0, 14.0, 15.0], days)
    pivots = pivots_by_session(daily, flat_atr(6, 1.0))
    # By the last session both a pivot high (20.5) and low (11.5) exist.
    bars = pd.DataFrame({"date": [days[5]], "close": [13.0]})
    atr = pd.Series(1.0, index=pd.Index(days, name="date"))
    features = pivot_and_node_features(bars, pivots, pd.Series(dtype=float), atr)
    assert features["dist_nearest_swing_pivot"].iloc[0] == pytest.approx(13.0 - 11.5)


# --- look-ahead --------------------------------------------------------------


def test_pivots_never_see_the_future():
    days = weekdays(40)
    prices = [10.0 + 5 * np.sin(n / 3) for n in range(40)]

    def compute(bars):
        bars = bars.assign(date=days[: len(bars)])
        return pivots_by_session(bars, flat_atr(len(bars)))["pivot_high"].to_numpy()

    assert_ignores_the_future(compute, daily_from(prices, days), cut_points=[20, 30, 38])


@pytest.mark.parametrize("column", COLUMNS)
def test_distances_never_see_the_future(column):
    days = weekdays(70)
    prices = [10.0 + 3 * np.sin(n / 4) for n in range(70)]
    hourly = hourly_for(days, [[p] for p in prices], [[100.0 + (n % 5)] for n in range(70)])
    atr = pd.Series(1.0, index=pd.Index(days, name="date"))

    def compute(bars):
        bars = bars.assign(date=days[: len(bars)])
        pivots = pivots_by_session(bars, flat_atr(len(bars)))
        nodes = volume_nodes(hourly[hourly["date"].isin(bars["date"])], list(bars["date"]))
        return pivot_and_node_features(bars, pivots, nodes, atr)[column].to_numpy()

    assert_ignores_the_future(compute, daily_from(prices, days), cut_points=[64, 67, 69])


def test_missing_columns_are_refused():
    with pytest.raises(ValueError, match="missing columns"):
        pivot_and_node_features(
            pd.DataFrame({"close": [1.0]}), pd.DataFrame(), pd.Series(dtype=float), pd.Series()
        )
