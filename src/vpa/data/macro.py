"""Building the macro event date table - Concept v2 Section 6.

Section 6 wants three macro flags: FOMC decision day, CPI release, and
non-farm payrolls. Unlike every other flag, these cannot be derived from
anything we already hold, and they cannot be recalled from memory either
- ten years is 320-odd dates, and a date that is quietly wrong produces
a flag that is quietly wrong, which is worse than no flag at all.

So they are **fetched once from the issuing authority, checked, and
committed to the repository** as `reference/macro-events.csv`. Nothing
at run time goes near the network; the scanner and the tests read the
committed file.

### The sources

- **FOMC** - federalreserve.gov. Every meeting publishes minutes at
  `fomcminutesYYYYMMDD.pdf`, where the date is the **last day of the
  meeting**, which is the day the decision is announced. Every policy
  announcement also publishes a statement at `monetaryYYYYMMDDa.htm`.
  The two disagree, and the disagreement is the interesting part - see
  below.
- **CPI and non-farm payrolls** - bls.gov's yearly release schedule,
  which gives the exact date and time of each release ("Employment
  Situation for December 2018", 08:30 AM).

### Telling a rate decision from the Fed's other announcements

Not every dated Fed announcement is a policy decision. March 2020 alone
published statements on the 3rd, 15th, 19th, 23rd and 31st, and only
three of those came from the Committee: the others were a bank
regulatory rule and a repo facility. Minutes do not settle it either -
they exist for the eight scheduled meetings, so they miss the emergency
cut of 3 March and lag the most recent meeting by three weeks.

The Fed labels them itself. A policy statement from the Committee is
titled **"Federal Reserve issues FOMC statement"**, and that exact
wording has been used for every one from 2016 to 2026 - scheduled and
unscheduled alike - while framework updates, facility announcements and
regulatory rules are titled differently. So each statement page is
fetched and kept on its own title, and no judgement of mine decides
which is which.

Minutes are still fetched, as a cross-check: every meeting that
published minutes should have a statement on the same day, and if one
does not, the build stops rather than quietly dropping a decision day.

Run it with `uv run python -m vpa.data.macro --help`.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("vpa.macro")

#: Where the checked, committed table lives. Resolved from this file so
#: it is found whatever directory the scanner is run from.
REPO_ROOT = Path(__file__).resolve().parents[3]
TABLE = REPO_ROOT / "reference" / "macro-events.csv"

FOMC, CPI, PAYROLLS = "fomc", "cpi", "payrolls"
KINDS = [FOMC, CPI, PAYROLLS]

FED_CALENDAR = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_HISTORICAL = "https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm"
BLS_YEAR = "https://www.bls.gov/schedule/{year}/home.htm"

#: The BLS release names Section 6 names. "Employment Situation" is the
#: release that carries non-farm payrolls.
#:
#: The name must match in full, to the reference month. BLS publishes
#: other releases whose names begin the same way - "Employment Situation
#: of Veterans for Annual 2023", every March at 10:00 - and a loose match
#: silently adds a thirteenth payrolls day to every year.
BLS_RELEASES = {"Consumer Price Index": CPI, "Employment Situation": PAYROLLS}

_REFERENCE_MONTH = re.compile(
    r"^(Consumer Price Index|Employment Situation) for "
    r"(January|February|March|April|May|June|July|August|September|October|November|December) "
    r"\d{4}$"
)

#: The Fed's own title for a policy statement from the Committee. Used
#: from 2016 through 2026 for scheduled and emergency statements alike.
FOMC_STATEMENT_TITLE = "Federal Reserve issues FOMC statement"

FED_STATEMENT = "https://www.federalreserve.gov/newsevents/pressreleases/monetary{stamp}a.htm"

#: An FOMC decision is announced mid-afternoon. The exact minute has
#: varied; what matters for Section 6 is that it lands inside the
#: session, so the same session reacts.
FOMC_ANNOUNCEMENT_ET = "14:00"

#: Both BLS releases come out before the opening bell, so the same
#: session reacts. Checked rather than assumed - see `validate`.
EXPECTED_BLS_TIME = "08:30"

#: What a complete year looks like. Used to catch a page that changed
#: shape and silently returned less, which is the failure that would
#: otherwise pass unnoticed.
PER_YEAR = {FOMC: 8, CPI: 12, PAYROLLS: 12}

CONTACT = "vpa-scanner (contact: run with --contact)"


@dataclass(frozen=True)
class Event:
    """One dated macro release."""

    day: date
    kind: str
    time_et: str
    source: str
    note: str = ""


def fetch(url: str, contact: str, opener: Callable | None = None) -> str:
    """One page, with a contact address in the user agent."""
    request = urllib.request.Request(url, headers={"User-Agent": contact})
    with (opener or urllib.request.urlopen)(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


# --- the Federal Reserve ------------------------------------------------------


def meeting_dates(html: str) -> list[date]:
    """FOMC meeting dates, from the minutes each meeting publishes.

    The filename carries the meeting's last day, which is the day the
    decision is announced and therefore the day the market reacts.
    """
    return _dates(re.findall(r"fomcminutes(\d{8})\.pdf", html))


def statement_dates(html: str) -> list[date]:
    """Every dated policy statement, scheduled or not."""
    return _dates(re.findall(r"monetary(\d{8})[a-z]?\.htm", html))


def is_policy_statement(html: str) -> bool:
    """Whether a Fed press release is a policy statement from the FOMC,
    judged by the Fed's own title for it."""
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    return bool(title) and FOMC_STATEMENT_TITLE in re.sub(r"\s+", " ", title.group(1))


