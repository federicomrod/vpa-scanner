"""Tests for the daily Pattern C watch email (src/vpa/data/scan.py).

The sender is a fake that records what would have been sent, so nothing
here touches the network (CLAUDE.md rule 4).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from vpa.data.scan import (
    UNVALIDATED,
    render,
    run,
    unavailable,
)

SESSION = date(2026, 9, 18)


class FakeSender:
    """Records what would have gone out."""

    def __init__(self, explode: bool = False):
        self.sent: list[dict] = []
        self.explode = explode

    def send(self, *, subject: str, body: str) -> None:
        if self.explode:
            raise RuntimeError("smtp is down")
        self.sent.append({"subject": subject, "body": body})


def found(*directions: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "requested_ticker": f"FAKE{n}",
                "direction": direction,
                "slot_index": 5,
                "vol_pct_slot_60": 98.0,
                "repeat_hv_narrow_5": 2,
                "progress_5": 0.31,
                "close_loc": 0.82,
                "events": "no known event",
            }
            for n, direction in enumerate(directions)
        ]
    )


# --- the warning --------------------------------------------------------------


def test_every_email_says_it_is_unvalidated_at_the_top():
    body = render(found("C-up", "C-down"), SESSION)
    assert UNVALIDATED in body.split("\n")[1]
    assert "has NOT been tested" in body


def test_the_warning_is_there_even_when_nothing_fired():
    body = render(found(), SESSION)
    assert UNVALIDATED in body
    assert "Nothing fired today" in body


def test_the_email_says_what_happened_to_the_tested_patterns():
    # No false confidence: the reader should know the two patterns that
    # were tested did not pass.
    body = render(found("C-up"), SESSION)
    assert "retired" in body and "did not pass" in body


def test_the_email_carries_no_recommendation():
    body = render(found("C-up", "C-down"), SESSION)
    assert "no trade recommendation" in body
    for forbidden in ("buy", "sell", "position size", "stop loss", "target price"):
        assert forbidden not in body.lower(), forbidden


# --- the list -----------------------------------------------------------------


def test_each_candidate_says_why_it_fired():
    body = render(found("C-up"), SESSION)
    assert "98th percentile" in body
    assert "2 of the last 5 bars busy and narrow" in body
    assert "0.31 ATR net" in body


def test_both_directions_are_counted_separately():
    body = render(found("C-up", "C-up", "C-down"), SESSION)
    assert "C-up 2, C-down 1" in body
    assert "Candidates: 3" in body


def test_event_flags_are_shown_next_to_the_candidate():
    busy = found("C-up")
    busy.loc[0, "events"] = "earnings, month end"
    assert "earnings, month end" in render(busy, SESSION)


def test_no_known_event_is_explained_rather_than_implied():
    body = render(found("C-up"), SESSION)
    assert "It does not mean nothing happened" in body


# --- failing loudly -----------------------------------------------------------


def test_a_broken_scan_never_looks_like_a_quiet_day(tmp_path):
    sender = FakeSender()
    code = run(tmp_path, SESSION, sender)  # no data at all under tmp_path
    assert code == 1
    assert len(sender.sent) == 1
    assert "SCAN UNAVAILABLE" in sender.sent[0]["subject"]
    assert "SCAN UNAVAILABLE" in sender.sent[0]["body"]


def test_the_failure_notice_says_it_is_not_a_quiet_day(tmp_path):
    sender = FakeSender()
    run(tmp_path, SESSION, sender)
    assert "not a quiet day" in sender.sent[0]["body"]


def test_the_failure_notice_carries_the_error():
    body = unavailable(SESSION, FileNotFoundError("no features for 2026-09-18"))
    assert "FileNotFoundError" in body
    assert "no features for 2026-09-18" in body


def test_a_dead_mail_server_does_not_crash_the_run(tmp_path):
    # If the failure notice cannot be sent either, that is logged and the
    # exit code still says something went wrong.
    assert run(tmp_path, SESSION, FakeSender(explode=True)) == 1


def test_an_empty_scan_still_sends_something(tmp_path, monkeypatch):
    import vpa.data.scan as module

    monkeypatch.setattr(module, "scan", lambda root, session: found())
    sender = FakeSender()
    assert run(tmp_path, SESSION, sender) == 0
    assert "Nothing fired today" in sender.sent[0]["body"]
    assert "0 unvalidated" in sender.sent[0]["subject"]


def test_the_subject_says_unvalidated_too(tmp_path, monkeypatch):
    # The subject line is all the reader sees in a notification.
    import vpa.data.scan as module

    monkeypatch.setattr(module, "scan", lambda root, session: found("C-up"))
    sender = FakeSender()
    run(tmp_path, SESSION, sender)
    assert "unvalidated" in sender.sent[0]["subject"]


def test_nothing_here_sends_mail_by_itself():
    # The sender is always injected. There is no default that would
    # quietly reach the network in a test.
    with pytest.raises(TypeError):
        run(SESSION)  # type: ignore[call-arg]
