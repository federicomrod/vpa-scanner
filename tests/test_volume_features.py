"""Tests for volume normalisation (src/vpa/signal/volume.py, Section 5.1).

Expected values are worked out by hand in the test. Every feature is put
through the look-ahead check. FAKE data only, no network.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tests.lookahead import assert_ignores_the_future
from vpa.signal.volume import (
    DAILY_COLUMNS,
    HOURLY_COLUMNS,
    daily_volume_features,
    hourly_volume_features,
    min_valid,
)

SLOTS = 7


def hourly_bars(volumes_by_session: list[list[float]], half_days: list[int] = ()) -> pd.DataFrame:
    """One security's hourly bars: one list of `SLOTS` volumes per session."""
    rows = []
    for session, volumes in enumerate(volumes_by_session):
        for slot, volume in enumerate(volumes):
            rows.append(
                {
                    "session": session,
                    "slot_index": slot,
                    "volume": volume,
                    "low_quality": volume == 0,
                    "is_half_day": session in half_days,
                }
            )
    return pd.DataFrame(rows)


def flat_sessions(count: int, volume: float = 1000.0) -> list[list[float]]:
    return [[volume + slot for slot in range(SLOTS)] for _ in range(count)]


def daily_bars(volumes: list[float], low_quality: list[bool] | None = None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "volume": volumes,
            "low_quality": low_quality if low_quality is not None else [v == 0 for v in volumes],
        }
    )


# --- how many observations are needed ----------------------------------------


def test_the_forty_of_sixty_rule_and_its_proportion_elsewhere():
    assert min_valid(60) == 40  # as Section 5.1 states
    assert min_valid(20) == 14  # same two-thirds
    assert min_valid(120) == 80


def test_a_stock_with_too_little_history_gets_nulls():
    bars = hourly_bars(flat_sessions(39))  # one session short of 40
    assert hourly_volume_features(bars)["vol_pct_slot_60"].isna().all()


def test_the_feature_appears_as_soon_as_forty_sessions_exist():
    bars = hourly_bars(flat_sessions(41))
    features = hourly_volume_features(bars)
    last_session = features.tail(SLOTS)
    assert last_session["vol_pct_slot_60"].notna().all()


# --- each slot is measured against its own history ---------------------------


def test_slots_are_compared_only_with_the_same_slot():
    # 40 quiet sessions, then a session where only slot 0 is busy.
    sessions = flat_sessions(40, volume=100.0)
    sessions.append([100_000.0] + [100.0 + slot for slot in range(1, SLOTS)])
    features = hourly_volume_features(hourly_bars(sessions))
    final = features.tail(SLOTS).reset_index(drop=True)
    assert final.loc[0, "vol_pct_slot_60"] == 100.0  # slot 0 beat all 40
    # The other slots are unchanged, so they tie their own history: 0.
    assert list(final.loc[1:, "vol_pct_slot_60"]) == [0.0] * (SLOTS - 1)


def test_a_busy_morning_does_not_make_the_afternoon_look_busy():
    sessions = flat_sessions(40, volume=100.0)
    sessions.append([50_000.0, 50_000.0] + [100.0 + slot for slot in range(2, SLOTS)])
    final = hourly_volume_features(hourly_bars(sessions)).tail(SLOTS).reset_index(drop=True)
    assert list(final["vol_pct_slot_60"])[:2] == [100.0, 100.0]
    assert final.loc[6, "vol_pct_slot_60"] == 0.0


# --- half days and low-quality bars (LEDGER-2, decisions 2 and 3) ------------


def test_half_day_bars_get_no_features_at_all():
    sessions = flat_sessions(41)
    features = hourly_volume_features(hourly_bars(sessions, half_days=[40]))
    assert features.tail(SLOTS)[HOURLY_COLUMNS].isna().all().all()


def test_a_half_day_never_enters_another_days_baseline():
    # Session 20 is a half day with an enormous slot-0 volume. If it were
    # counted, the later ordinary session would rank below it. 42 sessions,
    # so that dropping the half day still leaves the 40 observations needed.
    sessions = flat_sessions(42, volume=100.0)
    sessions[20] = [1_000_000.0] + [100.0 + slot for slot in range(1, SLOTS)]
    sessions[41] = [200.0] + [100.0 + slot for slot in range(1, SLOTS)]
    features = hourly_volume_features(hourly_bars(sessions, half_days=[20]))
    # Excluded: 40 observations, all below 200 -> 100. Were the half day
    # counted, its million would sit above and the answer would be 97.6.
    assert features.tail(SLOTS).reset_index(drop=True).loc[0, "vol_pct_slot_60"] == 100.0


