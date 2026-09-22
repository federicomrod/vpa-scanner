"""Building the Section 6 event table.

One row per security per session, carrying every flag Section 6 names.
Stored beside the bars and the features, and regenerable the same way:

    derived/events/date=YYYY-MM-DD/events.parquet

The rules all live in `vpa.signal.events`; this module only gathers the
facts they need and writes the answers out.

### Where each flag comes from

- **earnings** - SEC EDGAR 8-K item 2.02 filings (`vpa.data.edgar`),
  turned into the session that first traded on the announcement using
  the SEC's acceptance timestamp, then widened to D-1, D0, D+1.
- **ex_dividend**, **split** - the vendor's corporate-action tables,
  already downloaded for split adjustment.
- **index_change**, **halt** - no historical source exists; recorded as
  unknown for everyone, on every day (LEDGER-4).
- **the calendar flags** - derived from the sessions themselves.
- **fomc**, **cpi**, **payrolls** - `reference/macro-events.csv`, the
  checked table `vpa.data.macro` builds from the Federal Reserve and the
  BLS and commits to the repository. Nothing here touches the network.

### What "unknown" means here

The earnings flag is unknown, rather than false, in two situations.

**We hold no results filings for the security at all** - it has no CIK,
or it is a foreign private issuer, which files 6-K rather than 8-K and so
never produces the filing an earnings date comes from. 71 securities of
the 1,159 that have passed through the universe.

**Its filing record went quiet while it kept trading.** Item 2.02 is the
designated code for results, but companies are not obliged to use it:
Urban Outfitters has filed its releases under item 8.01 since November
2016, and it is not alone. Their 2.02 record simply stops, and every day
afterwards would otherwise read a confident false. A session is trusted
only when a filing lies within `STALE_AFTER_SESSIONS` either side of it
(LEDGER-4, decision 7).

`report_coverage` measures how stale the record is on each traded
session, so the size of this gap stays on record rather than assumed
small.

Run it with `uv run python -m vpa.data.events --help`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

from vpa.data.bars import DAILY, derived_path
from vpa.data.edgar import CIK_DATASET, FILINGS_DATASET
from vpa.data.ingest import setup_logging
from vpa.data.macro import read_table
from vpa.data.raw_store import DEFAULT_DATA_ROOT, read_partition
from vpa.signal.events import (
    ALL_FLAGS,
    CALENDAR_FLAGS,
    MACRO_FLAGS,
    STALE_AFTER_SESSIONS,
    UNAVAILABLE_FLAGS,
    any_event,
    calendar_flags,
    earnings_window,
    macro_flags,
    reacting_sessions,
    trusted_sessions,
)

log = logging.getLogger("vpa.events")

EVENTS = "events"

COLUMNS = ["security_key", "requested_ticker", "date", *ALL_FLAGS, "any_event"]


def events_path(data_root: Path, session: date) -> Path:
    return data_root / "derived" / EVENTS / f"date={session.isoformat()}" / "events.parquet"


# --- gathering the facts ------------------------------------------------------


def stored_sessions(data_root: Path) -> list[date]:
    """Every session with daily bars, in order."""
    folder = data_root / "derived" / DAILY
    if not folder.exists():
        return []
    return sorted(
        date.fromisoformat(path.name.removeprefix("date="))
        for path in folder.glob("date=*")
        if (path / "bars.parquet").exists()
    )


def session_securities(data_root: Path, session: date) -> pd.DataFrame:
    """The securities with bars on one session, and whether it was a half
    day for them."""
    return pd.read_parquet(
        derived_path(data_root, DAILY, session),
        columns=["security_key", "requested_ticker", "is_half_day"],
    ).drop_duplicates("security_key")


def load_dataset(data_root: Path, dataset: str) -> pd.DataFrame:
    """Every stored part of a raw dataset, concatenated."""
    folder = data_root / "raw" / dataset
    if not folder.exists():
        return pd.DataFrame()
    parts = [read_partition(data_root, dataset, p.name) for p in sorted(folder.iterdir())]
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def dated_events(table: pd.DataFrame, column: str) -> dict[str, set[date]]:
    """A corporate-action table as ticker -> the dates it happened on."""
    if table.empty:
        return {}
    days = pd.to_datetime(table[column], errors="coerce").dt.date
    by_ticker: dict[str, set[date]] = defaultdict(set)
    for ticker, day in zip(table["requested_ticker"], days, strict=True):
        if pd.notna(day):
            by_ticker[ticker].add(day)
    return dict(by_ticker)


def earnings_by_security(
    filings: pd.DataFrame, sessions: list[date], half_days: set[date] | None = None
) -> tuple[dict[str, set[date]], dict[str, set[date]]]:
    """Per security: the sessions its earnings flag is true on, and the
    sessions the flag can be trusted to be false on.

    A security missing from the second dictionary - or a session missing
    from its set - is unknown, not false. Two quite different things land
    there: a security we hold no filings for at all (a foreign private
    issuer), and a stretch where a security's filing record went quiet
    while it kept trading, which happens when a company reports under
    item 8.01 instead of 2.02 (LEDGER-4, decisions 4 and 7).
    """
    if filings.empty:
        return {}, {}
    reacting = reacting_sessions(filings["accepted_utc"], sessions, half_days)
    flagged: dict[str, set[date]] = {}
    trusted: dict[str, set[date]] = {}
    for key, announcements in reacting.groupby(filings["security_key"]):
        dated = [a for a in announcements if not pd.isna(a)]
        flagged[key] = earnings_window(dated, sessions)
        trusted[key] = trusted_sessions(dated, sessions, STALE_AFTER_SESSIONS)
    return flagged, trusted


# --- building -----------------------------------------------------------------


def build(data_root: Path, sessions: list[date], rebuild: bool = False) -> int:
    """Compute and store the event table for every session given."""
    started = time.monotonic()
    todo = [s for s in sessions if rebuild or not events_path(data_root, s).exists()]
    if not todo:
        log.info("Event table already built for all %d sessions", len(sessions))
        return 0

    filings = load_dataset(data_root, FILINGS_DATASET)
    ciks = load_dataset(data_root, CIK_DATASET)
    log.info(
        "Loaded %d results announcements for %d securities; %d securities have a CIK",
        len(filings), filings["security_key"].nunique() if not filings.empty else 0,
        int(ciks["cik"].notna().sum()) if not ciks.empty else 0,
    )  # fmt: skip

    half_days = half_days_in(data_root, sessions)
    earnings, trusted = earnings_by_security(filings, sessions, half_days)
    ex_dividends = dated_events(load_dataset(data_root, "dividends"), "ex_dividend_date")
    splits = dated_events(load_dataset(data_root, "splits"), "execution_date")
    log.info(
        "Earnings dates available for %d securities; ex-dividends for %d tickers, splits for %d",
        len(trusted), len(ex_dividends), len(splits),
    )  # fmt: skip

    calendar = calendar_flags(sessions, half_days).set_index("date")
    releases = [(e.day, e.kind, e.time_et) for e in read_table()]
    macro = macro_flags(sessions, releases, half_days).set_index("date")
    log.info(
        "Macro releases in the committed table: %d, of which %d land in these sessions",
        len(releases), int(macro[MACRO_FLAGS].any(axis=1).sum()),
    )  # fmt: skip
    calendar = calendar.join(macro)

    written = 0
    for n, session in enumerate(todo, 1):
        table = session_rows(
            data_root, session, calendar.loc[session], earnings, trusted, ex_dividends, splits
        )
        path = events_path(data_root, session)
        path.parent.mkdir(parents=True, exist_ok=True)
        table.to_parquet(path, index=False)
        written += len(table)
        if n % 250 == 0:
            log.info("  %d of %d sessions, %s rows", n, len(todo), f"{written:,}")

    log.info(
        "Wrote %s event rows over %d sessions in %.0fs",
        f"{written:,}", len(todo), time.monotonic() - started,
    )  # fmt: skip
    return written


def half_days_in(data_root: Path, sessions: list[date]) -> set[date]:
    """The half days among these sessions, as the bars recorded them."""
    half = set()
    for session in sessions:
        path = derived_path(data_root, DAILY, session)
        if path.exists() and bool(pd.read_parquet(path, columns=["is_half_day"]).any().iloc[0]):
            half.add(session)
    return half


def session_rows(
    data_root: Path,
    session: date,
    calendar: pd.Series,
    earnings: dict[str, set[date]],
    trusted: dict[str, set[date]],
    ex_dividends: dict[str, set[date]],
    splits: dict[str, set[date]],
) -> pd.DataFrame:
    """The event rows for one session."""
    securities = session_securities(data_root, session)
    rows = pd.DataFrame(
        {
            "security_key": securities["security_key"].to_numpy(),
            "requested_ticker": securities["requested_ticker"].to_numpy(),
            "date": session,
        }
    )
    rows["earnings"] = pd.array(
        [
            session in earnings.get(key, ()) if session in trusted.get(key, ()) else pd.NA
            for key in rows["security_key"]
        ],
        dtype="boolean",
    )
    rows["ex_dividend"] = pd.array(
        [session in ex_dividends.get(t, ()) for t in rows["requested_ticker"]], dtype="boolean"
    )
    rows["split"] = pd.array(
        [session in splits.get(t, ()) for t in rows["requested_ticker"]], dtype="boolean"
    )
    for flag in UNAVAILABLE_FLAGS:
        rows[flag] = pd.array([pd.NA] * len(rows), dtype="boolean")
    for flag in [*CALENDAR_FLAGS, *MACRO_FLAGS]:
        rows[flag] = pd.array([calendar[flag]] * len(rows), dtype="boolean")
    rows["any_event"] = any_event(rows)
    return rows[COLUMNS]


# --- checking what the earnings flag actually covers ---------------------------


#: A quarterly reporter files about every 63 sessions. Past twice that,
#: the filing record has a hole rather than an ordinary gap.
QUARTER_SESSIONS = 63
SUSPECT_GAP = 2 * QUARTER_SESSIONS


def report_coverage(data_root: Path, sessions: list[date]) -> pd.DataFrame:
    """How far each traded session is from the security's last filing.

    A security marked unknown is easy: we hold no filings for it at all.
    The case that would actually bite is a security whose filings *stop*
    while it keeps trading, because those days read a confident false.

    Counting "sessions after the last filing" alone does not find it: a
    quarterly reporter is always a month or two past its last filing, and
    every security is at the end of the store. So this measures the gap
    in sessions since the most recent filing on or before each session,
    which separates the ordinary quarterly rhythm from a real hole.
    """
    traded = traded_sessions(data_root, sessions)
    filings = load_dataset(data_root, FILINGS_DATASET)
    ordered = pd.Index(sessions)
    traded["session_index"] = ordered.get_indexer(traded["date"])

    if filings.empty:
        traded["state"] = "no filings at all"
        traded["gap_sessions"] = pd.NA
        return traded

    filed = pd.DataFrame(
        {
            "security_key": filings["security_key"].to_numpy(),
            # A filing can land on a non-session; count from the next one.
            "session_index": ordered.searchsorted(
                pd.to_datetime(filings["filing_date"]).dt.date.to_numpy(), side="left"
            ),
        }
    ).sort_values("session_index")

    parts = []
    for key, group in traded.groupby("security_key", sort=False):
        mine = filed.loc[filed["security_key"] == key, "session_index"].to_numpy()
        parts.append(_gaps_for(group, mine))
    return pd.concat(parts, ignore_index=True)


def _gaps_for(traded: pd.DataFrame, filed) -> pd.DataFrame:
    """One security's traded sessions, labelled by how stale its filings
    were on each."""
    traded = traded.copy()
    if len(filed) == 0:
        traded["gap_sessions"] = pd.array([pd.NA] * len(traded), dtype="Int64")
        traded["state"] = "no filings at all"
        return traded
    # How many filings had happened by each session, so the most recent
    # one is the entry just before that count.
    filed_by = filed.searchsorted(traded["session_index"].to_numpy(), side="right")
    gap = traded["session_index"].to_numpy() - filed[filed_by - 1]
    traded["gap_sessions"] = pd.array(
        [pd.NA if n == 0 else int(g) for n, g in zip(filed_by, gap, strict=True)], dtype="Int64"
    )
    traded["state"] = [
        "before first filing"
        if n == 0
        else "within a quarter of a filing"
        if g <= QUARTER_SESSIONS
        else "one to two quarters"
        if g <= SUSPECT_GAP
        else "over two quarters - suspect hole"
        for n, g in zip(filed_by, gap, strict=True)
    ]
    return traded


def traded_sessions(data_root: Path, sessions: list[date]) -> pd.DataFrame:
    """Every (session, security) pair with bars."""
    rows = []
    for session in sessions:
        path = derived_path(data_root, DAILY, session)
        if path.exists():
            keys = pd.read_parquet(path, columns=["security_key"])["security_key"].unique()
            rows.append(pd.DataFrame({"date": session, "security_key": keys}))
    if not rows:
        return pd.DataFrame(columns=["date", "security_key"])
    return pd.concat(rows, ignore_index=True)


# --- command line -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.events",
        description="Build the Section 6 event table, one row per security per session.",
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--first-session", type=date.fromisoformat)
    parser.add_argument("--last-session", type=date.fromisoformat)
    parser.add_argument("--rebuild", action="store_true", help="Recompute sessions already built")
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Report how much traded history the earnings flag can speak for, and stop",
    )
    args = parser.parse_args(argv)

    setup_logging(args.data_root, "events")
    try:
        sessions = stored_sessions(args.data_root)
        if not sessions:
            raise FileNotFoundError(f"No daily bars under {args.data_root / 'derived' / DAILY}")
        wanted = [
            s
            for s in sessions
            if (args.first_session is None or s >= args.first_session)
            and (args.last_session is None or s <= args.last_session)
        ]
        log.info("Sessions with bars: %d (%s to %s)", len(wanted), wanted[0], wanted[-1])
        if args.coverage:
            summarise_coverage(report_coverage(args.data_root, wanted))
            return 0
        build(args.data_root, wanted, rebuild=args.rebuild)
    except Exception as exc:
        log.exception("EVENT TABLE BUILD FAILED: %s: %s", type(exc).__name__, exc)
        return 1
    return 0


SUSPECT = "over two quarters - suspect hole"


def summarise_coverage(coverage: pd.DataFrame) -> None:
    total = len(coverage)
    log.info("=" * 70)
    log.info("EARNINGS FLAG COVERAGE over %s security-sessions", f"{total:,}")
    for state, count in coverage["state"].value_counts().items():
        log.info("  %-34s %12s  %5.2f%%", state, f"{count:,}", 100 * count / total)

    suspect = coverage[coverage["state"] == SUSPECT]
    log.info("")
    log.info(
        "Days reading a confident 'no earnings' while the filing record was "
        "more than two quarters stale: %s (%.2f%%), across %d securities",
        f"{len(suspect):,}", 100 * len(suspect) / total if total else 0,
        suspect["security_key"].nunique(),
    )  # fmt: skip
    if not suspect.empty:
        worst = suspect.groupby("security_key")["gap_sessions"].max().nlargest(10)
        log.info("  Longest gaps, in sessions:")
        for key, gap in worst.items():
            log.info("    %-22s %5d", key, gap)


if __name__ == "__main__":
    sys.exit(main())
