"""Tests for the NYSE calendar wrapper (src/vpa/data/calendar.py) and split
adjustment (src/vpa/signal/adjust.py). FAKE data only, no network calls."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from vpa.data.calendar import is_session, previous_session, sessions_between, sessions_ending
from vpa.signal.adjust import split_adjust_daily

# --- calendar ---------------------------------------------------------------


def test_one_off_closure_is_known():
    # National day of mourning for President Carter - not a regular holiday.
    assert not is_session(date(2025, 1, 9))
    assert sessions_between(date(2025, 1, 8), date(2025, 1, 10)) == [
        date(2025, 1, 8),
        date(2025, 1, 10),
    ]


def test_previous_session_skips_weekends_and_holidays():
    assert previous_session(date(2025, 9, 2)) == date(2025, 8, 29)  # over Labor Day
    assert previous_session(date(2025, 9, 1)) == date(2025, 8, 29)  # from a holiday
    assert previous_session(date(2025, 8, 29)) == date(2025, 8, 28)


def test_sessions_ending_includes_the_end_day():
    days = sessions_ending(date(2025, 8, 29), 3)
    assert days == [date(2025, 8, 27), date(2025, 8, 28), date(2025, 8, 29)]


def test_sessions_ending_refuses_a_non_trading_day():
    with pytest.raises(ValueError):
        sessions_ending(date(2025, 9, 1), 3)


# --- split adjustment -------------------------------------------------------


def fake_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["FAKEX", "FAKEX", "FAKEY"],
            "date": [date(2025, 8, 14), date(2025, 8, 15), date(2025, 8, 14)],
            "open": [39.0, 19.5, 10.0],
            "high": [41.0, 20.5, 10.0],
            "low": [38.0, 19.0, 10.0],
            "close": [40.0, 20.0, 10.0],
            "volume": [500.0, 1000.0, 100.0],
        }
    )


TWO_FOR_ONE = pd.DataFrame(
    [{"ticker": "FAKEX", "execution_date": date(2025, 8, 15), "split_from": 1, "split_to": 2}]
)


def test_bars_before_a_split_are_scaled_and_dollar_volume_is_unchanged():
    raw = fake_bars()
    adjusted = split_adjust_daily(raw, TWO_FOR_ONE, as_of=date(2025, 8, 15))
    assert adjusted.loc[0, ["open", "high", "low", "close"]].tolist() == [19.5, 20.5, 19.0, 20.0]
    assert adjusted.loc[0, "volume"] == 1000
    assert (adjusted["close"] * adjusted["volume"]).tolist() == (
        raw["close"] * raw["volume"]
    ).tolist()


def test_the_split_day_itself_and_other_tickers_are_untouched():
    adjusted = split_adjust_daily(fake_bars(), TWO_FOR_ONE, as_of=date(2025, 8, 15))
    assert adjusted.loc[1:].equals(fake_bars().loc[1:])


def test_a_split_after_the_as_of_date_is_not_applied():
    adjusted = split_adjust_daily(fake_bars(), TWO_FOR_ONE, as_of=date(2025, 8, 14))
    assert adjusted.equals(fake_bars())


def test_raw_input_is_never_modified():
    raw = fake_bars()
    split_adjust_daily(raw, TWO_FOR_ONE, as_of=date(2025, 8, 15))
    assert raw.equals(fake_bars())


def test_consecutive_splits_multiply():
    splits = pd.concat(
        [
            TWO_FOR_ONE,
            pd.DataFrame(
                [
                    {
                        "ticker": "FAKEX",
                        "execution_date": date(2025, 8, 20),
                        "split_from": 1,
                        "split_to": 3,
                    }
                ]
            ),
        ]
    )
    adjusted = split_adjust_daily(fake_bars(), splits, as_of=date(2025, 8, 20))
    assert adjusted.loc[0, "close"] == pytest.approx(40 / 6)
    assert adjusted.loc[1, "close"] == pytest.approx(20 / 3)


def test_invalid_split_ratio_is_refused():
    bad = TWO_FOR_ONE.assign(split_to=0)
    with pytest.raises(ValueError, match="non-positive"):
        split_adjust_daily(fake_bars(), bad, as_of=date(2025, 8, 15))
