"""Tests for bar construction (src/vpa/signal/bars.py) and the derived
bar store (src/vpa/data/bars.py). FAKE minutes only, no network."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from vpa.data.bars import build, build_session, derived_path, read_bars, session_bounds_for
from vpa.data.raw_store import write_part
from vpa.signal.bars import (
    MIN_MINUTES_WITH_TRADES,
    daily_bars,
    hourly_bars,
    is_half_day,
    session_slots,
)

NORMAL = date(2025, 9, 2)  # a full session
HALF = date(2025, 11, 28)  # the day after Thanksgiving: closes 13:00 ET


def minutes_for(
    session: date,
    start: str = "09:30",
    end: str = "15:59",
    price: float = 50.0,
    volume: float = 100,
) -> pd.DataFrame:
    """One FAKE minute bar per minute from `start` to `end` (ET, inclusive)."""
    stamps = pd.date_range(
        f"{session} {start}", f"{session} {end}", freq="min", tz="America/New_York"
    )
    return pd.DataFrame(
        {
            "security_key": "FIGI_FAKEA",
            "requested_ticker": "FAKEA",
            "ticker": "FAKEA",
            "timestamp_utc": stamps.tz_convert("UTC"),
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price + 0.5,
            "volume": volume,
            "transactions": 4,
        }
    )


def bounds(session: date):
    return session_bounds_for(session)


# --- slot structure (Section 4.1) --------------------------------------------


def test_a_normal_session_has_seven_slots_ending_in_a_thirty_minute_stub():
    slots = session_slots(*bounds(NORMAL))
    assert [s.minutes for s in slots] == [60, 60, 60, 60, 60, 60, 30]
    assert [s.start.strftime("%H:%M") for s in slots] == [
        "09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30",
    ]  # fmt: skip


def test_a_half_day_has_three_slots_plus_a_stub():
    session_open, session_close = bounds(HALF)
    assert is_half_day(session_close)
    assert session_close.strftime("%H:%M") == "13:00"
    assert [s.minutes for s in session_slots(session_open, session_close)] == [60, 60, 60, 30]


def test_hourly_bars_record_the_slot_and_half_day_columns():
    bars = hourly_bars(minutes_for(HALF, end="12:59"), *bounds(HALF))
    assert list(bars["slot_index"]) == [0, 1, 2, 3]
    assert list(bars["slot_minutes"]) == [60, 60, 60, 30]
    assert bars["is_half_day"].all()
    assert list(bars["minutes_with_trades"]) == [60, 60, 60, 30]


def test_bars_are_anchored_to_the_open_not_the_clock_hour():
    bars = hourly_bars(minutes_for(NORMAL), *bounds(NORMAL))
    first = bars.iloc[0]
    assert first["timestamp_utc"] == pd.Timestamp("2025-09-02 13:30", tz="UTC")  # 09:30 ET
    assert first["minutes_with_trades"] == 60


# --- what goes into a bar ----------------------------------------------------


def test_pre_market_and_after_hours_minutes_are_excluded():
    minutes = pd.concat(
        [
            minutes_for(NORMAL, start="04:00", end="09:29", price=99),  # pre-market
            minutes_for(NORMAL),
            minutes_for(NORMAL, start="16:00", end="19:59", price=1),  # after hours
        ]
    )
    daily = daily_bars(minutes, *bounds(NORMAL))
    assert daily["minutes_with_trades"].iloc[0] == 390  # 09:30-15:59 only
    assert daily["open"].iloc[0] == 50.0 and daily["high"].iloc[0] == 51.0
    hourly = hourly_bars(minutes, *bounds(NORMAL))
    assert hourly["volume"].sum() == daily["volume"].iloc[0]


def test_open_high_low_close_come_from_the_minutes_in_time_order():
    minutes = pd.concat(
        [
            minutes_for(NORMAL, start="09:30", end="09:30", price=10),
            minutes_for(NORMAL, start="09:31", end="09:31", price=30),
            minutes_for(NORMAL, start="09:32", end="09:32", price=20),
        ]
    ).sample(frac=1, random_state=0)  # order in the table must not matter
    bar = daily_bars(minutes, *bounds(NORMAL)).iloc[0]
    assert (bar["open"], bar["close"]) == (10.0, 20.5)
    assert (bar["high"], bar["low"]) == (31.0, 9.0)
    assert bar["volume"] == 300


# --- data quality (Section 4.3) ----------------------------------------------


def test_a_slot_with_too_few_traded_minutes_is_marked_low_quality():
    thin = minutes_for(NORMAL, start="09:30", end=f"09:3{MIN_MINUTES_WITH_TRADES - 2}")
    bars = hourly_bars(thin, *bounds(NORMAL)).set_index("slot_index")
    assert bars.loc[0, "minutes_with_trades"] == MIN_MINUTES_WITH_TRADES - 1
    assert bars.loc[0, "low_quality"]


def test_a_slot_with_no_trades_is_still_a_bar_with_zero_volume():
    bars = hourly_bars(minutes_for(NORMAL, end="10:29"), *bounds(NORMAL)).set_index("slot_index")
    assert len(bars) == 7
    assert bars.loc[1, "volume"] == 0
    assert bars.loc[1, "minutes_with_trades"] == 0
    assert bars.loc[1, "low_quality"]
    assert pd.isna(bars.loc[1, "close"])  # no trades means no price, never invented


def test_a_security_trading_only_pre_market_gets_empty_regular_hours_bars():
    bars = daily_bars(minutes_for(NORMAL, start="04:00", end="09:29"), *bounds(NORMAL))
    assert len(bars) == 1
    assert bars["volume"].iloc[0] == 0 and bars["low_quality"].iloc[0]


def test_missing_columns_are_refused():
    with pytest.raises(ValueError, match="missing columns"):
        daily_bars(minutes_for(NORMAL).drop(columns="volume"), *bounds(NORMAL))


# --- the derived store -------------------------------------------------------


def store_minutes(tmp_path, session: date, minutes: pd.DataFrame) -> None:
    write_part(
        tmp_path, "minute", f"date={session.isoformat()}", f"run-{session}", minutes,
        {"dataset": "minute", "securities": {"FIGI_FAKEA": {"bars": len(minutes)}}},
    )  # fmt: skip


def test_building_writes_one_file_per_session_and_reads_back(tmp_path):
    store_minutes(tmp_path, NORMAL, minutes_for(NORMAL))
    assert build_session(tmp_path, NORMAL) == (7, 1)
    assert derived_path(tmp_path, "hourly", NORMAL).exists()
    assert len(read_bars(tmp_path, "hourly", [NORMAL])) == 7
    assert len(read_bars(tmp_path, "daily", [NORMAL])) == 1


def test_already_built_sessions_are_skipped_unless_rebuilding(tmp_path):
    store_minutes(tmp_path, NORMAL, minutes_for(NORMAL))
    assert build(tmp_path, [NORMAL]) == 1
    assert build(tmp_path, [NORMAL]) == 0
    assert build(tmp_path, [NORMAL], rebuild=True) == 1


def test_derived_bars_can_be_rebuilt_after_more_minutes_arrive(tmp_path):
    store_minutes(tmp_path, NORMAL, minutes_for(NORMAL, end="10:29"))
    build(tmp_path, [NORMAL])
    assert read_bars(tmp_path, "daily", [NORMAL])["minutes_with_trades"].iloc[0] == 60
    write_part(
        tmp_path, "minute", f"date={NORMAL.isoformat()}", "run-2",
        minutes_for(NORMAL, start="10:30", end="15:59"), {"dataset": "minute", "securities": {}},
    )  # fmt: skip
    build(tmp_path, [NORMAL], rebuild=True)
    assert read_bars(tmp_path, "daily", [NORMAL])["minutes_with_trades"].iloc[0] == 390


def test_stored_prices_are_raw_and_adjusted_only_when_read(tmp_path):
    store_minutes(tmp_path, NORMAL, minutes_for(NORMAL, price=40, volume=50))
    build(tmp_path, [NORMAL])
    write_part(
        tmp_path, "splits", "fetched=2025-09-10", "run-1",
        pd.DataFrame([{"requested_ticker": "FAKEA", "ticker": "FAKEA",
                       "execution_date": "2025-09-08", "split_from": 1, "split_to": 2}]),
        {"dataset": "splits"},
    )  # fmt: skip
    raw = read_bars(tmp_path, "daily", [NORMAL])
    assert raw["close"].iloc[0] == 40.5 and raw["volume"].iloc[0] == 390 * 50

    # Before the split: unchanged. After it: prices halved, volume doubled.
    before = read_bars(tmp_path, "daily", [NORMAL], as_of=date(2025, 9, 5))
    assert before["close"].iloc[0] == 40.5
    after = read_bars(tmp_path, "daily", [NORMAL], as_of=date(2025, 9, 9))
    assert after["close"].iloc[0] == pytest.approx(20.25)
    assert after["volume"].iloc[0] == pytest.approx(390 * 50 * 2)
    # Dollar volume is unchanged by adjustment.
    assert after["close"].iloc[0] * after["volume"].iloc[0] == pytest.approx(
        raw["close"].iloc[0] * raw["volume"].iloc[0]
    )


def test_hourly_bars_are_adjusted_the_same_way(tmp_path):
    store_minutes(tmp_path, NORMAL, minutes_for(NORMAL, price=40))
    build(tmp_path, [NORMAL])
    write_part(
        tmp_path, "splits", "fetched=2025-09-10", "run-1",
        pd.DataFrame([{"requested_ticker": "FAKEA", "ticker": "FAKEA",
                       "execution_date": "2025-09-08", "split_from": 1, "split_to": 2}]),
        {"dataset": "splits"},
    )  # fmt: skip
    hourly = read_bars(tmp_path, "hourly", [NORMAL], as_of=date(2025, 9, 9))
    assert hourly["close"].iloc[0] == pytest.approx(20.25)
    assert list(hourly["slot_minutes"]) == [60, 60, 60, 60, 60, 60, 30]


def test_a_session_with_no_minute_data_is_refused(tmp_path):
    with pytest.raises(ValueError, match="No raw minute data"):
        build_session(tmp_path, NORMAL)
