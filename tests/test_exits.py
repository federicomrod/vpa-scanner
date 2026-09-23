"""Tests for the reference exit rule (src/vpa/signal/exits.py).

Hand-built bars, so every level, fill and boundary is exact rather than
approximately right. No network, no real data.

Section 10 is frozen permanently, so these tests pin the numbers as much
as the behaviour: if one of them ever needs changing, that is a Class 2
change with a ledger entry, not an edit.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from vpa.signal.exits import (
    DELISTED,
    FORWARD_HORIZONS,
    LONG,
    NO_ENTRY,
    SHORT,
    STOP_ATR,
    STOPPED,
    TARGET,
    TARGET_ATR,
    TIMEOUT,
    TIMEOUT_SESSIONS,
    forward_moves,
    levels,
    walk,
)

ATR = 2.0
ENTRY = 100.0
SESSION = date(2025, 3, 3)


def session(n: int) -> date:
    """The nth weekday from the entry session."""
    day = SESSION
    while n:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def bars(*rows: tuple[int, float, float, float, float]) -> pd.DataFrame:
    """(session offset, open, high, low, close), one hourly bar each."""
    return pd.DataFrame(
        [
            {
                "date": session(offset),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "timestamp_utc": pd.Timestamp(f"{session(offset)} 14:30", tz="UTC")
                + pd.Timedelta(n, "h"),
            }
            for n, (offset, open_, high, low, close) in enumerate(rows)
        ]
    )


def flat_days(count: int, price: float = ENTRY) -> pd.DataFrame:
    """`count` sessions where nothing happens at all."""
    return bars(*[(n, price, price + 0.1, price - 0.1, price) for n in range(count)])


# --- the levels themselves ---------------------------------------------------


def test_the_levels_are_the_ones_section_10_names():
    assert (STOP_ATR, TARGET_ATR, TIMEOUT_SESSIONS) == (1.5, 3.0, 10)
    assert FORWARD_HORIZONS == (1, 3, 5, 10)


def test_a_long_risks_below_and_aims_above():
    stop, target = levels(ENTRY, ATR, LONG)
    assert (stop, target) == (97.0, 106.0)


def test_a_short_is_the_mirror_image():
    stop, target = levels(ENTRY, ATR, SHORT)
    assert (stop, target) == (103.0, 94.0)


# --- reaching a level --------------------------------------------------------


def test_a_long_that_reaches_its_target():
    outcome = walk(bars((0, ENTRY, 106.5, 99.5, 106.2)), ENTRY, ATR, LONG)
    assert outcome.reason == TARGET
    assert outcome.exit == 106.0
    assert outcome.move_atr == pytest.approx(3.0)
    assert outcome.r_multiple == pytest.approx(2.0)


def test_a_long_that_is_stopped_out():
    outcome = walk(bars((0, ENTRY, 100.5, 96.5, 97.2)), ENTRY, ATR, LONG)
    assert outcome.reason == STOPPED
    assert outcome.exit == 97.0
    assert outcome.move_atr == pytest.approx(-1.5)
    assert outcome.r_multiple == pytest.approx(-1.0)


def test_a_short_reaching_its_target_is_a_fall():
    outcome = walk(bars((0, ENTRY, 100.5, 93.5, 94.1)), ENTRY, ATR, SHORT)
    assert outcome.reason == TARGET
    assert outcome.exit == 94.0
    assert outcome.r_multiple == pytest.approx(2.0)


def test_a_short_is_stopped_by_a_rise():
    outcome = walk(bars((0, ENTRY, 103.5, 99.5, 103.2)), ENTRY, ATR, SHORT)
    assert outcome.reason == STOPPED
    assert outcome.exit == 103.0
    assert outcome.r_multiple == pytest.approx(-1.0)


def test_the_target_is_exactly_two_r():
    # True by construction - 3.0 ATR of reward against 1.5 ATR of risk -
    # and worth pinning, because it is what makes R-multiples comparable
    # across stocks of different volatility.
    assert TARGET_ATR / STOP_ATR == 2.0


# --- what the specification leaves open --------------------------------------


def test_a_bar_that_reaches_both_levels_is_read_as_a_stop():
    # The pessimistic reading, applied identically every time. An
    # optimistic one would flatter every signal and every control alike,
    # but it would flatter the wild ones most (LEDGER-6, reading 2).
    outcome = walk(bars((0, ENTRY, 106.5, 96.5, 101.0)), ENTRY, ATR, LONG)
    assert outcome.reason == STOPPED
    assert outcome.r_multiple == pytest.approx(-1.0)


def test_separate_hours_resolve_what_a_single_day_could_not():
    # The same day, but the target came first and the fall after it. On
    # daily bars this is indistinguishable from the test above; on hourly
    # bars it is plainly a target.
    outcome = walk(
        bars((0, ENTRY, 106.5, 99.9, 106.2), (0, 106.2, 106.5, 96.5, 97.0)), ENTRY, ATR, LONG
    )
    assert outcome.reason == TARGET


def test_a_gap_through_the_stop_fills_at_the_open_not_the_level():
    # Price opened at 95, below the 97 stop. You could not have sold at
    # 97; the level was gone before trading began.
    outcome = walk(
        bars((0, ENTRY, 100.2, 99.8, 100.0), (1, 95.0, 95.5, 94.0, 94.5)), ENTRY, ATR, LONG
    )
    assert outcome.reason == STOPPED
    assert outcome.exit == 95.0
    assert outcome.r_multiple == pytest.approx(-5.0 / 3.0)


def test_a_gap_through_the_target_fills_better_for_the_same_reason():
    outcome = walk(
        bars((0, ENTRY, 100.2, 99.8, 100.0), (1, 108.0, 108.5, 107.5, 108.2)), ENTRY, ATR, LONG
    )
    assert outcome.reason == TARGET
    assert outcome.exit == 108.0


def test_a_security_that_stops_trading_is_not_a_timeout():
    # Acquired or delisted after three sessions. Calling that a timeout
    # would record an acquisition as a flat result.
    outcome = walk(flat_days(3), ENTRY, ATR, LONG)
    assert outcome.reason == DELISTED
    assert outcome.sessions_held == 3
    assert not outcome.resolved


# --- the clock ---------------------------------------------------------------


def test_nothing_happening_for_ten_sessions_is_a_timeout():
    outcome = walk(flat_days(TIMEOUT_SESSIONS), ENTRY, ATR, LONG)
    assert outcome.reason == TIMEOUT
    assert outcome.sessions_held == TIMEOUT_SESSIONS
    assert outcome.move_atr == pytest.approx(0.0)


def test_the_eleventh_session_is_never_looked_at():
    # Ten sessions flat, then a huge move on the eleventh. The rule had
    # already exited; seeing it would be look-ahead.
    quiet = flat_days(TIMEOUT_SESSIONS)
    late = bars((TIMEOUT_SESSIONS, 130.0, 131.0, 129.0, 130.0))
    outcome = walk(pd.concat([quiet, late], ignore_index=True), ENTRY, ATR, LONG)
    assert outcome.reason == TIMEOUT
    assert outcome.exit == ENTRY


def test_the_timeout_exits_at_the_last_close_of_the_tenth_session():
    rows = [(n, ENTRY, ENTRY + 0.1, ENTRY - 0.1, ENTRY) for n in range(TIMEOUT_SESSIONS - 1)]
    rows += [
        (TIMEOUT_SESSIONS - 1, ENTRY, 100.4, 99.6, 100.2),
        (TIMEOUT_SESSIONS - 1, 100.2, 101.0, 100.0, 100.8),
    ]
    outcome = walk(bars(*rows), ENTRY, ATR, LONG)
    assert outcome.reason == TIMEOUT
    assert outcome.exit == 100.8
    assert outcome.sessions_held == TIMEOUT_SESSIONS


def test_sessions_held_counts_sessions_not_bars():
    outcome = walk(
        bars((0, ENTRY, 100.2, 99.8, 100.0), (0, 100.0, 100.2, 99.8, 100.0),
             (1, 100.0, 106.5, 99.8, 106.2)),
        ENTRY, ATR, LONG,
    )  # fmt: skip
    assert outcome.reason == TARGET
    assert outcome.sessions_held == 2


# --- things that cannot be measured ------------------------------------------


def test_no_bars_means_no_entry():
    outcome = walk(bars(), ENTRY, ATR, LONG)
    assert outcome.reason == NO_ENTRY
    assert not outcome.resolved


def test_a_missing_or_zero_atr_means_no_entry():
    # Without an ATR there are no levels, so there is nothing to measure.
    for atr in (np.nan, 0.0, -1.0):
        assert walk(flat_days(3), ENTRY, atr, LONG).reason == NO_ENTRY


def test_an_unresolved_outcome_says_so():
    assert walk(flat_days(TIMEOUT_SESSIONS), ENTRY, ATR, LONG).resolved
    assert not walk(flat_days(2), ENTRY, ATR, LONG).resolved


# --- forward moves, independent of the rule ----------------------------------


def closes(*prices: float) -> pd.Series:
    return pd.Series(list(prices))


def test_forward_moves_are_measured_from_the_entry_in_atr():
    moves = forward_moves(closes(102.0, 104.0, 106.0, 108.0, 110.0), ENTRY, ATR)
    assert moves["move_1d_atr"] == pytest.approx(1.0)
    assert moves["move_3d_atr"] == pytest.approx(3.0)
    assert moves["move_5d_atr"] == pytest.approx(5.0)


def test_forward_moves_are_signed():
    moves = forward_moves(closes(96.0), ENTRY, ATR)
    assert moves["move_1d_atr"] == pytest.approx(-2.0)


def test_forward_moves_ignore_the_stop_entirely():
    # The position would have been stopped on day one. These numbers say
    # what the price did, which is a different question and the one
    # Section 13.1 asks.
    moves = forward_moves(closes(90.0, 95.0, 105.0), ENTRY, ATR)
    assert moves["move_1d_atr"] == pytest.approx(-5.0)
    assert moves["move_3d_atr"] == pytest.approx(2.5)


def test_a_horizon_beyond_the_data_is_unknown_not_zero():
    moves = forward_moves(closes(102.0, 104.0), ENTRY, ATR)
    assert moves["move_1d_atr"] == pytest.approx(1.0)
    assert np.isnan(moves["move_5d_atr"])
    assert np.isnan(moves["move_10d_atr"])


def test_every_horizon_section_10_names_is_recorded():
    moves = forward_moves(closes(*[ENTRY] * 10), ENTRY, ATR)
    assert set(moves) == {f"move_{n}d_atr" for n in FORWARD_HORIZONS}
