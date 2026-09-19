"""The NYSE exchange calendar, from the `exchange_calendars` library.

Concept v2 Section 3.2 requires session logic to use a real exchange
calendar library rather than hand-written rules, because it has to get
one-off closures (e.g. the national day of mourning on 9 January 2025)
and half days right. `vpa.trading_calendar` is the older best-effort
calendar used only by the Milestone 1 supervisor script; anything that
touches market data uses this module instead.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

import exchange_calendars as xcals
import pandas as pd

#: Earliest date the calendar is loaded from. Comfortably before any
#: data we can buy, with room for the 250-session history lookback.
CALENDAR_START = "2000-01-01"


@cache
def nyse_calendar() -> xcals.ExchangeCalendar:
    """The NYSE calendar (loaded once, then reused)."""
    return xcals.get_calendar("XNYS", start=CALENDAR_START)


def sessions_between(start: date, end: date) -> list[date]:
    """Every NYSE trading day from `start` to `end`, both inclusive."""
    sessions = nyse_calendar().sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [s.date() for s in sessions]


def is_session(day: date) -> bool:
    """Whether the NYSE is open on `day`."""
    return nyse_calendar().is_session(pd.Timestamp(day))


def previous_session(day: date) -> date:
    """The last NYSE trading day strictly before `day`."""
    day_before = pd.Timestamp(day - timedelta(days=1))
    return nyse_calendar().date_to_session(day_before, "previous").date()


def sessions_ending(day: date, count: int) -> list[date]:
    """The `count` trading days ending on (and including) session `day`."""
    if not is_session(day):
        raise ValueError(f"{day} is not an NYSE trading day")
    cal = nyse_calendar()
    end = cal.sessions.get_loc(pd.Timestamp(day))
    if end + 1 < count:
        raise ValueError(f"Calendar does not reach {count} sessions back from {day}")
    return [s.date() for s in cal.sessions[end + 1 - count : end + 1]]
