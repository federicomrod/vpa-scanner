"""Tests for structure distances (src/vpa/signal/structure.py, Section 5.6).

Expected values worked out by hand; the features go through the
look-ahead check. FAKE data only, no network."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from tests.lookahead import assert_ignores_the_future
from vpa.signal.structure import (
    COLUMNS,
    daily_atr,
    nearest_round_number,
    session_levels,
    structure_features,
)

START = date(2025, 1, 6)  # a Monday


def weekdays(count: int, start: date = START) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def daily_bars(highs: list[float], lows: list[float] | None = None, days=None) -> pd.DataFrame:
    days = days or weekdays(len(highs))
    lows = lows if lows is not None else [h - 2 for h in highs]
    closes = [(h + low) / 2 for h, low in zip(highs, lows, strict=True)]
    return pd.DataFrame(
        {
            "date": days,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000.0] * len(highs),
            "low_quality": [False] * len(highs),
        }
    )


def steady_atr(days: list[date], value: float = 2.0) -> pd.Series:
    return pd.Series(value, index=pd.Index(days, name="date"))


# --- the levels themselves ---------------------------------------------------


def test_prior_day_extremes_are_yesterdays():
    levels = session_levels(daily_bars([10.0, 12.0, 11.0]))
    assert np.isnan(levels["prior_day_high"].iloc[0])  # nothing before it
    assert levels["prior_day_high"].iloc[1] == 10.0
    assert levels["prior_day_low"].iloc[2] == 10.0  # bar 1's low: 12 - 2


def test_the_twenty_day_high_excludes_today():
    highs = [10.0] * 20 + [50.0, 11.0]
    levels = session_levels(daily_bars(highs))
    assert np.isnan(levels["high_20"].iloc[19])  # only 19 sessions behind it
    assert levels["high_20"].iloc[20] == 10.0  # the 50 is today: not counted
    assert levels["high_20"].iloc[21] == 50.0  # now it is yesterday


def test_the_sixty_day_window_needs_sixty_sessions():
    levels = session_levels(daily_bars([10.0] * 61))
    assert levels["high_60"].iloc[:60].isna().all()
    assert levels["high_60"].iloc[60] == 10.0


def test_prior_week_means_the_last_completed_week():
    days = weekdays(10)  # two Monday-to-Friday weeks
    highs = [10.0, 11.0, 12.0, 11.5, 11.0] + [20.0, 21.0, 22.0, 21.5, 21.0]
    levels = session_levels(daily_bars(highs, days=days))
    assert levels["prior_week_high"].iloc[:5].isna().all()  # no week before the first
    assert (levels["prior_week_high"].iloc[5:] == 12.0).all()  # week one's high, all week two
    assert (levels["prior_week_low"].iloc[5:] == 8.0).all()  # 10 - 2


def test_prior_month_means_the_last_completed_month():
    january = weekdays(5, date(2025, 1, 27))
    february = weekdays(5, date(2025, 2, 3))
    highs = [10.0, 11.0, 30.0, 11.0, 10.0] + [20.0, 21.0, 22.0, 21.0, 20.0]
    levels = session_levels(daily_bars(highs, days=january + february))
    assert levels["prior_month_high"].iloc[:5].isna().all()
    assert (levels["prior_month_high"].iloc[5:] == 30.0).all()


# --- distances ---------------------------------------------------------------


def test_distance_is_signed_and_measured_in_atr():
    bars = daily_bars([10.0] * 21 + [20.0])
    levels = session_levels(bars)
    features = structure_features(bars, levels, steady_atr(list(bars["date"]), 2.0))
    # Last bar closes at 19; the 20-day high behind it is 10, ATR 2.
    assert features["dist_high_20"].iloc[-1] == pytest.approx((19.0 - 10.0) / 2.0)
    # Below a level gives a negative distance.
    assert features["dist_prior_day_high"].iloc[1] < 0


def test_distance_to_the_nearest_round_number():
    assert list(nearest_round_number(np.array([10.2, 10.3, 10.75, 9.9]))) == [
        10.0,
        10.5,
        11.0,
        10.0,
    ]
    bars = daily_bars([10.4], [10.4])  # closes at 10.40
    features = structure_features(bars, session_levels(bars), steady_atr(list(bars["date"]), 0.2))
    # The nearest round number to 10.40 is 10.50, which is 0.10 above it,
    # so the signed distance is -0.10, and the ATR of 0.20 halves it.
    assert features["dist_round_number"].iloc[0] == pytest.approx(-0.5)


def test_hourly_bars_use_their_sessions_daily_atr_and_levels():
    days = weekdays(3)
    daily = daily_bars([10.0, 12.0, 11.0], days=days)
    levels = session_levels(daily)
    hourly = pd.DataFrame(
        {
            "date": [days[2], days[2]],
            "slot_index": [0, 1],
            "close": [13.0, 14.0],
        }
    )
    features = structure_features(hourly, levels, steady_atr(days, 2.0))
    # Both hours measure against the same session's levels: prior day high 12.
    assert features["dist_prior_day_high"].iloc[0] == pytest.approx((13.0 - 12.0) / 2.0)
    assert features["dist_prior_day_high"].iloc[1] == pytest.approx((14.0 - 12.0) / 2.0)


def test_no_atr_yet_means_no_distances():
    bars = daily_bars([10.0, 11.0, 12.0])
    features = structure_features(
        bars, session_levels(bars), pd.Series(np.nan, index=pd.Index(bars["date"], name="date"))
    )
    assert features.isna().all().all()


def test_daily_atr_is_through_the_previous_session():
    bars = daily_bars([10.0] * 21 + [100.0])
    atr = daily_atr(bars)
    assert np.isnan(atr.iloc[19])
    # The 100-high session cannot be in the ATR the same session uses.
    assert atr.iloc[21] == atr.iloc[20]


def test_all_columns_are_present():
    bars = daily_bars([10.0] * 61)
    features = structure_features(bars, session_levels(bars), steady_atr(list(bars["date"])))
    assert list(features.columns) == COLUMNS


# --- look-ahead --------------------------------------------------------------


@pytest.mark.parametrize("column", COLUMNS)
def test_no_distance_sees_the_future(column):
    days = weekdays(70)
    highs = [10.0 + (n % 7) for n in range(70)]

    def compute(bars):
        bars = bars.assign(date=days[: len(bars)])
        levels = session_levels(bars)
        return structure_features(bars, levels, steady_atr(days, 2.0))[column].to_numpy()

    assert_ignores_the_future(compute, daily_bars(highs, days=days), cut_points=[62, 66, 68])


def test_missing_columns_are_refused():
    with pytest.raises(ValueError, match="missing columns"):
        session_levels(pd.DataFrame({"date": [START]}))
    with pytest.raises(ValueError, match="missing columns"):
        structure_features(pd.DataFrame({"close": [1.0]}), pd.DataFrame(), pd.Series())
