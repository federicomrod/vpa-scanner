"""Event flags - Concept v2 Section 6.

Every ticker-day carries flags saying what was happening to it. They
exist so the system never has to guess why volume was unusual: if the
event table says nothing, the correct output is "no known event", not a
plausible story.

They are used in opposite directions on purpose. **In validation**,
flagged days are excluded - the question is whether a pattern carries
information under clean conditions before asking whether it survives
contamination. **In production**, flagged days are kept but marked, so
the trader reads "elevated volume; ex-dividend today" and discounts it.

### Three states, not two

A flag is true, false, or **unknown**, and the third is not decoration.
Section 11 excludes flagged days from validation, so a flag reading false
where the truth is merely unavailable quietly leaves contaminated days in
the clean set - and nothing about the output would look wrong.

Unknown arises in two quite different ways, and LEDGER-4 keeps them
apart:

- **Per security.** Foreign private issuers file 6-K rather than 8-K, so
  no results filing exists to date their earnings - 71 securities that
  have passed through the universe, about 3.4% of member-months. Their
  earnings flag is unknown on every day, and `any_event` carries that
  through, so validation can drop them.
- **Everywhere, for everyone.** Trading halts and index additions or
  deletions have no historical source at any price. A flag that is
  unknown for every security on every day cannot distinguish anything,
  and folding it into `any_event` would make every day unknown and
  validation impossible. These two are therefore recorded as columns,
  always unknown, and left **out** of `any_event` - which consequently
  means "no event that we can observe", not "no event". That limit is
  stated here rather than hidden.

### Facts in, flags out

Like the rest of the frozen area, nothing here looks anything up. The
caller supplies the sessions, which half days they were, and the dated
company events; this module decides only what Section 6 says about them.
That is also why every calendar rule below is derived from the ordered
session list rather than from a holiday table.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

import numpy as np
import pandas as pd

from vpa.signal.bars import HALF_DAY_CLOSE, REGULAR_CLOSE

#: Sessions either side of an announcement that are flagged: Section 6's
#: "earnings (D-1, D0, D+1)".
EARNINGS_WINDOW = 1

#: A quarterly reporter files about every 63 sessions. Past twice that
#: with nothing on either side, the filing record has stopped speaking
#: for the company and "no earnings" becomes a guess, not a fact
#: (LEDGER-4, decision 7).
STALE_AFTER_SESSIONS = 126

#: Flags that come from a company's own events (Section 6).
COMPANY_FLAGS = ["earnings", "ex_dividend", "split", "index_change", "halt"]

#: Flags that apply to every security on a given session (Section 6).
CALENDAR_FLAGS = [
    "option_expiry",
    "triple_witching",
    "month_end",
    "quarter_end",
    "half_day",
    "day_after_holiday",
]

#: Scheduled economic events that move the whole market (Section 6).
#: Their dates come from the committed table built by `vpa.data.macro`
#: from the Federal Reserve and the BLS - never from recollection
#: (LEDGER-4, amendment 1).
MACRO_FLAGS = ["fomc", "cpi", "payrolls"]

#: Flags with no historical source for anyone, ever (LEDGER-4). They are
#: recorded as always-unknown columns and excluded from `any_event`.
UNAVAILABLE_FLAGS = ["index_change", "halt"]

ALL_FLAGS = [*COMPANY_FLAGS, *CALENDAR_FLAGS, *MACRO_FLAGS]

#: The flags `any_event` is computed from: everything with a source.
OBSERVABLE_FLAGS = [flag for flag in ALL_FLAGS if flag not in UNAVAILABLE_FLAGS]

#: Session close times come from Section 4's definition of a session, so
#: there is one place that says when trading stops.


def reacting_sessions(
    accepted_utc: pd.Series, sessions: Sequence[date], half_days: set[date] | None = None
) -> pd.Series:
    """The session that first traded on each announcement.

    **The close is what matters, not the open.** A release published at
    07:38 and one published at 12:00 are both traded by the session they
    land in - the first at the bell, the second within the minute. Only a
    release at or after the close waits for the next session.

    Getting this wrong is not a rounding error. Measured over the store's
    75,995 results filings: 43.5% arrive before the open, 42.6% after the
    close, and **13.9% during the session** - and an earlier version of
    this function pushed all of that middle group to the following day
    (LEDGER-4, amendment 1).

    Weekends and holidays fall out without special handling, because only
    sessions are candidates. An announcement with no session to react to
    - before the first we hold, or after the last - comes back as NaT.
    """
    if accepted_utc.empty:
        return pd.Series([], index=accepted_utc.index, dtype="object")
    eastern = pd.to_datetime(accepted_utc, utc=True, format="mixed").dt.tz_convert(
        "America/New_York"
    )
    ordered = np.array(sessions, dtype="object")
    reacting = [
        _reacting_session(day, clock, ordered, half_days or set())
        for day, clock in zip(eastern.dt.date, eastern.dt.time, strict=True)
    ]
    return pd.Series(reacting, index=accepted_utc.index, dtype="object")


def _reacting_session(day: date, clock, ordered, half_days: set[date]) -> date:
    """The first session that could trade on news released at `clock` on
    `day`.

    "left" lets that day react if it is a session; "right" steps past it,
    so only a later session can.

    News from outside the range comes back as NaT at **both** ends, and
    the earlier end is the one that bites. Without the check, every
    release before the first session we hold lands on that first session:
    a company with filings back to 2004 would show an earnings day on the
    first morning of the store, and so would every other company, all at
    once. Measured before this guard was added: 739 of 817 securities
    flagged on 2016-10-03, against 3 to 8 on an ordinary day.
    """
    if len(ordered) == 0 or day < ordered[0]:
        return pd.NaT
    close = HALF_DAY_CLOSE if day in half_days else REGULAR_CLOSE
    before_the_close = clock is None or clock < close
    at = int(np.searchsorted(ordered, day, side="left" if before_the_close else "right"))
    return ordered[at] if at < len(ordered) else pd.NaT


def macro_flags(
    sessions: Sequence[date],
    releases: Sequence[tuple[date, str, str]],
    half_days: set[date] | None = None,
) -> pd.DataFrame:
    """Section 6's macro events, one row per session.

    `releases` is (date, kind, time in ET) as the committed table records
    them. The release date is not always the session that traded on it:
    the CPI and payrolls come out at 08:30 and an FOMC decision at 14:00,
    so in both cases that same session reacts - but the FOMC's emergency
    statement of Sunday 15 March 2020 was first traded on the Monday. The
    same rule decides all three.

    A session with no release of a given kind is a plain false, never
    unknown: unlike a company's earnings, the release calendar is
    complete - every one of these is scheduled and published in advance.
    """
    ordered = np.array(sessions, dtype="object")
    flagged: dict[str, set[date]] = {kind: set() for kind in MACRO_FLAGS}
    for day, kind, time_et in releases:
        if kind not in flagged:
            raise ValueError(f"Unknown macro event kind {kind!r}; expected one of {MACRO_FLAGS}")
        session = _reacting_session(day, _clock(time_et), ordered, half_days or set())
        if not pd.isna(session):
            flagged[kind].add(session)
    frame = pd.DataFrame({"date": list(sessions)})
    for kind in MACRO_FLAGS:
        frame[kind] = pd.array([s in flagged[kind] for s in sessions], dtype="boolean")
    return frame


def _clock(time_et: str):
    """A release time in ET, or None where the table records none - in
    which case the release is taken to have landed during the session."""
    return pd.Timestamp(time_et).time() if time_et else None


def earnings_window(announcements: Sequence[date], sessions: Sequence[date]) -> set[date]:
    """Every session within `EARNINGS_WINDOW` of an announcement.

    Measured in sessions, not calendar days: the day before a Monday
    announcement is the previous Friday.
    """
    ordered = np.array(sessions, dtype="object")
    flagged: set[date] = set()
    for announcement in announcements:
        at = int(np.searchsorted(ordered, announcement, side="left"))
        if at >= len(ordered) or ordered[at] != announcement:
            continue  # not a session we hold; nothing to flag
        low = max(0, at - EARNINGS_WINDOW)
        flagged.update(ordered[low : at + EARNINGS_WINDOW + 1])
    return flagged


def trusted_sessions(
    announcements: Sequence[date], sessions: Sequence[date], stale_after: int
) -> set[date]:
    """The sessions where "no announcement" can be asserted.

    A quarterly reporter files about every 63 sessions, so a session near
    one of its filings sits inside a working record and a false there
    means something. A session far from any filing does not: the record
    may simply have stopped.

    That is not hypothetical. Item 2.02 is the *designated* code for
    results, but it is not the only one companies use - Urban Outfitters
    has filed its releases under item 8.01 since November 2016, Energy
    Fuels since March 2016, DISH since January 2023. Their 2.02 record
    goes quiet while the company keeps reporting, and every day after it
    would otherwise read a confident false (LEDGER-4, decision 7).

    A session is trusted when a filing lies within `stale_after` sessions
    **either side** of it. Looking both ways handles the three shapes the
    same way: before a company's first filing, after its last, and a hole
    in the middle.
    """
    ordered = np.array(sessions, dtype="object")
    trusted: set[date] = set()
    for announcement in announcements:
        at = int(np.searchsorted(ordered, announcement, side="left"))
        low = max(0, at - stale_after)
        trusted.update(ordered[low : at + stale_after + 1])
    return trusted


def option_expiries(sessions: Sequence[date]) -> set[date]:
    """Monthly option expiry: the third Friday of each month, or the last
    session before it when that Friday is a holiday (Good Friday, most
    often).

    A month whose expiry falls outside `sessions` contributes nothing,
    which is the wanted behaviour: we only flag sessions we hold.
    """
    ordered = np.array(sessions, dtype="object")
    expiries: set[date] = set()
    for year, month in sorted({(session.year, session.month) for session in sessions}):
        friday = third_friday(year, month)
        at = int(np.searchsorted(ordered, friday, side="right")) - 1
        if at >= 0 and ordered[at] >= date(year, month, 1):
            expiries.add(ordered[at])
    return expiries


def third_friday(year: int, month: int) -> date:
    """The third Friday of a month."""
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def is_triple_witching(session: date, expiries: set[date]) -> bool:
    """Quarterly triple witching: the option expiry of a quarter-end
    month (March, June, September, December)."""
    return session.month in (3, 6, 9, 12) and session in expiries


def calendar_flags(sessions: Sequence[date], half_days: set[date]) -> pd.DataFrame:
    """Section 6's calendar events, one row per session.

    These apply to every security, so they are computed once for the
    session rather than once per ticker-day.

    The first and last sessions carry an unknown where the answer depends
    on a neighbour we were not given: the first cannot know whether a
    holiday preceded it, the last cannot know whether the month turned
    after it. Pass a range wider than the one you need, or accept the two
    unknowns honestly.
    """
    ordered = list(sessions)
    expiries = option_expiries(ordered)
    rows = []
    for at, session in enumerate(ordered):
        following = ordered[at + 1] if at + 1 < len(ordered) else None
        previous = ordered[at - 1] if at else None
        turns_month = None if following is None else following.month != session.month
        rows.append(
            {
                "date": session,
                "option_expiry": session in expiries,
                "triple_witching": is_triple_witching(session, expiries),
                "month_end": turns_month,
                "quarter_end": (
                    None if turns_month is None else turns_month and session.month in (3, 6, 9, 12)
                ),
                "half_day": session in half_days,
                "day_after_holiday": (
                    None if previous is None else weekdays_between(previous, session) > 0
                ),
            }
        )
    frame = pd.DataFrame(rows, columns=["date", *CALENDAR_FLAGS])
    for column in CALENDAR_FLAGS:
        frame[column] = frame[column].astype("boolean")
    return frame


def weekdays_between(previous: date, session: date) -> int:
    """Weekdays the market was shut between two consecutive sessions.

    Zero over a normal weekend; one or more means a holiday, which is
    what Section 6's "day after a market holiday" is about.
    """
    return sum(
        1
        for n in range(1, (session - previous).days)
        if (previous + timedelta(days=n)).weekday() < 5
    )


def dated_flag(days: Sequence[date], flagged: set[date]) -> pd.Series:
    """A flag true on the given sessions - for events with a known date,
    like an ex-dividend or a split."""
    return pd.Series([day in flagged for day in days], dtype="boolean")


def unknown_flag(days: Sequence[date]) -> pd.Series:
    """A flag with no source: unknown on every day, never false."""
    return pd.Series([pd.NA] * len(days), dtype="boolean")


def any_event(flags: pd.DataFrame) -> pd.Series:
    """True if any observable flag is true; unknown if any is unknown;
    false only when every one of them is known to be false.

    The middle case is the point: "no known event" may only be asserted
    where everything observable was actually checked. The two flags with
    no source at all (`UNAVAILABLE_FLAGS`) are excluded, because a column
    that is unknown for everyone always would make this unknown always -
    see the module docstring.
    """
    present = [column for column in OBSERVABLE_FLAGS if column in flags.columns]
    if not present:
        raise ValueError("No Section 6 flags to combine")
    values = flags[present]
    combined = np.where(
        values.eq(True).any(axis=1), True, np.where(values.isna().any(axis=1), pd.NA, False)
    )
    return pd.Series(combined, index=flags.index, dtype="boolean")
