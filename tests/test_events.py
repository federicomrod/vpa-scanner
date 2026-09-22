"""Tests for the Section 6 event flags.

The rules (src/vpa/signal/events.py) are tested against hand-built
session lists, so every calendar case is exact rather than "whatever the
library said". The table builder (src/vpa/data/events.py) is tested
against FAKE bars in a temporary folder. No network. See CLAUDE.md rule 4.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from vpa.data.calendar import sessions_between
from vpa.data.events import (
    build,
    dated_events,
    earnings_by_security,
    events_path,
    report_coverage,
)
from vpa.data.raw_store import write_part
from vpa.signal.events import (
    ALL_FLAGS,
    CALENDAR_FLAGS,
    EARNINGS_WINDOW,
    OBSERVABLE_FLAGS,
    STALE_AFTER_SESSIONS,
    UNAVAILABLE_FLAGS,
    any_event,
    calendar_flags,
    earnings_window,
    option_expiries,
    reacting_sessions,
    third_friday,
    trusted_sessions,
    weekdays_between,
)

# Real NYSE sessions, so the calendar rules are checked against the
# calendar the rest of the system uses.
SESSIONS_2025 = sessions_between(date(2024, 12, 1), date(2025, 12, 31))


def flags_for(session: date, sessions=None) -> pd.Series:
    table = calendar_flags(sessions or SESSIONS_2025, half_days=set()).set_index("date")
    return table.loc[session]


# --- which session reacts to an announcement ---------------------------------


def accepted(*timestamps: str) -> pd.Series:
    return pd.Series(list(timestamps))


def test_a_release_after_the_close_is_traded_the_next_session():
    # 16:17 ET on Thursday 27 February 2025 -> Friday the 28th.
    reacting = reacting_sessions(accepted("2025-02-27T21:17:00.000Z"), SESSIONS_2025)
    assert list(reacting) == [date(2025, 2, 28)]


def test_a_release_before_the_open_is_traded_the_same_session():
    # 07:38 ET on Thursday 27 February 2025 -> that same Thursday.
    reacting = reacting_sessions(accepted("2025-02-27T12:38:00.000Z"), SESSIONS_2025)
    assert list(reacting) == [date(2025, 2, 27)]


def test_the_opening_bell_itself_counts_as_too_late():
    # 09:30:00 ET exactly: the session has begun, so it is the next one.
    at_the_bell = reacting_sessions(accepted("2025-02-27T14:30:00.000Z"), SESSIONS_2025)
    a_minute_before = reacting_sessions(accepted("2025-02-27T14:29:00.000Z"), SESSIONS_2025)
    assert list(at_the_bell) == [date(2025, 2, 28)]
    assert list(a_minute_before) == [date(2025, 2, 27)]


def test_a_friday_evening_release_is_traded_on_monday():
    reacting = reacting_sessions(accepted("2025-02-28T21:17:00.000Z"), SESSIONS_2025)
    assert list(reacting) == [date(2025, 3, 3)]


def test_a_release_on_a_holiday_is_traded_the_next_session():
    # 4 July 2025 was a Friday and a market holiday.
    reacting = reacting_sessions(accepted("2025-07-04T12:00:00.000Z"), SESSIONS_2025)
    assert list(reacting) == [date(2025, 7, 7)]


def test_a_release_after_the_last_session_we_hold_is_not_pinned_to_it():
    # Silently attaching it to the final session would invent an earnings
    # day on a date that had nothing to do with it.
    reacting = reacting_sessions(accepted("2027-01-04T21:00:00.000Z"), SESSIONS_2025)
    assert pd.isna(reacting.iloc[0])


def test_no_announcements_is_not_an_error():
    assert reacting_sessions(pd.Series([], dtype="object"), SESSIONS_2025).empty


# --- the D-1 / D0 / D+1 window -----------------------------------------------


def test_the_window_is_the_day_before_the_day_and_the_day_after():
    assert EARNINGS_WINDOW == 1
    flagged = earnings_window([date(2025, 2, 27)], SESSIONS_2025)
    assert flagged == {date(2025, 2, 26), date(2025, 2, 27), date(2025, 2, 28)}


def test_the_window_is_measured_in_sessions_not_calendar_days():
    # Monday 3 March: the day before is Friday, not Sunday.
    flagged = earnings_window([date(2025, 3, 3)], SESSIONS_2025)
    assert flagged == {date(2025, 2, 28), date(2025, 3, 3), date(2025, 3, 4)}


def test_the_window_steps_over_a_holiday():
    # Friday 18 April 2025 was Good Friday; D-1 for Monday the 21st is
    # Thursday the 17th.
    assert date(2025, 4, 17) in earnings_window([date(2025, 4, 21)], SESSIONS_2025)


def test_a_window_at_the_edge_of_the_range_does_not_wrap_around():
    flagged = earnings_window([SESSIONS_2025[0]], SESSIONS_2025)
    assert flagged == {SESSIONS_2025[0], SESSIONS_2025[1]}
    assert SESSIONS_2025[-1] not in flagged


def test_an_announcement_outside_the_range_flags_nothing():
    assert earnings_window([date(2030, 6, 3)], SESSIONS_2025) == set()


# --- where a false can be believed -------------------------------------------


def test_the_days_around_a_filing_are_trusted():
    trusted = trusted_sessions([date(2025, 6, 2)], SESSIONS_2025, stale_after=10)
    assert date(2025, 6, 2) in trusted
    # Ten sessions either side, and no further.
    assert len(trusted) == 21
    assert max(trusted) == SESSIONS_2025[SESSIONS_2025.index(date(2025, 6, 2)) + 10]


def test_a_quiet_stretch_is_not_trusted():
    # The shape that prompted this rule: a company whose item 2.02 record
    # stops in 2016 because it reports under item 8.01 instead. Every day
    # after would otherwise read a confident "no earnings".
    trusted = trusted_sessions([SESSIONS_2025[0]], SESSIONS_2025, STALE_AFTER_SESSIONS)
    assert SESSIONS_2025[0] in trusted
    assert SESSIONS_2025[-1] not in trusted


def test_trust_looks_both_ways_so_a_hole_is_found_from_either_side():
    # A gap in the middle: trusted near each filing, not in between.
    filings = [SESSIONS_2025[0], SESSIONS_2025[-1]]
    trusted = trusted_sessions(filings, SESSIONS_2025, stale_after=5)
    assert SESSIONS_2025[3] in trusted  # after the first
    assert SESSIONS_2025[-4] in trusted  # before the last
    assert SESSIONS_2025[100] not in trusted


def test_a_quarterly_reporter_is_trusted_throughout():
    # Filing every 63 sessions, the ordinary rhythm: no honest quarterly
    # reporter should be dropped by this rule.
    filings = SESSIONS_2025[::63]
    trusted = trusted_sessions(filings, SESSIONS_2025, STALE_AFTER_SESSIONS)
    assert set(SESSIONS_2025) == trusted


def test_nothing_filed_means_nothing_is_trusted():
    assert trusted_sessions([], SESSIONS_2025, STALE_AFTER_SESSIONS) == set()


def test_the_staleness_threshold_is_two_reporting_quarters():
    assert STALE_AFTER_SESSIONS == 126


# --- option expiry and triple witching ---------------------------------------


def test_option_expiry_is_the_third_friday():
    assert third_friday(2025, 1) == date(2025, 1, 17)
    assert third_friday(2025, 8) == date(2025, 8, 15)
    # 1 August 2025 was itself a Friday: the third is the 15th, not the 22nd.
    assert flags_for(date(2025, 1, 17))["option_expiry"]
    assert not flags_for(date(2025, 1, 16))["option_expiry"]


def test_expiry_moves_back_a_session_when_the_third_friday_is_a_holiday():
    # Good Friday, 18 April 2025: expiry is Thursday the 17th.
    assert third_friday(2025, 4) == date(2025, 4, 18)
    assert flags_for(date(2025, 4, 17))["option_expiry"]
    assert date(2025, 4, 18) not in option_expiries(SESSIONS_2025)


def test_there_is_exactly_one_expiry_a_month():
    expiries = option_expiries(SESSIONS_2025)
    months = sorted({(d.year, d.month) for d in SESSIONS_2025})
    assert len(expiries) == len(months)


def test_triple_witching_is_the_quarterly_expiry_only():
    for quarter_end in (date(2025, 3, 21), date(2025, 6, 20), date(2025, 9, 19),
                        date(2025, 12, 19)):  # fmt: skip
        assert flags_for(quarter_end)["triple_witching"], quarter_end
        assert flags_for(quarter_end)["option_expiry"], quarter_end
    # January's expiry is an ordinary one.
    assert not flags_for(date(2025, 1, 17))["triple_witching"]


# --- month end, quarter end, holidays ----------------------------------------


def test_month_end_is_the_last_session_of_the_month():
    assert flags_for(date(2025, 1, 31))["month_end"]
    assert not flags_for(date(2025, 1, 30))["month_end"]
    # 31 May 2025 was a Saturday: the month ends on Friday the 30th.
    assert flags_for(date(2025, 5, 30))["month_end"]


def test_quarter_end_is_a_month_end_in_march_june_september_december():
    assert flags_for(date(2025, 3, 31))["quarter_end"]
    assert flags_for(date(2025, 6, 30))["quarter_end"]
    assert not flags_for(date(2025, 1, 31))["quarter_end"]
    assert not flags_for(date(2025, 3, 28))["quarter_end"]


def test_day_after_holiday_is_set_only_when_the_market_was_actually_shut():
    # Monday 21 April 2025 follows Good Friday.
    assert flags_for(date(2025, 4, 21))["day_after_holiday"]
    # An ordinary Monday follows only a weekend.
    assert not flags_for(date(2025, 4, 14))["day_after_holiday"]
    # Friday 25 July follows Thursday: no gap at all.
    assert not flags_for(date(2025, 7, 25))["day_after_holiday"]


def test_a_mid_week_holiday_flags_the_session_after_it():
    # Christmas Day 2025 was a Thursday; Friday the 26th follows it.
    assert flags_for(date(2025, 12, 26))["day_after_holiday"]


def test_weekdays_between_ignores_the_weekend():
    assert weekdays_between(date(2025, 4, 11), date(2025, 4, 14)) == 0  # Fri -> Mon
    assert weekdays_between(date(2025, 4, 17), date(2025, 4, 21)) == 1  # Good Friday
    assert weekdays_between(date(2025, 7, 3), date(2025, 7, 7)) == 1  # 4 July


def test_half_days_are_the_ones_the_caller_says_they_are():
    # The bars know the close time; this module does not guess it.
    table = calendar_flags(SESSIONS_2025, half_days={date(2025, 11, 28)}).set_index("date")
    assert table.loc[date(2025, 11, 28), "half_day"]
    assert not table.loc[date(2025, 11, 26), "half_day"]


def test_the_edges_of_the_range_admit_what_they_cannot_know():
    table = calendar_flags(SESSIONS_2025, half_days=set()).set_index("date")
    # Nothing before the first session was given, so a preceding holiday
    # is unknowable - and says so rather than guessing "no".
    assert pd.isna(table.iloc[0]["day_after_holiday"])
    assert pd.isna(table.iloc[-1]["month_end"])
    assert pd.isna(table.iloc[-1]["quarter_end"])


def test_a_single_session_is_all_unknowns_where_it_must_be():
    table = calendar_flags([date(2025, 4, 21)], half_days=set())
    assert pd.isna(table.iloc[0]["day_after_holiday"])
    assert pd.isna(table.iloc[0]["month_end"])


# --- combining the flags ------------------------------------------------------


def frame(**columns) -> pd.DataFrame:
    return pd.DataFrame({k: pd.array(v, dtype="boolean") for k, v in columns.items()})


def test_any_event_is_true_when_something_fired():
    combined = any_event(frame(earnings=[True, False], half_day=[False, False]))
    assert list(combined) == [True, False]


def test_any_event_is_unknown_when_something_could_not_be_checked():
    combined = any_event(frame(earnings=[None, False], half_day=[False, False]))
    assert pd.isna(combined.iloc[0])
    assert combined.iloc[1] is False or combined.iloc[1] == False  # noqa: E712


def test_a_known_event_beats_an_unknown_one():
    # We do not need to know everything to say something happened.
    combined = any_event(frame(earnings=[None], split=[True]))
    assert combined.iloc[0]


def test_the_flags_with_no_source_do_not_swallow_every_day():
    # halt and index_change are unknown for everyone, always. Folding
    # them in would make any_event unknown on every row ever, and
    # validation would have nothing left to exclude against.
    assert set(UNAVAILABLE_FLAGS) == {"halt", "index_change"}
    assert not (set(UNAVAILABLE_FLAGS) & set(OBSERVABLE_FLAGS))
    combined = any_event(
        frame(earnings=[False], half_day=[False], halt=[None], index_change=[None])
    )
    assert combined.iloc[0] == False  # noqa: E712


def test_combining_nothing_is_an_error_not_a_confident_false():
    with pytest.raises(ValueError, match="No Section 6 flags"):
        any_event(pd.DataFrame({"security_key": ["FIGI_A"]}))


# --- the stored table ---------------------------------------------------------


def store_daily_bars(root, sessions, securities: dict[str, str], half_days=()) -> None:
    for session in sessions:
        path = root / "derived" / "daily" / f"date={session.isoformat()}" / "bars.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            {
                "security_key": list(securities),
                "requested_ticker": list(securities.values()),
                "date": session,
                "close": 10.0,
                "volume": 1000.0,
                "is_half_day": session in half_days,
            }
        ).to_parquet(path, index=False)


def store_raw(root, dataset, table: pd.DataFrame) -> None:
    write_part(root, dataset, "fetched=2026-01-01", "run-1", table, {"dataset": dataset})


@pytest.fixture
def store(tmp_path):
    sessions = sessions_between(date(2025, 1, 2), date(2025, 6, 30))
    store_daily_bars(tmp_path, sessions, {"FIGI_FAKEA": "FAKEA", "FIGI_FPI": "FPI"})
    store_raw(
        tmp_path,
        "earnings_filings",
        pd.DataFrame(
            {
                "security_key": ["FIGI_FAKEA"],
                "ticker": ["FAKEA"],
                "cik": [1018],
                "accession_number": ["0000-01"],
                "filing_date": [date(2025, 2, 27)],
                "accepted_utc": ["2025-02-27T21:17:00.000Z"],
                "items": ["2.02"],
            }
        ),
    )
    store_raw(
        tmp_path,
        "dividends",
        pd.DataFrame({"requested_ticker": ["FAKEA"], "ex_dividend_date": ["2025-03-14"]}),
    )
    store_raw(
        tmp_path,
        "splits",
        pd.DataFrame({"requested_ticker": ["FAKEA"], "execution_date": ["2025-05-06"]}),
    )
    store_raw(
        tmp_path, "security_cik", pd.DataFrame({"security_key": ["FIGI_FAKEA"], "cik": [1018]})
    )
    return tmp_path, sessions


def stored(root, session) -> pd.DataFrame:
    return pd.read_parquet(events_path(root, session)).set_index("security_key")


def test_it_writes_one_file_per_session(store):
    root, sessions = store
    build(root, sessions)
    for session in sessions:
        assert events_path(root, session).exists()


def test_every_section_6_flag_is_present(store):
    root, sessions = store
    build(root, sessions)
    table = stored(root, sessions[0])
    for flag in [*ALL_FLAGS, "any_event"]:
        assert flag in table.columns, flag


def test_the_earnings_flag_lands_on_the_session_that_reacted(store):
    root, sessions = store
    build(root, sessions)
    # Accepted 16:17 ET on Thursday the 27th: the 28th reacted, so the
    # window is the 27th, 28th and the following Monday the 3rd.
    for day in (date(2025, 2, 27), date(2025, 2, 28), date(2025, 3, 3)):
        assert stored(root, day).loc["FIGI_FAKEA", "earnings"], day
    assert not stored(root, date(2025, 2, 26)).loc["FIGI_FAKEA", "earnings"]
    assert not stored(root, date(2025, 3, 4)).loc["FIGI_FAKEA", "earnings"]


def test_a_security_we_hold_no_filings_for_is_unknown_never_false(store):
    root, sessions = store
    build(root, sessions)
    # FPI files 6-K, not 8-K: we cannot say whether it reported. Saying
    # "no" would quietly leave its earnings days in the validation set.
    for session in (sessions[0], date(2025, 2, 27), sessions[-1]):
        assert pd.isna(stored(root, session).loc["FIGI_FPI", "earnings"]), session
        assert pd.isna(stored(root, session).loc["FIGI_FPI", "any_event"]), session


def test_ex_dividend_and_split_land_on_their_own_dates(store):
    root, sessions = store
    build(root, sessions)
    assert stored(root, date(2025, 3, 14)).loc["FIGI_FAKEA", "ex_dividend"]
    assert not stored(root, date(2025, 3, 13)).loc["FIGI_FAKEA", "ex_dividend"]
    assert stored(root, date(2025, 5, 6)).loc["FIGI_FAKEA", "split"]
    assert not stored(root, date(2025, 5, 5)).loc["FIGI_FAKEA", "split"]
    # They belong to the one security, not to everything that day.
    assert not stored(root, date(2025, 3, 14)).loc["FIGI_FPI", "ex_dividend"]


def test_calendar_flags_apply_to_every_security_that_day(store):
    root, sessions = store
    build(root, sessions)
    table = stored(root, date(2025, 3, 21))  # triple witching
    assert table["option_expiry"].all()
    assert table["triple_witching"].all()


def test_an_ordinary_day_with_nothing_on_it_is_a_clean_false(store):
    root, sessions = store
    build(root, sessions)
    row = stored(root, date(2025, 3, 12)).loc["FIGI_FAKEA"]
    assert row["any_event"] == False  # noqa: E712
    # ...even though two flags on the row are unknown for everyone.
    assert pd.isna(row["halt"]) and pd.isna(row["index_change"])


def test_half_days_come_from_the_bars(tmp_path):
    sessions = sessions_between(date(2025, 11, 20), date(2025, 12, 5))
    store_daily_bars(tmp_path, sessions, {"FIGI_A": "A"}, half_days={date(2025, 11, 28)})
    store_raw(tmp_path, "earnings_filings", pd.DataFrame())
    build(tmp_path, sessions)
    assert stored(tmp_path, date(2025, 11, 28))["half_day"].all()
    assert not stored(tmp_path, date(2025, 11, 26))["half_day"].any()


def test_a_second_run_skips_sessions_already_built(store):
    root, sessions = store
    assert build(root, sessions) > 0
    assert build(root, sessions) == 0
    assert build(root, sessions, rebuild=True) > 0


def test_rows_are_written_for_the_securities_that_traded_that_day(store):
    root, sessions = store
    build(root, sessions)
    assert set(stored(root, sessions[0]).index) == {"FIGI_FAKEA", "FIGI_FPI"}


# --- coverage reporting -------------------------------------------------------


def test_a_security_with_no_filings_is_reported_as_such(store):
    root, sessions = store
    coverage = report_coverage(root, sessions)
    assert set(coverage[coverage["security_key"] == "FIGI_FPI"]["state"]) == {"no filings at all"}


def test_coverage_measures_staleness_not_merely_being_past_the_last_filing(store):
    root, sessions = store
    fakea = report_coverage(root, sessions)
    fakea = fakea[fakea["security_key"] == "FIGI_FAKEA"].set_index("date")
    # Before it had ever filed: nothing to be stale against.
    assert fakea.loc[date(2025, 1, 2), "state"] == "before first filing"
    assert pd.isna(fakea.loc[date(2025, 1, 2), "gap_sessions"])
    # The filing itself, and the ordinary weeks that follow it. Counting
    # these as a coverage failure - as "after the last filing" would -
    # would condemn every quarterly reporter on most of its days.
    assert fakea.loc[date(2025, 2, 27), "gap_sessions"] == 0
    assert fakea.loc[date(2025, 4, 1), "state"] == "within a quarter of a filing"
    # Four months on with no second filing: that is a hole.
    assert fakea.loc[date(2025, 6, 30), "state"] == "one to two quarters"


def test_a_long_silence_is_called_a_suspect_hole(tmp_path):
    sessions = sessions_between(date(2025, 1, 2), date(2026, 6, 30))
    store_daily_bars(tmp_path, sessions, {"FIGI_QUIET": "QUIET"})
    store_raw(
        tmp_path,
        "earnings_filings",
        pd.DataFrame(
            {
                "security_key": ["FIGI_QUIET"],
                "filing_date": [date(2025, 1, 10)],
                "accepted_utc": ["2025-01-10T21:00:00.000Z"],
            }
        ),
    )
    coverage = report_coverage(tmp_path, sessions).set_index("date")
    assert coverage.loc[date(2026, 6, 30), "state"] == "over two quarters - suspect hole"
    assert coverage.loc[date(2026, 6, 30), "gap_sessions"] > 126


# --- gathering helpers --------------------------------------------------------


def test_corporate_actions_are_grouped_by_the_ticker_they_were_asked_for():
    table = pd.DataFrame(
        {
            "requested_ticker": ["FAKEA", "FAKEA", "FAKEB"],
            "ex_dividend_date": ["2025-03-14", "2025-06-13", "2025-03-14"],
        }
    )
    assert dated_events(table, "ex_dividend_date") == {
        "FAKEA": {date(2025, 3, 14), date(2025, 6, 13)},
        "FAKEB": {date(2025, 3, 14)},
    }


def test_a_missing_date_is_dropped_rather_than_becoming_a_flag():
    table = pd.DataFrame({"requested_ticker": ["FAKEA"], "ex_dividend_date": [None]})
    assert dated_events(table, "ex_dividend_date") == {}


def test_a_filing_makes_the_days_around_it_answerable():
    filings = pd.DataFrame(
        {
            "security_key": ["FIGI_A"],
            "accepted_utc": ["2025-02-27T21:17:00.000Z"],
        }
    )
    flagged, trusted = earnings_by_security(filings, SESSIONS_2025)
    assert flagged["FIGI_A"] == {date(2025, 2, 27), date(2025, 2, 28), date(2025, 3, 3)}
    # A false near the filing means something; far from it, nothing does.
    assert date(2025, 3, 4) in trusted["FIGI_A"]
    assert date(2025, 12, 31) not in trusted["FIGI_A"]


def test_no_filings_at_all_means_nobody_is_answerable():
    assert earnings_by_security(pd.DataFrame(), SESSIONS_2025) == ({}, {})


def test_the_calendar_flag_list_matches_what_section_6_names():
    assert CALENDAR_FLAGS == [
        "option_expiry",
        "triple_witching",
        "month_end",
        "quarter_end",
        "half_day",
        "day_after_holiday",
    ]