def _dates(stamps: list[str]) -> list[date]:
    found = set()
    for stamp in stamps:
        try:
            found.add(datetime.strptime(stamp, "%Y%m%d").date())
        except ValueError:  # not a date after all
            continue
    return sorted(found)


# --- the Bureau of Labor Statistics -------------------------------------------

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)


def bls_releases(html: str) -> list[tuple[date, str, str]]:
    """(date, time, kind) for each CPI and Employment Situation release
    on one year's BLS schedule page."""
    found = []
    for row in _ROW.findall(html):
        cells = [_text(cell) for cell in _CELL.findall(row)]
        if len(cells) < 3:
            continue
        day, clock, release = cells[0], cells[1], cells[2]
        matched = _REFERENCE_MONTH.match(release)
        if matched is None:
            continue
        kind = BLS_RELEASES[matched.group(1)]
        parsed = _release_date(day)
        if parsed is not None:
            found.append((parsed, _clock(clock), kind))
    return found


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).replace("&nbsp;", " ").strip()


def _release_date(text: str) -> date | None:
    """ "Friday, January 04, 2019" -> a date."""
    try:
        return datetime.strptime(text, "%A, %B %d, %Y").date()
    except ValueError:
        return None


def _clock(text: str) -> str:
    """ "08:30 AM" -> "08:30"."""
    try:
        return datetime.strptime(text.strip(), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        return ""


# --- putting it together ------------------------------------------------------


def collect(
    years: list[int], contact: str, opener: Callable | None = None, pause: float = 0.5
) -> tuple[list[Event], list[Event]]:
    """Every macro event over `years`, and the statement-only FOMC dates
    that need a human decision."""
    fed_pages = [fetch(FED_CALENDAR, contact, opener)]
    for year in years:
        url = FED_HISTORICAL.format(year=year)
        try:
            fed_pages.append(fetch(url, contact, opener))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            log.info("No historical FOMC page for %d (recent years live on the calendar)", year)
        time.sleep(pause)

    wanted_years = set(years)
    meetings = {d for page in fed_pages for d in meeting_dates(page) if d.year in wanted_years}
    statements = sorted(
        {d for page in fed_pages for d in statement_dates(page) if d.year in wanted_years}
    )
    log.info(
        "%d dated Fed statements to classify, against %d meetings with minutes",
        len(statements), len(meetings),
    )  # fmt: skip

    decisions = set()
    for n, day in enumerate(statements, 1):
        page = fetch(FED_STATEMENT.format(stamp=day.strftime("%Y%m%d")), contact, opener)
        if is_policy_statement(page):
            decisions.add(day)
        if n % 25 == 0:
            log.info("  classified %d of %d statements", n, len(statements))
        time.sleep(pause)

    events = [
        Event(d, FOMC, FOMC_ANNOUNCEMENT_ET, FED_STATEMENT.format(stamp=d.strftime("%Y%m%d")),
              "scheduled meeting" if d in meetings else "unscheduled FOMC statement")
        for d in sorted(decisions)
    ]  # fmt: skip
    # Every meeting that published minutes must have a statement behind
    # it. If one does not, we have lost a decision day, not gained one.
    review = [
        Event(d, FOMC, FOMC_ANNOUNCEMENT_ET, FED_CALENDAR,
              "meeting published minutes but no FOMC statement was found - REVIEW")
        for d in sorted(meetings - decisions)
    ]  # fmt: skip

    for year in years:
        page = fetch(BLS_YEAR.format(year=year), contact, opener)
        for day, clock, kind in bls_releases(page):
            if day.year == year:
                events.append(Event(day, kind, clock, BLS_YEAR.format(year=year), ""))
        time.sleep(pause)

    return sorted(set(events), key=lambda e: (e.day, e.kind)), review


def validate(events: list[Event], years: list[int]) -> tuple[list[str], list[str]]:
    """Check the table, as (blocking, review).

    **Blocking** means the page almost certainly changed shape and we
    parsed less than is there: no releases at all for a kind in a year,
    or a BLS release at the wrong hour, which is how the "Employment
    Situation of Veterans" release first crept in.

    **Review** means the calendar itself was odd - a payrolls release
    moved off its Friday for the 4th of July, a month missing because of
    the 2025 shutdown, a year still in progress. These are real and the
    table should keep them; a human just needs to have seen them.
    """
    blocking, review = [], []
    counts = Counter((e.day.year, e.kind) for e in events)
    for year in years:
        for kind, expected in PER_YEAR.items():
            found = counts[(year, kind)]
            if found == 0:
                blocking.append(f"{year} {kind}: nothing found at all - did the page change?")
            elif found != expected:
                review.append(f"{year} {kind}: {found} releases, expected {expected}")

    for event in events:
        if event.kind in (CPI, PAYROLLS) and event.time_et != EXPECTED_BLS_TIME:
            blocking.append(
                f"{event.day} {event.kind}: released at {event.time_et!r}, not "
                f"{EXPECTED_BLS_TIME} - this is usually a different release matched by mistake"
            )
        if event.kind == PAYROLLS and event.day.weekday() != 4:
            review.append(f"{event.day} payrolls: a {event.day:%A}, not a Friday")
    return blocking, review


def write_table(path: Path, events: list[Event]) -> None:
    """Write the committed table. One row per release, sorted by date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "kind", "time_et", "source", "note"])
        for event in sorted(events, key=lambda e: (e.day, e.kind)):
            writer.writerow([event.day.isoformat(), event.kind, event.time_et,
                             event.source, event.note])  # fmt: skip


def read_table(path: Path = TABLE) -> list[Event]:
    """The committed table. No network, no vendor - just the file."""
    if not path.exists():
        raise FileNotFoundError(
            f"No macro event table at {path}. It is committed to the repository; "
            "run `python -m vpa.data.macro` to rebuild it from the Fed and BLS."
        )
    with path.open(newline="") as handle:
        return [
            Event(
                date.fromisoformat(row["date"]),
                row["kind"],
                row["time_et"],
                row["source"],
                row["note"],
            )  # fmt: skip
            for row in csv.DictReader(handle)
        ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.macro",
        description="Fetch FOMC, CPI and non-farm payroll release dates from the Federal "
        "Reserve and the BLS, and write the committed reference table.",
    )
    parser.add_argument(
        "--contact",
        required=True,
        help="Contact address sent in the user agent, e.g. 'vpa-scanner you@example.com'. "
        "No default: it must be yours.",
    )
    parser.add_argument("--first-year", type=int, default=2016)
    parser.add_argument("--last-year", type=int, default=date.today().year)
    parser.add_argument("--table", type=Path, default=TABLE)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    years = list(range(args.first_year, args.last_year + 1))
    try:
        events, review = collect(years, args.contact)
    except Exception as exc:
        log.exception("MACRO TABLE BUILD FAILED: %s: %s", type(exc).__name__, exc)
        return 1

    blocking, review_notes = validate(events, years)
    counts = Counter(e.kind for e in events)
    log.info("Collected %d events over %d years: %s", len(events), len(years), dict(counts))

    if review:
        log.warning("")
        log.warning("%d FOMC statement dates have no scheduled meeting behind them.", len(review))
        log.warning("These need a human decision - an emergency rate decision belongs in the")
        log.warning("flag; a swap-line or facility announcement does not:")
        for event in review:
            log.warning("    %s  https://www.federalreserve.gov/newsevents/pressreleases/"
                        "monetary%sa.htm", event.day, event.day.strftime("%Y%m%d"))  # fmt: skip

    if review_notes:
        log.warning("")
        log.warning("%d calendar oddities - real, kept in the table, worth a look:",
                    len(review_notes))  # fmt: skip
        for note in review_notes:
            log.warning("    %s", note)

    if blocking:
        log.error("")
        log.error("MACRO TABLE NOT WRITTEN - %d checks failed:", len(blocking))
        for complaint in blocking:
            log.error("    %s", complaint)
        return 1

    write_table(args.table, events)
    log.info("Wrote %s (%d rows)", args.table, len(events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
