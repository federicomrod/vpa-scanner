"""Tests for volatility and spread (Section 5.2) and candle geometry
(Section 5.3). Expected values worked out by hand; every feature goes
through the look-ahead check. FAKE data only, no network."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests.lookahead import assert_ignores_the_future
from vpa.signal.geometry import COLUMNS as GEOMETRY_COLUMNS
from vpa.signal.geometry import geometry_features
from vpa.signal.volatility import (
    ATR_PERIOD,
    HOURLY_COLUMNS,
    true_range,
    volatility_features,
    wilder_atr_through_previous,
)

SLOTS = 7


def bars_from(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column, default in (("low_quality", False), ("is_half_day", False), ("slot_index", 0)):
        if column not in frame.columns:
            frame[column] = default
    return frame


def candle(o: float, h: float, low: float, c: float, **extra) -> dict:
    return {"open": o, "high": h, "low": low, "close": c, **extra}


def steady_bars(count: int, span: float = 2.0, slots: int = 1) -> pd.DataFrame:
    """Bars that each travel `span`, with no gaps between them."""
    rows = []
    for n in range(count):
        base = 100.0
        rows.append(candle(base, base + span, base, base, slot_index=n % slots, is_half_day=False))
    return bars_from(rows)


# --- true range --------------------------------------------------------------


def test_true_range_is_the_high_low_span_when_there_is_no_gap():
    bars = bars_from([candle(10, 12, 9, 11), candle(11, 13, 10.5, 12)])
    assert list(true_range(bars)) == [3.0, 2.5]


def test_true_range_includes_a_gap_from_the_previous_close():
    # Closed at 11, then opened at 20 and traded 20-21: it travelled from
    # 11 to 21, not just 1 point.
    bars = bars_from([candle(10, 12, 9, 11), candle(20, 21, 20, 20.5)])
    assert list(true_range(bars)) == [3.0, 10.0]


def test_a_gap_down_counts_the_same_way():
    bars = bars_from([candle(10, 12, 9, 11), candle(5, 6, 4, 5)])
    assert list(true_range(bars))[1] == 7.0  # 11 down to 4


def test_the_first_bar_of_a_history_has_no_previous_close():
    assert true_range(bars_from([candle(10, 12, 9, 11)]))[0] == 3.0


def test_a_bar_with_no_trades_has_no_true_range():
    bars = bars_from(
        [candle(10, 12, 9, 11), candle(np.nan, np.nan, np.nan, np.nan), candle(11, 12, 10, 11)]
    )
    ranges = true_range(bars)
    assert np.isnan(ranges[1])
    assert ranges[2] == 2.0  # measured from its own range: no previous close to use


# --- Wilder ATR --------------------------------------------------------------


def test_atr_is_seeded_with_the_average_of_the_first_twenty_ranges():
    ranges = np.array([2.0] * ATR_PERIOD + [10.0])
    atr = wilder_atr_through_previous(ranges)
    assert np.isnan(atr[:ATR_PERIOD]).all()  # nothing to average yet
    assert atr[ATR_PERIOD] == 2.0  # the average of the first twenty


def test_atr_smooths_by_wilders_formula():
    ranges = np.array([2.0] * ATR_PERIOD + [10.0, 10.0])
    atr = wilder_atr_through_previous(ranges)
    # After the 10: (2 x 19 + 10) / 20 = 2.4, seen by the bar after it.
    assert atr[ATR_PERIOD + 1] == pytest.approx((2.0 * 19 + 10.0) / 20)


def test_atr_never_includes_the_bar_it_is_measuring():
    ranges = np.array([2.0] * ATR_PERIOD + [100.0, 2.0])
    atr = wilder_atr_through_previous(ranges)
    assert atr[ATR_PERIOD] == 2.0  # the 100 has not happened yet
    assert atr[ATR_PERIOD + 1] > 2.0  # now it has


def test_a_bar_with_no_range_leaves_the_running_average_alone():
    ranges = np.array([2.0] * ATR_PERIOD + [np.nan, 2.0])
    atr = wilder_atr_through_previous(ranges)
    assert atr[ATR_PERIOD + 1] == 2.0  # unchanged by the gap
    assert atr[ATR_PERIOD + 2 - 1] == 2.0


# --- spread ------------------------------------------------------------------


def test_spread_atr_compares_a_bar_with_the_usual_range():
    bars = steady_bars(ATR_PERIOD)
    wild = bars_from([candle(100, 106, 100, 105)])  # a 6-point bar
    features = volatility_features(pd.concat([bars, wild], ignore_index=True), hourly=False)
    assert features["atr20"].iloc[-1] == 2.0
    assert features["spread_atr"].iloc[-1] == pytest.approx(6.0 / 2.0)


def test_spread_atr_is_null_before_there_is_an_atr():
    features = volatility_features(steady_bars(5), hourly=False)
    assert features["spread_atr"].isna().all()


def test_a_bar_that_did_not_move_has_no_spread():
    flat = bars_from([candle(100, 100, 100, 100)] * (ATR_PERIOD + 1))
    features = volatility_features(flat, hourly=False)
    assert features["spread_atr"].isna().all()  # ATR is zero: undefined, not infinite


def test_spread_percentile_is_measured_within_the_same_slot():
    rows = []
    for _session in range(41):
        for slot in range(SLOTS):
            span = 1.0 if slot else 5.0  # slot 0 always travels further
            rows.append(candle(100, 100 + span, 100, 100.5, slot_index=slot))
    rows.append(candle(100, 103, 100, 101, slot_index=0))  # a 3-point slot-0 bar
    features = volatility_features(bars_from(rows), hourly=True)
    # 3 is small for slot 0 (whose history is all 5s), so 0 of them are below.
    assert features["spread_pct_slot_60"].iloc[-1] == 0.0


def test_half_day_bars_have_no_spread_percentile_but_keep_their_atr():
    rows = []
    for _session in range(41):
        for slot in range(SLOTS):
            rows.append(candle(100, 102, 100, 101, slot_index=slot))
    rows.append(candle(100, 104, 100, 103, slot_index=0, is_half_day=True))
    features = volatility_features(bars_from(rows), hourly=True)
    assert np.isnan(features["spread_pct_slot_60"].iloc[-1])
    assert features["atr20"].iloc[-1] > 0
    assert features["spread_atr"].iloc[-1] > 0


# --- candle geometry ---------------------------------------------------------


def test_geometry_of_a_bar_with_a_long_upper_wick():
    # Opened 10, ran to 16, closed back at 11. No gap, so TR = 7 (16-9).
    bars = bars_from([candle(10, 16, 9, 11)])
    row = geometry_features(bars).iloc[0]
    assert row["body_frac"] == pytest.approx(1 / 7)
    assert row["upper_wick_frac"] == pytest.approx(5 / 7)  # 16 - max(10, 11)
    assert row["lower_wick_frac"] == pytest.approx(1 / 7)  # min(10, 11) - 9
    assert row["close_loc"] == pytest.approx((11 - 9) / (16 - 9))


def test_the_three_fractions_account_for_the_whole_bar_when_there_is_no_gap():
    bars = bars_from([candle(10, 16, 9, 11), candle(11, 14, 10, 13)])
    features = geometry_features(bars)
    total = features["body_frac"] + features["upper_wick_frac"] + features["lower_wick_frac"]
    assert list(total.round(10)) == [1.0, 1.0]


def test_a_gap_makes_the_fractions_sum_to_less_than_one():
    # The bar's own range is small, but it travelled a long way from the
    # previous close, so the fractions are measured against that.
    bars = bars_from([candle(10, 12, 9, 11), candle(20, 21, 20, 20.5)])
    features = geometry_features(bars)
    total = features.iloc[1][["body_frac", "upper_wick_frac", "lower_wick_frac"]].sum()
    assert total < 0.2


def test_close_loc_marks_where_the_bar_closed():
    at_high = geometry_features(bars_from([candle(10, 12, 10, 12)])).iloc[0]
    at_low = geometry_features(bars_from([candle(12, 12, 10, 10)])).iloc[0]
    assert at_high["close_loc"] == 1.0
    assert at_low["close_loc"] == 0.0


def test_a_bar_that_did_not_move_has_no_shape():
    features = geometry_features(bars_from([candle(10, 10, 10, 10)]))
    assert features.iloc[0].isna().all()


def test_a_bar_with_no_trades_has_no_geometry():
    bars = bars_from([candle(10, 12, 9, 11), candle(np.nan, np.nan, np.nan, np.nan)])
    assert geometry_features(bars).iloc[1].isna().all()


# --- look-ahead --------------------------------------------------------------


def varied_bars(count: int = 40) -> pd.DataFrame:
    rows = []
    for n in range(count):
        base = 100.0 + n
        rows.append(
            candle(base, base + 1 + (n % 4), base - (n % 3), base + 0.5, slot_index=n % SLOTS)
        )
    return bars_from(rows)


@pytest.mark.parametrize("column", HOURLY_COLUMNS)
def test_volatility_features_never_see_the_future(column):
    bars = varied_bars()
    assert_ignores_the_future(
        lambda b: volatility_features(b, hourly=True)[column].to_numpy(),
        bars,
        cut_points=[25, 30, 38],
    )


@pytest.mark.parametrize("column", GEOMETRY_COLUMNS)
def test_geometry_features_never_see_the_future(column):
    assert_ignores_the_future(
        lambda b: geometry_features(b)[column].to_numpy(), varied_bars(12), cut_points=[5, 8, 10]
    )


def test_missing_columns_are_refused():
    with pytest.raises(ValueError, match="missing columns"):
        volatility_features(pd.DataFrame({"high": [1.0], "low": [1.0]}), hourly=False)
    with pytest.raises(ValueError, match="missing columns"):
        geometry_features(pd.DataFrame({"high": [1.0]}))
