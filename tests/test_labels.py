"""Tests for the blind label tool (src/vpa/data/labels.py, charts.py).

FAKE bars in a temporary folder, and a temporary label file. No network,
no real data, and nothing here touches a real label.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from vpa.data.bars import DAILY, HOURLY, derived_path
from vpa.data.calendar import sessions_between
from vpa.data.charts import REQUIRED, chart_svg
from vpa.data.labels import (
    DONE,
    PAGE,
    already_seen,
    append_label,
    daily_history,
    history_for,
    plan_session,
    read_labels,
)

SESSIONS = sessions_between(date(2025, 3, 3), date(2025, 3, 21))
SLOTS = 7


def write_bars(root, sessions, keys=("FIGI_A",)) -> None:
    for n, session in enumerate(sessions):
        rows = [
            {
                "security_key": key,
                "date": session,
                "slot_index": slot,
                "open": 100.0 + n + slot * 0.1,
                "high": 100.6 + n + slot * 0.1,
                "low": 99.4 + n + slot * 0.1,
                "close": 100.2 + n + slot * 0.1,
                "volume": 1000.0 + slot * 10,
            }
            for key in keys
            for slot in range(SLOTS)
        ]
        path = derived_path(root, HOURLY, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(path, index=False)
        # The session list comes from the daily store, as in the real one.
        daily = derived_path(root, DAILY, session)
        daily.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{**rows[0], "slot_index": 0}]).to_parquet(daily, index=False)


def bars(count: int = 20) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": date(2025, 3, 10),
                "slot_index": n,
                "open": 100.0 + n,
                "high": 101.0 + n,
                "low": 99.0 + n,
                "close": 100.5 + n,
                "volume": 1000 + n * 50,
            }
            for n in range(count)
        ]
    )


# --- the chart ----------------------------------------------------------------


def test_a_chart_draws_every_bar_it_is_given():
    svg = chart_svg(bars(20))
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count("<rect") >= 20  # a body and a volume bar for each


def test_a_chart_shows_volume():
    # This is Volume Price Analysis. A chart without volume asks the
    # wrong question.
    tall = bars(5).assign(volume=[100, 200, 300, 400, 5000])
    svg = chart_svg(tall)
    assert svg.count('opacity="0.55"') == 5


def test_up_and_down_bars_are_told_apart():
    mixed = bars(2).copy()
    mixed.loc[0, "close"] = mixed.loc[0, "open"] - 1  # a down bar
    svg = chart_svg(mixed)
    assert "#1a7f4b" in svg and "#b3261e" in svg


def test_a_chart_needs_the_columns_it_draws():
    with pytest.raises(ValueError, match="chart needs columns"):
        chart_svg(bars(5).drop(columns=["volume"]))
    assert "volume" in REQUIRED


def test_an_empty_chart_is_an_error_not_a_blank_picture():
    with pytest.raises(ValueError, match="at least one bar"):
        chart_svg(bars(1).iloc[:0])


def test_a_subtitle_is_escaped_not_injected():
    svg = chart_svg(bars(3), subtitle="<script>x</script>")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


# --- truncation ---------------------------------------------------------------


def test_history_stops_at_the_bar_being_judged(tmp_path):
    write_bars(tmp_path, SESSIONS)
    found = history_for(tmp_path, "FIGI_A", SESSIONS[5], slot=3)
    last = found.iloc[-1]
    assert last["date"] == SESSIONS[5]
    assert last["slot_index"] == 3


def test_no_later_session_is_read_at_all(tmp_path):
    write_bars(tmp_path, SESSIONS)
    found = history_for(tmp_path, "FIGI_A", SESSIONS[5], slot=3)
    assert found["date"].max() == SESSIONS[5]
    assert SESSIONS[6] not in set(found["date"])


def test_no_later_slot_of_the_same_session_is_read(tmp_path):
    # The bar being judged is slot 3; slots 4 to 6 of that day have not
    # happened yet from the trader's point of view.
    write_bars(tmp_path, SESSIONS)
    found = history_for(tmp_path, "FIGI_A", SESSIONS[5], slot=3)
    same_day = found[found["date"] == SESSIONS[5]]
    assert set(same_day["slot_index"]) == {0, 1, 2, 3}


def test_the_future_cannot_be_in_the_picture_because_it_is_never_loaded(tmp_path):
    write_bars(tmp_path, SESSIONS)
    found = history_for(tmp_path, "FIGI_A", SESSIONS[5], slot=3)
    svg = chart_svg(found)
    # Prices rise by 1.0 a session, so any later session's prices would
    # be numerically out of range of every number drawn on the chart.
    assert found["high"].max() < 100.6 + 6
    for later in range(6, len(SESSIONS)):
        assert f"{100.6 + later:,.2f}" not in svg


def test_a_session_we_do_not_hold_is_a_clear_error(tmp_path):
    write_bars(tmp_path, SESSIONS)
    with pytest.raises(ValueError, match="not a stored session"):
        history_for(tmp_path, "FIGI_A", date(1999, 1, 4), slot=3)


def test_only_the_charted_security_is_read(tmp_path):
    write_bars(tmp_path, SESSIONS, keys=("FIGI_A", "FIGI_B"))
    found = history_for(tmp_path, "FIGI_A", SESSIONS[5], slot=3)
    assert set(found["security_key"]) == {"FIGI_A"}


# --- never the same chart twice ----------------------------------------------


def test_a_labelled_bar_is_not_offered_again(tmp_path):
    # Section 12 shows a chart twice only as a deliberate test-retest,
    # after 28 days. An accidental repeat would look like agreement with
    # himself and inflate the consistency this is meant to measure.
    labels = [record(security_key="FIGI_A", date="2025-03-10", slot_index=3)]
    assert already_seen(labels) == {("FIGI_A", "2025-03-10", 3)}


def test_seen_bars_are_matched_however_the_date_was_stored(tmp_path):
    # Labels round-trip through JSON, so the date comes back as a string
    # while the bar frame holds a date object.
    labels = [record(date=str(date(2025, 3, 10)))]
    assert ("FIGI_A", "2025-03-10", 3) in already_seen(labels)


def test_two_sessions_in_one_day_draw_different_charts(tmp_path, monkeypatch):
    # The bug this replaces: the seed was the date, so a second session
    # on the same day handed back the same ten charts.
    import vpa.data.labels as module

    pool = pd.DataFrame(
        [
            {
                "security_key": f"FIGI_{n:03d}",
                "requested_ticker": f"T{n}",
                "date": date(2025, 3, 10),
                "slot_index": n % 7,
                "is_candidate": n < 20,
            }
            for n in range(400)
        ]
    )
    # plan_session skips the first 120 sessions as feature warm-up.
    many = sessions_between(date(2024, 1, 2), date(2025, 3, 10))
    assert len(many) > 130
    monkeypatch.setattr(module, "stored_sessions", lambda root: many)
    monkeypatch.setattr(module, "eligible_bars", lambda root, session: pool)
    first = plan_session(tmp_path, seed="2025-09-24-0", count=10)
    second = plan_session(tmp_path, seed="2025-09-24-10", count=10)
    assert list(first["security_key"]) != list(second["security_key"])


# --- the daily panel ----------------------------------------------------------


def test_the_daily_panel_ends_with_a_partial_bar(tmp_path):
    # The stored daily bar covers the whole session, so mid-session it
    # contains hours that have not happened yet. The last bar must be
    # built from the hours up to the one being judged.
    write_bars(tmp_path, SESSIONS)
    found = daily_history(tmp_path, "FIGI_A", SESSIONS[5], slot=2)
    last = found.iloc[-1]
    assert last["date"] == SESSIONS[5]
    # Slots 0-2 only: the high cannot include slots 3-6, which are higher.
    assert last["high"] < 100.6 + 5 + 3 * 0.1


def test_the_daily_panel_gives_weeks_of_context_not_days(tmp_path):
    write_bars(tmp_path, SESSIONS)
    found = daily_history(tmp_path, "FIGI_A", SESSIONS[-1], slot=6)
    assert len(found) == len(SESSIONS)  # every earlier session, plus today


def test_the_daily_panel_reads_no_later_session(tmp_path):
    write_bars(tmp_path, SESSIONS)
    found = daily_history(tmp_path, "FIGI_A", SESSIONS[5], slot=3)
    assert found["date"].max() == SESSIONS[5]


def test_the_daily_panel_shows_only_the_charted_security(tmp_path):
    write_bars(tmp_path, SESSIONS, keys=("FIGI_A", "FIGI_B"))
    found = daily_history(tmp_path, "FIGI_A", SESSIONS[5], slot=3)
    assert set(found["security_key"]) == {"FIGI_A"}


# --- keeping the trader blind -------------------------------------------------


def test_the_page_cannot_say_which_stratum_a_chart_came_from():
    # Section 12: the trader is never told. If the stratum reached the
    # page, "view source" would answer it and the labels would stop
    # being blind.
    page = PAGE.format(chart="<svg/>", daily="<svg/>", position=1, total=10, stored=0)
    assert "stratum" not in page
    assert "candidate" not in page.lower()
    assert "weight" not in page


def test_the_finish_page_counts_labels_and_sessions_separately():
    # The confusion this replaces: "29 sessions to reach 300" read as
    # though 29 labels remained.
    page = DONE.format(labelled=10, to_go=290, remaining=29, target=300)
    assert "290 more labels" in page
    assert "29 more sessions" in page


def test_the_page_offers_both_answers_and_two_optional_fields():
    page = PAGE.format(chart="<svg/>", daily="<svg/>", position=1, total=10, stored=0)
    assert 'value="interesting"' in page and 'value="not_interesting"' in page
    assert 'name="note"' in page and 'name="pattern_guess"' in page


# --- the label file -----------------------------------------------------------


def record(**overrides) -> dict:
    return {
        "security_key": "FIGI_A",
        "date": "2025-03-10",
        "slot_index": 3,
        "stratum": "candidate",
        "weight": 10.0,
        "shown_on": "2025-09-23",
        "is_retest": False,
        "response": "interesting",
        "note": "",
        "pattern_guess": "",
        **overrides,
    }


def test_labels_round_trip(tmp_path):
    append_label(tmp_path, record())
    found = read_labels(tmp_path)
    assert len(found) == 1
    assert found[0]["response"] == "interesting"


def test_the_stratum_and_weight_are_recorded_even_though_they_are_never_shown(tmp_path):
    append_label(tmp_path, record())
    stored = read_labels(tmp_path)[0]
    assert stored["stratum"] == "candidate"
    assert stored["weight"] == 10.0


def test_the_file_is_appended_never_rewritten(tmp_path):
    for n in range(3):
        append_label(tmp_path, record(note=f"note {n}"))
    found = read_labels(tmp_path)
    assert [f["note"] for f in found] == ["note 0", "note 1", "note 2"]


def test_a_session_cannot_damage_what_earlier_ones_stored(tmp_path):
    append_label(tmp_path, record(note="first"))
    raw = (tmp_path / "blind-labels.jsonl").read_text()
    append_label(tmp_path, record(note="second"))
    assert (tmp_path / "blind-labels.jsonl").read_text().startswith(raw)


def test_no_labels_yet_is_not_an_error(tmp_path):
    assert read_labels(tmp_path) == []


def test_a_label_is_one_json_object_per_line(tmp_path):
    append_label(tmp_path, record())
    append_label(tmp_path, record())
    lines = (tmp_path / "blind-labels.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
    assert all(json.loads(line)["security_key"] == "FIGI_A" for line in lines)
