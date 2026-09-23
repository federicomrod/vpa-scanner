"""Tests for the outcomes pipeline (src/vpa/data/outcomes.py).

FAKE bars written to a temporary folder. No network, no real data.

The frozen rules themselves are tested in test_exits.py and
test_controls.py; what is tested here is the plumbing that feeds them -
which window of bars a signal sees, and what lands in each row.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from vpa.data.bars import HOURLY, derived_path
from vpa.data.calendar import sessions_between
from vpa.data.outcomes import CANDIDATE, CONTROL, Window, _outcome

SESSIONS = sessions_between(date(2025, 3, 3), date(2025, 4, 30))


def write_hourly(root, sessions, securities: dict[str, float]) -> None:
    """One bar per session per security, at a walking price."""
    for n, session in enumerate(sessions):
        rows = []
        for key, base in securities.items():
            price = base + n
            rows.append(
                {
                    "security_key": key,
                    "date": session,
                    "slot_index": 0,
                    "open": price,
                    "high": price + 0.5,
                    "low": price - 0.5,
                    "close": price,
                }
            )
        path = derived_path(root, HOURLY, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(path, index=False)


# --- the forward window ------------------------------------------------------


def test_the_window_starts_the_session_after_the_signal(tmp_path):
    write_hourly(tmp_path, SESSIONS, {"FIGI_A": 100.0})
    window = Window(tmp_path, SESSIONS)
    bars = window.after(SESSIONS[0])["FIGI_A"]
    assert bars["date"].min() == SESSIONS[1]


def test_the_window_never_reaches_past_ten_sessions(tmp_path):
    # Anything further out is not available to the rule at all, so it
    # cannot influence an outcome even by accident.
    write_hourly(tmp_path, SESSIONS, {"FIGI_A": 100.0})
    bars = Window(tmp_path, SESSIONS).after(SESSIONS[0])["FIGI_A"]
    assert len(bars) == 10
    assert bars["date"].max() == SESSIONS[10]


def test_the_signals_own_session_is_not_in_its_window(tmp_path):
    write_hourly(tmp_path, SESSIONS, {"FIGI_A": 100.0})
    bars = Window(tmp_path, SESSIONS).after(SESSIONS[3])["FIGI_A"]
    assert SESSIONS[3] not in set(bars["date"])


def test_each_security_gets_only_its_own_bars(tmp_path):
    write_hourly(tmp_path, SESSIONS, {"FIGI_A": 100.0, "FIGI_B": 50.0})
    found = Window(tmp_path, SESSIONS).after(SESSIONS[0])
    assert set(found) == {"FIGI_A", "FIGI_B"}
    assert set(found["FIGI_A"]["security_key"]) == {"FIGI_A"}


def test_a_window_at_the_end_of_the_store_is_short_not_wrong(tmp_path):
    write_hourly(tmp_path, SESSIONS, {"FIGI_A": 100.0})
    bars = Window(tmp_path, SESSIONS).after(SESSIONS[-3])["FIGI_A"]
    assert len(bars) == 2


def test_the_last_session_has_no_window_at_all(tmp_path):
    write_hourly(tmp_path, SESSIONS, {"FIGI_A": 100.0})
    assert Window(tmp_path, SESSIONS).after(SESSIONS[-1]) == {}


def test_sessions_already_passed_are_dropped_from_the_cache(tmp_path):
    write_hourly(tmp_path, SESSIONS, {"FIGI_A": 100.0})
    window = Window(tmp_path, SESSIONS)
    window.after(SESSIONS[0])
    window.after(SESSIONS[5])
    assert all(day > SESSIONS[5] for day in window._cache)


# --- one outcome -------------------------------------------------------------


def daily(atr: float) -> pd.DataFrame:
    return pd.DataFrame({"security_key": ["FIGI_A"], "daily_atr": [atr]}).set_index("security_key")


def rising_bars() -> dict[str, pd.DataFrame]:
    return {
        "FIGI_A": pd.DataFrame(
            [
                {
                    "date": SESSIONS[n + 1],
                    "slot_index": 0,
                    "open": 100.0 + n,
                    "high": 100.5 + n,
                    "low": 99.5 + n,
                    "close": 100.0 + n,
                }
                for n in range(10)
            ]
        )  # fmt: skip
    }


def test_an_outcome_records_both_directions():
    found = _outcome("FIGI_A", rising_bars(), daily(2.0), {})
    # Rising steadily: the long reaches its target, the short its stop.
    assert found["long_reason"] == "target"
    assert found["short_reason"] == "stop"
    assert found["long_r"] == pytest.approx(2.0)
    assert found["short_r"] == pytest.approx(-1.0)


def test_the_entry_is_the_first_bar_of_the_window():
    found = _outcome("FIGI_A", rising_bars(), daily(2.0), {})
    assert found["entry"] == 100.0


def test_forward_moves_are_recorded_at_every_horizon():
    found = _outcome("FIGI_A", rising_bars(), daily(2.0), {})
    assert found["move_1d_atr"] == pytest.approx(0.0)
    assert found["move_5d_atr"] == pytest.approx(2.0)
    assert found["move_10d_atr"] == pytest.approx(4.5)


def test_a_security_with_no_bars_ahead_is_blank_not_an_error():
    found = _outcome("FIGI_GONE", {}, daily(2.0), {})
    assert np.isnan(found["entry"])
    assert np.isnan(found["long_r"])
    assert found["long_reason"] is None


def test_an_outcome_is_computed_once_and_reused():
    # A control matched to three candidates is the same security-session
    # three times; Section 10's answer cannot depend on which candidate
    # it stood in for.
    cache: dict = {}
    bars = rising_bars()
    first = _outcome("FIGI_A", bars, daily(2.0), cache)
    assert "FIGI_A" in cache
    again = _outcome("FIGI_A", {}, daily(2.0), cache)  # no bars offered
    assert again == first


def test_the_daily_atr_decides_the_levels():
    # The trap from LEDGER-6: a smaller ATR puts the stop and target
    # closer, so the same bars resolve differently.
    tight = _outcome("FIGI_A", rising_bars(), daily(0.5), {})
    wide = _outcome("FIGI_A", rising_bars(), daily(20.0), {})
    assert tight["long_reason"] == "target"
    assert wide["long_reason"] == "timeout"


def test_the_kinds_are_the_two_section_11_compares():
    assert (CANDIDATE, CONTROL) == ("candidate", "control")
