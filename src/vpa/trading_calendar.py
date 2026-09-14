"""A best-effort NYSE trading-day calendar.

Used only to decide whether the scan should even attempt to run today -
this has nothing to do with the (still nonexistent) VPA signal logic,
and none of the dates here are a "trading threshold" in CLAUDE.md rule
8's sense.

This covers weekends and the standard annual NYSE holidays (including
the usual weekend-observed shifts). It does NOT know about one-off
closures - a national day of mourning, an emergency closure, etc. If
the market is ever closed for a reason not on this list, this will
incorrectly say it's a trading day. That's an acceptable gap for
Milestone 1 (no real trading happens either way, and a spurious run on
a closed day just means an extra, harmless "3 fake candidates" email);
if this ever matters for real, replace this with a proper market
calendar library.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """The nth occurrence (1-indexed) of `weekday` (Monday=0) in a month."""
    first_of_month = date(year, month, 1)
    first_weekday_offset = (weekday - first_of_month.weekday()) % 7
    return first_of_month + timedelta(days=first_weekday_offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """The last occurrence of `weekday` (Monday=0) in a month."""
    first_of_next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day_of_month = first_of_next_month - timedelta(days=1)
    offset = (last_day_of_month.weekday() - weekday) % 7
    return last_day_of_month - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """The date of Easter Sunday, via the standard "anonymous Gregorian"
    algorithm. Needed for Good Friday, a full NYSE closure."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(holiday: date) -> date:
    """A fixed-date holiday on a weekend is observed on the nearest
    weekday: Saturday -> the Friday before, Sunday -> the Monday after."""
    if holiday.weekday() == 5:  # Saturday
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:  # Sunday
        return holiday + timedelta(days=1)
    return holiday


def nyse_holidays(year: int) -> set[date]:
    """The standard annual NYSE holidays for `year`.

    Note: Juneteenth has only been an NYSE holiday since 2022 - this
    will (harmlessly) mark it as a holiday in earlier years too.
    """
    return {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday_of_month(year, 2, 0, 3),  # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday_of_month(year, 5, 0),  # Memorial Day
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),  # Labor Day
        _nth_weekday_of_month(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Christmas
    }


def is_trading_day(day: date) -> bool:
    """Whether the NYSE is (probably) open on `day` - see the module
    docstring for what this does and doesn't account for."""
    if day.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    # A weekend-observed shift can cross a year boundary - New Year's
    # Day observed on the preceding Friday is the one case here (e.g.
    # Jan 1, 2022 was a Saturday, observed Friday Dec 31, 2021, which
    # belongs to nyse_holidays(2022)'s output, not nyse_holidays(2021)'s
    # - so check the surrounding years too, not just day.year.
    holidays = nyse_holidays(day.year - 1) | nyse_holidays(day.year) | nyse_holidays(day.year + 1)
    return day not in holidays


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for scripts/run_scan.sh: exit 0 if `today` (or an
    explicit YYYY-MM-DD argument, mainly for manual testing) is a
    trading day, exit 1 otherwise."""
    args = sys.argv[1:] if argv is None else argv
    check_date = date.fromisoformat(args[0]) if args else date.today()
    return 0 if is_trading_day(check_date) else 1


if __name__ == "__main__":
    sys.exit(main())
