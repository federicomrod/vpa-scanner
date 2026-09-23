"""The daily Pattern C watch email.

**Everything this sends is unvalidated.** Pattern C was pre-registered on
2026-09-23 and has not been tested against history. Families A and B
have been: A was retired and B failed Section 13.1 on the strict fold
reading (LEDGER-9). Nothing here is evidence that Pattern C works.

It exists so the project owner can watch it live while Sections 12 and
13 run their course, and **every email says so, at the top, every time**.

There is no trading here and never will be. The email lists what the
filter picked and what to look at. It contains no recommendation, no
position size, no entry, and no broker anywhere near it.

### Failing loudly

If any step fails the email says **SCAN UNAVAILABLE** with the error,
rather than a short list that looks like a quiet day (CLAUDE.md).
A quiet day and a broken scan must never look alike.

    uv run python -m vpa.data.scan --session 2026-09-18
"""

from __future__ import annotations

import argparse
import glob
import logging
import sys
import traceback
from datetime import date
from pathlib import Path

import pandas as pd

from vpa.config import load_config
from vpa.data.bars import HOURLY, derived_path
from vpa.data.events import events_path, stored_sessions
from vpa.data.ingest import setup_logging
from vpa.data.raw_store import DEFAULT_DATA_ROOT
from vpa.delivery.email import EmailSender, build_email_sender
from vpa.signal.candidates import candidates

log = logging.getLogger("vpa.scan")

UNVALIDATED = "UNVALIDATED - Pattern C has not been tested. See below."

SUBJECT = "VPA watch {session} - {count} unvalidated Pattern C candidates"
UNAVAILABLE_SUBJECT = "VPA watch {session} - SCAN UNAVAILABLE"

HEADER = """{rule}
{banner}
{rule}

Pattern C was written down on 2026-09-23 and has NOT been tested against
history. It may well be worthless. Of the two patterns that have been
tested, one was retired and the other did not pass.

These are bars the filter picked out. That is all they are.
Inspect the chart - no trade recommendation.

{rule}

Session: {session}
Candidates: {count}   (C-up {ups}, C-down {downs})
"""

FOOTER = """
How to read this
----------------
C-up    repeated heavy trading at support that will not break, closing strong
C-down  repeated heavy trading at resistance that will not clear, closing weak

Both mean the same underlying thing: effort without result, repeated
over several bars, at a price level that matters.

Event flags are shown where anything was happening that day.
"no known event" means the flags we can observe did not fire.
It does not mean nothing happened: trading halts and index
changes have no source at all, so we never see them.

This email is information for a person to read. It is not advice, and
nothing in this project places a trade.
"""


def latest_session(data_root: Path) -> date:
    sessions = stored_sessions(data_root)
    if not sessions:
        raise FileNotFoundError(f"No sessions stored under {data_root}")
    return sessions[-1]


def scan(data_root: Path, session: date) -> pd.DataFrame:
    """Pattern C's picks for one session, with the context to read them."""
    parts = glob.glob(
        str(data_root / "derived/features/hourly" / f"date={session}" / "shard-*.parquet")
    )
    if not parts:
        raise FileNotFoundError(f"No features stored for {session}")
    features = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    prices = pd.read_parquet(
        derived_path(data_root, HOURLY, session),
        columns=["security_key", "slot_index", "open", "high", "low", "close"],
    )
    marked = candidates(features.merge(prices, on=["security_key", "slot_index"], how="left"))

    firing = marked[marked["family_c_up"] | marked["family_c_down"]].copy()
    if firing.empty:
        return firing
    firing["direction"] = ["C-up" if up else "C-down" for up in firing["family_c_up"]]
    return firing.merge(_event_notes(data_root, session), on="security_key", how="left")


def _event_notes(data_root: Path, session: date) -> pd.DataFrame:
    """A plain-language note about what was happening to each security."""
    path = events_path(data_root, session)
    if not path.exists():
        return pd.DataFrame(columns=["security_key", "events"])
    rows = pd.read_parquet(path)
    flags = [c for c in rows.columns if c not in ("security_key", "requested_ticker", "date")]
    notes = []
    for row in rows.itertuples(index=False):
        firing = [
            f.replace("_", " ") for f in flags if f != "any_event" and getattr(row, f) is True
        ]
        notes.append(", ".join(firing) if firing else "no known event")
    return pd.DataFrame({"security_key": rows["security_key"], "events": notes})


def render(found: pd.DataFrame, session: date) -> str:
    """The email body. The warning goes first, every time."""
    rule = "=" * 68
    ups = int(found["direction"].eq("C-up").sum()) if not found.empty else 0
    downs = int(found["direction"].eq("C-down").sum()) if not found.empty else 0
    body = HEADER.format(
        rule=rule, banner=UNVALIDATED, session=session, count=len(found), ups=ups, downs=downs
    )
    if found.empty:
        body += "\nNothing fired today. The scan ran and found nothing.\n"
        return body + FOOTER

    body += "\n"
    for row in found.sort_values(["direction", "requested_ticker"]).itertuples(index=False):
        body += (
            f"\n{row.requested_ticker:<8} {row.direction:<7} "
            f"hour ending slot {int(row.slot_index)}\n"
            f"         volume {row.vol_pct_slot_60:.0f}th percentile for this hour, "
            f"{int(row.repeat_hv_narrow_5)} of the last 5 bars busy and narrow\n"
            f"         the stretch moved {abs(row.progress_5):.2f} ATR net, "
            f"closing at {row.close_loc:.0%} of the bar's range\n"
            f"         {getattr(row, 'events', 'no known event')}\n"
        )
    return body + FOOTER


def unavailable(session: date, error: Exception) -> str:
    """What goes out when the scan could not be trusted to have run."""
    return (
        f"{'=' * 68}\nSCAN UNAVAILABLE\n{'=' * 68}\n\n"
        f"The scan for {session} did not complete, so no list was produced.\n"
        f"This is not a quiet day - it is a broken scan.\n\n"
        f"{type(error).__name__}: {error}\n\n"
        f"{traceback.format_exc()}"
    )


def run(data_root: Path, session: date | None, sender: EmailSender) -> int:
    """Scan, and send either the list or SCAN UNAVAILABLE."""
    chosen = session
    try:
        chosen = session or latest_session(data_root)
        found = scan(data_root, chosen)
        sender.send(
            subject=SUBJECT.format(session=chosen, count=len(found)),
            body=render(found, chosen),
        )
        log.info("Sent %d Pattern C candidates for %s", len(found), chosen)
        return 0
    except Exception as exc:
        log.exception("SCAN FAILED for %s", chosen)
        try:
            sender.send(
                subject=UNAVAILABLE_SUBJECT.format(session=chosen),
                body=unavailable(chosen or "unknown session", exc),
            )
        except Exception:
            log.exception("Could not even send the failure notice")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.scan",
        description="Email the day's unvalidated Pattern C candidates.",
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--session", type=date.fromisoformat)
    parser.add_argument("--print", action="store_true", help="Print instead of sending")
    args = parser.parse_args(argv)

    setup_logging(args.data_root, "scan")
    if args.print:
        chosen = args.session or latest_session(args.data_root)
        print(render(scan(args.data_root, chosen), chosen))
        return 0
    return run(args.data_root, args.session, build_email_sender(load_config().email))


if __name__ == "__main__":
    sys.exit(main())
