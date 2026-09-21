"""Tests for the trailing-window machinery (src/vpa/signal/windows.py)
and for the look-ahead test helper itself (tests/lookahead.py).

Expected values are worked out by hand in the test, never by the code
being tested. FAKE data only, no network.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests.lookahead import assert_ignores_the_future, tamper_with_the_future
from vpa.signal.windows import (
    baseline_valid,
    log_volume,
    measurable,
    trailing_count,
    trailing_percentile,
    trailing_robust_z,
)

ALL_VALID = None


def valid_for(values) -> np.ndarray:
    return np.ones(len(values), dtype=bool)


def percentile(values, window, min_valid=1, valid=None):
    return trailing_percentile(
        values, valid_for(values) if valid is None else valid, window, min_valid
    )


# --- the window itself -------------------------------------------------------


def test_the_window_stops_at_the_previous_bar():
    # Rising values: every bar beats all of its predecessors, so 100.
    result = percentile([1.0, 2.0, 3.0, 4.0], window=3)
    assert np.isnan(result[0])  # nothing before it
    assert list(result[1:]) == [100.0, 100.0, 100.0]


def test_the_window_holds_at_most_the_last_n_bars_and_never_the_current_one():
    # window=2, so bar 3 sees only bars 1 and 2 - not bar 3, not bar 0.
    # Were the current bar included, these counts would be 1, 2, 2, 2.
    counts = trailing_count(valid_for([1, 2, 3, 4]), window=2)
    assert list(counts) == [0, 1, 2, 2]


def test_too_few_observations_gives_null_not_a_number():
    result = percentile([1.0, 2.0, 3.0, 9.0], window=3, min_valid=3)
    assert list(np.isnan(result)) == [True, True, True, False]
    assert result[3] == 100.0  # 3 of 3 below


# --- the tie rule (LEDGER-2, decision 1) -------------------------------------


def test_ties_do_not_count_as_below():
    # Bar 3 equals all three of its predecessors: 0 below out of 3.
    result = percentile([5.0, 5.0, 5.0, 5.0], window=3)
    assert result[3] == 0.0


def test_the_highest_of_sixty_scores_just_under_a_hundred():
    values = list(range(60)) + [1000.0]
    result = percentile(values, window=60, min_valid=40)
    assert result[60] == 100.0  # all 60 below
    # A bar equalling its window's top: 59 of 60 below.
    tying = list(range(60)) + [59.0]
    assert percentile(tying, window=60, min_valid=40)[60] == pytest.approx(59 / 60 * 100)


def test_a_worked_example_by_hand():
    #            bar:   0    1    2    3     4
    values = [10.0, 30.0, 20.0, 40.0, 25.0]
    # bar 4's window is bars 1..3 = [30, 20, 40]; below 25 -> just the 20.
    assert percentile(values, window=3)[4] == pytest.approx(100 / 3)


# --- valid observations (LEDGER-2, decisions 2 and 3) ------------------------


def test_only_valid_observations_count():
    values = [1.0, 99.0, 99.0, 5.0]
    valid = np.array([True, False, False, True])  # the two 99s don't count
    result = trailing_percentile(values, valid, window=3, min_valid=1)
    assert result[3] == 100.0  # only the 1 counts, and 1 < 5
    assert list(trailing_count(valid, window=3)) == [0, 1, 1, 1]


def test_low_quality_and_half_day_bars_are_not_baseline_observations():
    bars = pd.DataFrame(
        {
            "low_quality": [False, True, False, False],
            "is_half_day": [False, False, True, False],
        }
    )
    assert list(baseline_valid(bars)) == [True, False, False, True]


def test_only_half_days_cannot_be_measured():
    bars = pd.DataFrame({"low_quality": [False, True, False], "is_half_day": [False, False, True]})
    # A low-quality bar still gets features (it just can't be a candidate).
    assert list(measurable(bars)) == [True, True, False]


def test_log_volume_keeps_an_hour_with_no_trades_at_zero():
    assert list(log_volume([0, np.e - 1])) == [0.0, pytest.approx(1.0)]


# --- robust z-score ----------------------------------------------------------


def test_robust_z_against_a_hand_computed_baseline():
    # Window [1, 2, 3, 4, 5]: median 3, deviations [2, 1, 0, 1, 2], MAD 1.
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 8.0]
    result = trailing_robust_z(values, valid_for(values), window=5, min_valid=5)
    assert result[5] == pytest.approx((8 - 3) / (1.4826 * 1))


def test_robust_z_is_null_when_the_window_has_no_spread():
    values = [7.0, 7.0, 7.0, 7.0, 7.0, 9.0]
    result = trailing_robust_z(values, valid_for(values), window=5, min_valid=5)
    assert np.isnan(result[5])  # MAD is zero: undefined, not infinite


def test_robust_z_is_null_with_too_few_observations():
    values = [1.0, 2.0, 9.0]
    assert np.isnan(trailing_robust_z(values, valid_for(values), window=5, min_valid=5)).all()


# --- the look-ahead helper itself --------------------------------------------


def example_bars(rows: int = 12) -> pd.DataFrame:
    rise = np.arange(rows, dtype=float)
    return pd.DataFrame(
        {
            "open": 100 + rise,
            "high": 101 + rise,
            "low": 99 + rise,
            "close": 100.5 + rise,
            "volume": 1000 + rise * 10,
            "transactions": 10 + rise.astype(int),
            "minutes_with_trades": np.full(rows, 60),
            "low_quality": np.zeros(rows, dtype=bool),
            "is_half_day": np.zeros(rows, dtype=bool),
        }
    )


def test_the_tamperer_changes_only_the_future():
    bars = example_bars(5)
    tampered = tamper_with_the_future(bars, after=2)
    assert list(tampered["close"][:3]) == list(bars["close"][:3])
    assert list(tampered["close"][3:]) != list(bars["close"][3:])


def test_a_trailing_feature_passes_the_look_ahead_test():
    def trailing(bars):
        return trailing_percentile(
            log_volume(bars["volume"]), baseline_valid(bars), window=3, min_valid=1
        )

    assert_ignores_the_future(trailing, example_bars())


def test_a_window_that_includes_the_current_bar_is_not_what_this_helper_catches():
    # The current bar's own data is not future data, so tampering with
    # later bars cannot reveal an off-by-one window. That half of rule 5
    # is covered by the window tests above - stated here so nobody
    # mistakes this helper for a check on both.
    def includes_current(bars):
        return bars["volume"].rolling(3, min_periods=1).mean().to_numpy()

    assert_ignores_the_future(includes_current, example_bars())


def test_a_feature_reading_one_bar_ahead_is_caught():
    def one_ahead(bars):
        return bars["volume"].shift(-1).rolling(3, min_periods=1).mean().to_numpy()

    with pytest.raises(AssertionError, match="saw the future"):
        assert_ignores_the_future(one_ahead, example_bars())


def test_a_feature_that_uses_later_bars_is_caught():
    def peeking(bars):
        return bars["close"].shift(-1).to_numpy()

    with pytest.raises(AssertionError, match="saw the future"):
        assert_ignores_the_future(peeking, example_bars())


def test_a_feature_using_the_whole_history_at_once_is_caught():
    def whole_history(bars):
        return np.full(len(bars), bars["volume"].mean())

    with pytest.raises(AssertionError, match="saw the future"):
        assert_ignores_the_future(whole_history, example_bars())