def test_dropping_a_half_day_can_take_a_stock_below_the_forty_needed():
    # 41 sessions, one of them a half day: 40 trailing sessions minus the
    # half day is 39 observations, so the feature is null rather than
    # computed from too little.
    features = hourly_volume_features(hourly_bars(flat_sessions(41), half_days=[20]))
    assert features.tail(SLOTS)["vol_pct_slot_60"].isna().all()


def test_an_hour_with_no_trades_never_enters_a_baseline():
    sessions = flat_sessions(41, volume=100.0)
    for session in range(10):  # ten dead slot-0 hours
        sessions[session][0] = 0.0
    features = hourly_volume_features(hourly_bars(sessions))
    # 30 valid observations of slot 0 remain, below the 40 needed.
    assert np.isnan(features.tail(SLOTS).reset_index(drop=True).loc[0, "vol_pct_slot_60"])


# --- the robust z-score ------------------------------------------------------


def test_robust_z_by_hand():
    # 60 sessions of slot-0 volume 999 (log = 6.9078), then one of 8102.
    sessions = [[999.0] + [50.0] * (SLOTS - 1) for _ in range(60)]
    sessions.append([8102.0] + [50.0] * (SLOTS - 1))
    features = hourly_volume_features(hourly_bars(sessions))
    # Every trailing value is identical, so the MAD is zero: undefined.
    assert np.isnan(features.tail(SLOTS).reset_index(drop=True).loc[0, "vol_z_slot_60"])


def test_robust_z_uses_the_median_and_mad_of_the_window():
    # Slot-0 volumes cycle through e^1-1, e^2-1, e^3-1 so that log volume
    # is exactly 1, 2, 3: median 2, deviations 1,0,1 -> MAD 1.
    cycle = [np.e - 1, np.e**2 - 1, np.e**3 - 1]
    sessions = [[cycle[n % 3]] + [50.0] * (SLOTS - 1) for n in range(60)]
    sessions.append([np.e**5 - 1] + [50.0] * (SLOTS - 1))  # log volume 5
    features = hourly_volume_features(hourly_bars(sessions))
    z = features.tail(SLOTS).reset_index(drop=True).loc[0, "vol_z_slot_60"]
    assert z == pytest.approx((5 - 2) / (1.4826 * 1), rel=1e-6)


# --- daily features ----------------------------------------------------------


def test_daily_features_need_their_own_windows():
    features = daily_volume_features(daily_bars([100.0] * 79 + [500.0]))
    assert features["vol_pct_d_120"].isna().all()  # 79 < 80 observations
    assert features["vol_pct_d_20"].iloc[-1] == 100.0  # 20-day window is fine


def test_daily_percentile_by_hand():
    volumes = [float(v) for v in range(1, 121)] + [60.5]
    features = daily_volume_features(daily_bars(volumes))
    # The last bar's window is the 120 before it: 1..120. 60 are below 60.5.
    assert features["vol_pct_d_120"].iloc[-1] == pytest.approx(50.0)


def test_daily_columns_are_all_present():
    features = daily_volume_features(daily_bars([100.0] * 130))
    assert list(features.columns) == DAILY_COLUMNS


# --- look-ahead --------------------------------------------------------------


def rising_hourly() -> pd.DataFrame:
    sessions = [[100.0 * (n + 1) + slot for slot in range(SLOTS)] for n in range(70)]
    return hourly_bars(sessions)


@pytest.mark.parametrize("column", HOURLY_COLUMNS)
def test_hourly_features_never_see_the_future(column):
    bars = rising_hourly()
    cuts = [len(bars) - 1 - SLOTS * n for n in range(4)]  # a few session boundaries
    assert_ignores_the_future(
        lambda b: hourly_volume_features(b)[column].to_numpy(), bars, cut_points=cuts
    )


@pytest.mark.parametrize("column", DAILY_COLUMNS)
def test_daily_features_never_see_the_future(column):
    bars = daily_bars([100.0 + n for n in range(130)])
    assert_ignores_the_future(
        lambda b: daily_volume_features(b)[column].to_numpy(), bars, cut_points=[125, 128]
    )


def test_missing_columns_are_refused():
    with pytest.raises(ValueError, match="missing columns"):
        hourly_volume_features(pd.DataFrame({"volume": [1.0], "low_quality": [False]}))
