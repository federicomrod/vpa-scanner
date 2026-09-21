"""Tests for the nearest-level features (Section 5.6 part 3) and the
sequence features (Section 5.7).

Expected values worked out by hand; the features go through the
look-ahead check. FAKE data only, no network."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from tests.lookahead import assert_ignores_the_future
from vpa.signal.levels import (
    ANCHORS,
    anchor_prices,
    anchor_set_positions,
    nearest_level_features,
    touch_counts,
)
from vpa.signal.pivots import pivots_by_session
from vpa.signal.sequence import (
    COLUMNS as SEQUENCE_COLUMNS,
)
from vpa.signal.sequence import (
    HIGH_VOLUME_PERCENTILE,
    LONG_UPPER_WICK,
    NARROW_SPREAD_ATR,
    NEW_EXTREME_WINDOW,
    REPEAT_WINDOW,
    sequence_features,
)
from vpa.signal.structure import session_levels

START = date(2025, 1, 6)


def weekdays(count: int, start: date = START) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def daily_from(highs: list[float], lows: list[float] | None = None, days=None) -> pd.DataFrame:
    days = days or weekdays(len(highs))
    lows = lows if lows is not None else [h - 2.0 for h in highs]
    return pd.DataFrame(
        {
            "date": days,
            "open": [(h + low) / 2 for h, low in zip(highs, lows, strict=True)],
            "high": highs,
            "low": lows,
            "close": [(h + low) / 2 for h, low in zip(highs, lows, strict=True)],
            "volume": [1000.0] * len(highs),
            "low_quality": [False] * len(highs),
        }
    )


def parts_for(daily: pd.DataFrame):
    """Everything the nearest-level features need, from daily bars."""
    levels = session_levels(daily)
    pivots = pivots_by_session(daily, np.full(len(daily), 1.0))
    prices = anchor_prices(levels, pivots)
    positions = anchor_set_positions(daily, levels, pivots)
    counts = touch_counts(daily, prices)
    session_position = pd.Series(range(len(daily)), index=pd.Index(daily["date"], name="date"))
    return prices, positions, counts, session_position


# --- which levels are eligible (LEDGER-2, decision 7) ------------------------


def test_only_the_frozen_pattern_anchors_are_eligible():
    assert set(ANCHORS) == {
        "high_20",
        "low_20",
        "prior_week_high",
        "prior_week_low",
        "prior_month_high",
        "pivot_high",
    }
    # Deliberately absent: prior-day extremes, 60-day extremes, volume
    # nodes and round numbers - they keep their own distance features.
    assert "prior_day_high" not in ANCHORS and "high_60" not in ANCHORS


# --- age and touches ---------------------------------------------------------


def test_a_level_dates_from_the_session_that_set_it():
    # A single spike on session 5, then quiet. The 20-day high is that
    # spike until it falls out of the window, and it keeps ageing.
    highs = [10.0] * 5 + [30.0] + [10.0] * 25
    daily = daily_from(highs)
    levels = session_levels(daily)
    prices, positions, counts, position = parts_for(daily)
    assert levels["high_20"].iloc[25] == 30.0  # still the spike
    assert positions["high_20"].iloc[25] == 5  # set by session 5
    assert levels["high_20"].iloc[26] == 10.0  # the spike has dropped out


def test_age_grows_while_the_same_level_holds():
    # Closes sit just under a 20-day high that never changes, so that
    # high is the nearest level throughout.
    highs = [30.0] + [20.0] * 30
    lows = [29.0] + [19.0] * 30
    daily = daily_from(highs, lows)
    prices, positions, counts, position = parts_for(daily)
    bars = pd.DataFrame({"date": daily["date"], "close": [29.5] * len(daily)})
    ages = nearest_level_features(bars, prices, positions, counts, position)["level_age_bars"]
    assert ages.iloc[22] == 22 - 0  # the session-0 high, 22 sessions ago
    assert ages.iloc[23] == ages.iloc[22] + 1


def test_touches_count_sessions_whose_range_contains_the_level():
    # The 20-day high is 12; several later sessions trade through 12.
    highs = [12.0] + [12.5] * 25
    lows = [10.0] + [11.0] * 25
    daily = daily_from(highs, lows)
    prices, positions, counts, position = parts_for(daily)
    touched = counts["high_20"].iloc[-1]
    # Sessions 1 to 24 each span 11.0-12.5 and so contain the 12.5 level;
    # session 0 tops out at 12.0, and session 25 itself is not counted.
    assert touched == 24


def test_the_nearest_level_is_chosen_for_each_bar():
    days = weekdays(30)
    daily = daily_from([20.0] * 20 + [30.0] * 10, days=days)
    prices, positions, counts, position = parts_for(daily)
    # Two hours of the same session at very different prices.
    hourly = pd.DataFrame({"date": [days[-1], days[-1]], "close": [18.5, 29.0]})
    features = nearest_level_features(hourly, prices, positions, counts, position)
    assert features["level_age_bars"].notna().all()
    # They need not agree: different closes can be nearest different levels.
    assert len(features) == 2


def test_no_levels_yet_means_no_answer():
    daily = daily_from([10.0, 11.0])
    prices, positions, counts, position = parts_for(daily)
    features = nearest_level_features(daily[["date", "close"]], prices, positions, counts, position)
    assert features.isna().all().all()


# --- sequence: the repeat counts ---------------------------------------------


def bars_for(count: int, highs=None, lows=None, closes=None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "high": highs if highs is not None else [10.0] * count,
            "low": lows if lows is not None else [9.0] * count,
            "close": closes if closes is not None else [9.5] * count,
        }
    )


def test_a_busy_narrow_bar_is_counted_only_in_the_five_bars_after_it():
    count = 10
    volume = np.full(count, 50.0)
    spread = np.full(count, 1.0)
    volume[2] = HIGH_VOLUME_PERCENTILE  # busy
    spread[2] = NARROW_SPREAD_ATR  # and narrow
    features = sequence_features(bars_for(count), volume, spread, np.zeros(count))
    counts = features["repeat_hv_narrow_5"]
    assert counts.iloc[:5].isna().all()  # no full window yet
    assert counts.iloc[5] == 1.0  # bar 2 is among bars 0-4
    assert counts.iloc[7] == 1.0  # still among bars 2-6
    assert counts.iloc[8] == 0.0  # bars 3-7: it has dropped out


def test_the_bar_being_measured_is_not_in_its_own_count():
    count = 8
    volume = np.full(count, HIGH_VOLUME_PERCENTILE)
    spread = np.full(count, NARROW_SPREAD_ATR)
    features = sequence_features(bars_for(count), volume, spread, np.zeros(count))
    # Every bar qualifies, so the count is the window size, never six.
    assert features["repeat_hv_narrow_5"].iloc[7] == REPEAT_WINDOW


def test_a_busy_but_wide_bar_does_not_count():
    count = 8
    volume = np.full(count, HIGH_VOLUME_PERCENTILE)
    spread = np.full(count, NARROW_SPREAD_ATR + 0.01)  # just too wide
    features = sequence_features(bars_for(count), volume, spread, np.zeros(count))
    assert features["repeat_hv_narrow_5"].iloc[-1] == 0.0


def test_upper_rejection_needs_both_volume_and_a_long_wick():
    count = 8
    volume = np.full(count, HIGH_VOLUME_PERCENTILE)
    wick = np.full(count, LONG_UPPER_WICK)
    quiet = np.full(count, HIGH_VOLUME_PERCENTILE - 1)
    features = sequence_features(bars_for(count), volume, np.ones(count), wick)
    assert features["repeat_upper_reject_5"].iloc[-1] == REPEAT_WINDOW
    without_volume = sequence_features(bars_for(count), quiet, np.ones(count), wick)
    assert without_volume["repeat_upper_reject_5"].iloc[-1] == 0.0


def test_counts_are_null_until_a_full_window_exists():
    features = sequence_features(bars_for(4), np.zeros(4), np.zeros(4), np.zeros(4))
    assert features["repeat_hv_narrow_5"].isna().all()


# --- sequence: failed new highs and lows -------------------------------------


def sequence_of(count: int) -> tuple[list[float], list[float], list[float]]:
    return [10.0] * count, [9.0] * count, [9.5] * count


def test_a_failed_new_high_reaches_higher_but_cannot_hold_it():
    count = NEW_EXTREME_WINDOW + 1
    highs, lows, closes = sequence_of(count)
    highs[-1] = 12.0  # beats the previous ten bars' high of 10
    closes[-1] = 9.4  # but closes below their highest close of 9.5
    features = sequence_features(
        bars_for(count, highs, lows, closes), np.zeros(count), np.zeros(count), np.zeros(count)
    )
    assert bool(features["failed_new_high"].iloc[-1]) is True


def test_reaching_higher_and_holding_it_is_not_a_failure():
    count = NEW_EXTREME_WINDOW + 1
    highs, lows, closes = sequence_of(count)
    highs[-1] = 12.0
    closes[-1] = 11.0  # closed above the previous highest close
    features = sequence_features(
        bars_for(count, highs, lows, closes), np.zeros(count), np.zeros(count), np.zeros(count)
    )
    assert bool(features["failed_new_high"].iloc[-1]) is False


def test_the_comparison_uses_the_highest_close_not_the_close_of_the_highest_bar():
    count = NEW_EXTREME_WINDOW + 1
    highs, lows, closes = sequence_of(count)
    highs[3] = 11.0  # the highest bar of the window...
    closes[3] = 9.2  # ...closed weakly
    closes[6] = 10.8  # while another bar closed strongly
    highs[-1] = 12.0
    closes[-1] = 10.0  # above bar 3's close, below bar 6's
    features = sequence_features(
        bars_for(count, highs, lows, closes), np.zeros(count), np.zeros(count), np.zeros(count)
    )
    # Against the highest close (10.8) this is a failure; against the
    # close of the highest bar (9.2) it would not be.
    assert bool(features["failed_new_high"].iloc[-1]) is True


def test_a_failed_new_low_is_the_mirror_image():
    count = NEW_EXTREME_WINDOW + 1
    highs, lows, closes = sequence_of(count)
    lows[-1] = 7.0  # below the previous ten bars' low of 9
    closes[-1] = 9.6  # but closed above their lowest close
    features = sequence_features(
        bars_for(count, highs, lows, closes), np.zeros(count), np.zeros(count), np.zeros(count)
    )
    assert bool(features["failed_new_low"].iloc[-1]) is True


def test_without_ten_bars_behind_it_the_answer_is_unknown_not_false():
    count = 5
    features = sequence_features(bars_for(count), np.zeros(count), np.zeros(count), np.zeros(count))
    assert features["failed_new_high"].isna().all()
    assert features["failed_new_low"].isna().all()


def test_all_sequence_columns_are_present():
    features = sequence_features(bars_for(20), np.zeros(20), np.zeros(20), np.zeros(20))
    assert list(features.columns) == SEQUENCE_COLUMNS


# --- look-ahead --------------------------------------------------------------


@pytest.mark.parametrize("column", SEQUENCE_COLUMNS)
def test_sequence_features_never_see_the_future(column):
    count = 30
    highs = [10.0 + (n % 5) for n in range(count)]
    lows = [h - 2 for h in highs]
    closes = [h - 1 for h in highs]
    volume = np.array([90.0 if n % 3 else 10.0 for n in range(count)])
    spread = np.array([0.5 if n % 2 else 2.0 for n in range(count)])
    wick = np.array([0.6 if n % 4 else 0.1 for n in range(count)])

    def compute(bars):
        rows = len(bars)
        result = sequence_features(bars, volume[:rows], spread[:rows], wick[:rows])[column]
        return result.astype("float64").to_numpy()

    assert_ignores_the_future(
        compute, bars_for(count, highs, lows, closes), cut_points=[20, 25, 28]
    )


def test_nearest_level_features_never_see_the_future():
    days = weekdays(40)
    highs = [10.0 + 3 * np.sin(n / 4) for n in range(40)]

    def compute(bars):
        bars = bars.assign(date=days[: len(bars)])
        prices, positions, counts, position = parts_for(bars)
        return nearest_level_features(bars[["date", "close"]], prices, positions, counts, position)[
            "level_age_bars"
        ].to_numpy()

    assert_ignores_the_future(compute, daily_from(highs, days=days), cut_points=[30, 35, 38])


def test_missing_columns_are_refused():
    with pytest.raises(ValueError, match="missing columns"):
        sequence_features(pd.DataFrame({"high": [1.0]}), np.zeros(1), np.zeros(1), np.zeros(1))
    with pytest.raises(ValueError, match="missing columns"):
        nearest_level_features(
            pd.DataFrame({"close": [1.0]}),
            pd.DataFrame({"date": []}),
            pd.DataFrame(),
            pd.DataFrame(),
            pd.Series(dtype=float),
        )
