"""Tests for ticker-change stitching (src/vpa/data/tickers.py).
FAKE tickers and FIGIs only."""

from __future__ import annotations

from datetime import date

from vpa.data.tickers import (
    STATUS_EVENTS_DISAGREE,
    STATUS_INVALID_EVENTS,
    STATUS_NO_EVENTS,
    STATUS_NO_FIGI,
    STATUS_OK,
    Segment,
    build_identity,
    segments,
)

# FAKEOLD listed 2015, renamed FAKENEW on Wednesday 2025-08-20.
EVENTS = [
    {"type": "ticker_change", "date": "2025-08-20", "ticker_change": {"ticker": "FAKENEW"}},
    {"type": "ticker_change", "date": "2015-03-02", "ticker_change": {"ticker": "FAKEOLD"}},
]


def renamed(reference=date(2025, 9, 2)):
    return build_identity("FAKENEW", reference, "FAKEFIGI0001", "Fake Corp", EVENTS)


def test_history_before_a_rename_is_fetched_under_the_old_symbol():
    assert segments(renamed(), date(2025, 8, 1), date(2025, 8, 29)) == [
        Segment("FAKEOLD", date(2025, 8, 1), date(2025, 8, 19)),
        Segment("FAKENEW", date(2025, 8, 20), date(2025, 8, 29)),
    ]


def test_ranges_entirely_on_one_side_of_a_rename_need_no_stitch():
    identity = renamed()
    assert segments(identity, date(2025, 9, 1), date(2025, 9, 30)) == [
        Segment("FAKENEW", date(2025, 9, 1), date(2025, 9, 30))
    ]
    assert segments(identity, date(2025, 1, 1), date(2025, 1, 31)) == [
        Segment("FAKEOLD", date(2025, 1, 1), date(2025, 1, 31))
    ]


def test_a_rename_on_the_first_day_of_the_range_is_one_segment():
    assert segments(renamed(), date(2025, 8, 20), date(2025, 8, 29)) == [
        Segment("FAKENEW", date(2025, 8, 20), date(2025, 8, 29))
    ]


def test_identity_lists_every_symbol_used():
    identity = renamed()
    assert identity.status == STATUS_OK
    assert identity.all_tickers == ["FAKENEW", "FAKEOLD"]
    assert identity.security_key == "FAKEFIGI0001"


def test_no_figi_means_no_stitching():
    identity = build_identity("FAKENEW", date(2025, 9, 2), None, "Fake Corp", EVENTS)
    assert identity.status == STATUS_NO_FIGI
    assert identity.security_key == "TICKER:FAKENEW"
    assert segments(identity, date(2025, 8, 1), date(2025, 8, 29)) == [
        Segment("FAKENEW", date(2025, 8, 1), date(2025, 8, 29))
    ]


def test_no_events_means_no_stitching():
    identity = build_identity("FAKENEW", date(2025, 9, 2), "FAKEFIGI0001", "Fake Corp", [])
    assert identity.status == STATUS_NO_EVENTS
    assert len(segments(identity, date(2025, 8, 1), date(2025, 8, 29))) == 1


def test_events_that_contradict_the_requested_symbol_are_flagged_not_trusted():
    # On 2025-08-01 the vendor's history says the symbol was FAKEOLD, but
    # we were told FAKENEW was valid then. Don't guess - flag it.
    identity = renamed(reference=date(2025, 8, 1))
    assert identity.status == STATUS_EVENTS_DISAGREE
    assert segments(identity, date(2025, 8, 1), date(2025, 8, 29)) == [
        Segment("FAKENEW", date(2025, 8, 1), date(2025, 8, 29))
    ]


def test_non_ticker_change_events_are_ignored():
    events = [*EVENTS, {"type": "something_else", "date": "2025-08-25"}]
    identity = build_identity("FAKENEW", date(2025, 9, 2), "FAKEFIGI0001", "Fake Corp", events)
    assert len(identity.changes) == 2


def test_a_blank_symbol_in_the_history_means_it_cannot_be_trusted():
    # Seen in real vendor data for Talen Energy (TLN).
    events = [
        {"type": "ticker_change", "date": "2023-05-01", "ticker_change": {"ticker": ""}},
        {"type": "ticker_change", "date": "2024-07-10", "ticker_change": {"ticker": "FAKENEW"}},
    ]
    identity = build_identity("FAKENEW", date(2025, 9, 2), "FAKEFIGI0001", "Fake Corp", events)
    assert identity.status == STATUS_INVALID_EVENTS
    assert identity.changes == ()
    # Falls back to the requested symbol for the whole range - never "".
    assert segments(identity, date(2024, 6, 1), date(2024, 8, 1)) == [
        Segment("FAKENEW", date(2024, 6, 1), date(2024, 8, 1))
    ]


def test_a_missing_symbol_field_is_treated_the_same_way():
    events = [{"type": "ticker_change", "date": "2023-05-01", "ticker_change": {}}]
    identity = build_identity("FAKENEW", date(2025, 9, 2), "FAKEFIGI0001", "Fake Corp", events)
    assert identity.status == STATUS_INVALID_EVENTS
