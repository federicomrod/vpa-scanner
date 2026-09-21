"""Tests for price progress (src/vpa/signal/progress.py, Section 5.4).

Expected values worked out by hand; every feature goes through the
look-ahead check. FAKE data only, no network."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests.lookahead import assert_ignores_the_future
from vpa.signal.progress import COLUMNS, MIN_PARTICIPATION, progress_features

SLOTS = 7


def bars(closes: list[float], volumes: list[float] | None = None, **columns) -> pd.DataFrame:
    count = len(closes)
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": volumes if volumes is not None else [1000.0] * count,
            "low_quality": [False] * count,
            "is_half_day": [False] * count,
            "slot_index": [n % SLOTS for n in range(count)],
        }
    )
    for name, values in columns.items():
        frame[name] = values
    return frame


def fixed_atr(count: int, value: float = 2.0) -> np.ndarray:
    """A steady yardstick, so hand-worked sums are easy to read."""
    return np.full(count, value)


# --- returns and net progress ------------------------------------------------


def test_ret_atr_is_the_close_to_close_move_in_atr_units():
    closes = [100.0, 104.0, 103.0]
    features = progress_features(bars(closes), hourly=True, atr=fixed_atr(3))
    assert np.isnan(features["ret_atr"].iloc[0])  # no previous close
    assert features["ret_atr"].iloc[1] == pytest.approx(4.0 / 2.0)
    assert features["ret_atr"].iloc[2] == pytest.approx(-1.0 / 2.0)


def test_progress_measures_the_net_move_across_a_span():
    #        bar:   0      1     2      3      4      5
    closes = [100.0, 105.0, 95.0, 100.0, 101.0, 102.0]
    features = progress_features(bars(closes), hourly=True, atr=fixed_atr(6))
    # The last three bars are 3, 4 and 5; the move across them is measured
    # from where they started, which is bar 2's close of 95.
    assert features["progress_3"].iloc[5] == pytest.approx((102.0 - 95.0) / 2.0)
    # The last five bars start at bar 0's close of 100.
    assert features["progress_5"].iloc[5] == pytest.approx((102.0 - 100.0) / 2.0)


def test_effort_with_no_result_shows_as_progress_near_zero():
    # Price ends exactly where it started ten bars earlier.
    closes = [100.0, 108.0, 95.0, 104.0, 99.0, 103.0, 97.0, 105.0, 96.0, 102.0, 100.0]
    features = progress_features(bars(closes), hourly=True, atr=fixed_atr(len(closes)))
    assert features["progress_10"].iloc[10] == 0.0


def test_progress_is_null_until_the_span_exists():
    # Three bars is not enough for progress_3: the move across bars 0, 1
    # and 2 needs a close from before bar 0, which does not exist.
    three = progress_features(bars([100.0, 101.0, 102.0]), hourly=True, atr=fixed_atr(3))
    assert three["progress_3"].isna().all()
    assert three["progress_5"].isna().all()

    four = progress_features(bars([100.0, 101.0, 102.0, 103.0]), hourly=True, atr=fixed_atr(4))
    assert four["progress_3"].iloc[3] == pytest.approx((103.0 - 100.0) / 2.0)


def test_no_atr_yet_means_no_return():
    closes = [100.0, 101.0, 102.0]
    features = progress_features(bars(closes), hourly=True, atr=np.full(3, np.nan))
    assert features["ret_atr"].isna().all()


# --- cumulative volume -------------------------------------------------------


def busy_history(sessions: int = 45, volume: float = 100.0) -> pd.DataFrame:
    closes = [100.0 + n * 0.01 for n in range(sessions * SLOTS)]
    return bars(closes, [volume] * (sessions * SLOTS))


def test_cumulative_volume_percentile_compares_the_same_stretch():
    history = busy_history()
    history.loc[history.index[-3:], "volume"] = 10_000.0  # a busy last three bars
    features = progress_features(history, hourly=True, atr=fixed_atr(len(history)))
    assert features["cum_vol_pct_3"].iloc[-1] == 100.0


def test_a_stretch_containing_a_dead_hour_is_not_compared():
    history = busy_history()
    history.loc[history.index[-2], "low_quality"] = True
    features = progress_features(history, hourly=True, atr=fixed_atr(len(history)))
    assert np.isnan(features["cum_vol_pct_3"].iloc[-1])  # span includes the dead hour
    assert np.isnan(features["cum_vol_pct_5"].iloc[-1])


def test_half_day_bars_keep_prices_but_lose_the_volume_features():
    history = busy_history()
    history.loc[history.index[-1], "is_half_day"] = True
    features = progress_features(history, hourly=True, atr=fixed_atr(len(history)))
    last = features.iloc[-1]
    assert not np.isnan(last["ret_atr"])  # a price measurement: still there
    assert np.isnan(last["cum_vol_pct_3"]) and np.isnan(last["efficiency"])


# --- efficiency --------------------------------------------------------------


def test_efficiency_is_movement_per_unit_of_participation():
    history = busy_history(volume=100.0)
    # The last bar: twice the usual volume, and a 4-point move (ATR 2).
    history.loc[history.index[-1], "volume"] = 200.0
    history.loc[history.index[-1], "close"] = history["close"].iloc[-2] + 4.0
    features = progress_features(history, hourly=True, atr=fixed_atr(len(history)))
    # |ret_atr| = 2, participation = 200/100 = 2 -> efficiency 1.
    assert features["efficiency"].iloc[-1] == pytest.approx(1.0)


def test_heavy_trading_that_goes_nowhere_scores_near_zero():
    history = busy_history(volume=100.0)
    history.loc[history.index[-1], "volume"] = 5_000.0
    history.loc[history.index[-1], "close"] = history["close"].iloc[-2] + 0.02
    features = progress_features(history, hourly=True, atr=fixed_atr(len(history)))
    assert features["efficiency"].iloc[-1] < 0.001


def test_the_floor_stops_a_dormant_stock_looking_efficient():
    history = busy_history(volume=1000.0)
    # One hundredth of the usual volume, but a full ATR of movement.
    history.loc[history.index[-1], "volume"] = 10.0
    history.loc[history.index[-1], "close"] = history["close"].iloc[-2] + 2.0
    features = progress_features(history, hourly=True, atr=fixed_atr(len(history)))
    # Participation is floored at 0.1, so efficiency is 1 / 0.1 = 10, not 100.
    assert features["efficiency"].iloc[-1] == pytest.approx(1.0 / MIN_PARTICIPATION)


def test_efficiency_needs_a_baseline_before_it_can_be_measured():
    features = progress_features(busy_history(sessions=5), hourly=True, atr=fixed_atr(35))
    assert features["efficiency"].isna().all()


# --- daily -------------------------------------------------------------------


def test_daily_features_need_no_slots():
    daily = bars([100.0 + n for n in range(80)]).drop(columns="slot_index")
    features = progress_features(daily, hourly=False, atr=fixed_atr(80))
    assert features["ret_atr"].iloc[-1] == pytest.approx(0.5)
    assert features["cum_vol_pct_3"].iloc[-1] == 0.0  # every stretch identical: ties


# --- look-ahead --------------------------------------------------------------


@pytest.mark.parametrize("column", COLUMNS)
def test_no_feature_sees_the_future(column):
    history = busy_history(sessions=44)
    history["volume"] = [100.0 + (n % 13) * 10 for n in range(len(history))]
    assert_ignores_the_future(
        lambda b: progress_features(b, hourly=True)[column].to_numpy(),
        history,
        cut_points=[len(history) - 1, len(history) - 8, len(history) - 20],
    )


def test_missing_columns_are_refused():
    with pytest.raises(ValueError, match="missing columns"):
        progress_features(pd.DataFrame({"close": [1.0]}), hourly=False)
