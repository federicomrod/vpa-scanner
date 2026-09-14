"""Tests for src/vpa/trading_calendar.py, checked against real,
publicly-known NYSE holiday dates. No network calls."""

from datetime import date

from vpa.trading_calendar import is_trading_day, main

# --- weekends --------------------------------------------------------------


def test_saturday_is_not_a_trading_day():
    assert is_trading_day(date(2026, 9, 19)) is False  # a Saturday


def test_sunday_is_not_a_trading_day():
    assert is_trading_day(date(2026, 9, 20)) is False  # a Sunday


def test_an_ordinary_tuesday_is_a_trading_day():
    assert is_trading_day(date(2026, 9, 15)) is True


# --- known 2024 NYSE holidays ------------------------------------------------


def test_new_years_day_2024_is_not_a_trading_day():
    assert is_trading_day(date(2024, 1, 1)) is False


def test_mlk_day_2024_is_not_a_trading_day():
    assert is_trading_day(date(2024, 1, 15)) is False


def test_presidents_day_2024_is_not_a_trading_day():
    assert is_trading_day(date(2024, 2, 19)) is False


def test_good_friday_2024_is_not_a_trading_day():
    assert is_trading_day(date(2024, 3, 29)) is False


def test_memorial_day_2024_is_not_a_trading_day():
    assert is_trading_day(date(2024, 5, 27)) is False


def test_juneteenth_2024_is_not_a_trading_day():
    assert is_trading_day(date(2024, 6, 19)) is False


def test_independence_day_2024_is_not_a_trading_day():
    assert is_trading_day(date(2024, 7, 4)) is False


def test_labor_day_2024_is_not_a_trading_day():
    assert is_trading_day(date(2024, 9, 2)) is False


def test_thanksgiving_2024_is_not_a_trading_day():
    assert is_trading_day(date(2024, 11, 28)) is False


def test_christmas_2024_is_not_a_trading_day():
    assert is_trading_day(date(2024, 12, 25)) is False


def test_day_after_christmas_2024_is_a_trading_day():
    assert is_trading_day(date(2024, 12, 26)) is True


# --- weekend-observed shift ---------------------------------------------------


def test_new_years_day_2022_falls_on_saturday_observed_previous_friday():
    # New Year's Day 2022 was a Saturday - NYSE observed it on Friday,
    # December 31, 2021, and traded normally on the actual Saturday
    # (which wasn't a trading day anyway, being a weekend).
    assert is_trading_day(date(2021, 12, 31)) is False
    assert is_trading_day(date(2022, 1, 1)) is False  # also a weekend


# --- CLI entry point ---------------------------------------------------------


def test_main_exits_zero_for_a_trading_day():
    assert main(["2026-09-15"]) == 0


def test_main_exits_nonzero_for_a_non_trading_day():
    assert main(["2026-09-19"]) != 0
