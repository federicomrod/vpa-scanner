"""Earnings announcement dates, from SEC EDGAR.

Concept v2 Section 6 wants every ticker-day to carry an earnings flag
(D-1, D0, D+1). No Massive plan includes earnings dates: they are sold
separately as a partner dataset. EDGAR has them for free, and is the
source those datasets are derived from.

**How an earnings date is identified.** Every 8-K filing carries item
codes saying what it reports. Item 2.02 is "Results of Operations and
Financial Condition" - the earnings filing. EDGAR's submissions API
lists every filing a company has made, with its item codes and the exact
moment the SEC accepted it.

Checked against real universe members before this was written: ANF and
OHI each show 40 such filings across the eleven years, exactly four a
year, and **companies that have since been acquired keep their full
history** - KSU's filings run to October 2021 and stop, Alexion's to
July 2021. That last point decided the choice: the universe was built
at some length to be free of survivorship bias, and a feed that thins
out for dead companies would have quietly put it back.

**Timing.** The acceptance timestamp says whether a release landed
before the open or after the close - ANF at 07:38 ET, OHI at 16:17 ET -
so the session that actually reacted can be identified rather than
guessed. A date-only feed cannot do that.

Recorded in LEDGER-4:

- **Item 2.02 is slightly broader than "quarterly earnings".** It also
  covers mid-quarter updates and pre-announcements - ANF's January sales
  update is one. For Section 6's purpose, flagging a day when a company
  published financial results is the point, so this is treated as a
  feature rather than trimmed; the flag means "results announcement".
- **CIK comes from Massive's ticker details**, which are point-in-time
  and follow renames and delistings. Mapping tickers to CIKs by hand is
  the one dangerous step here: the wrong CIK returns another company's
  filings, and they look perfectly plausible.

SEC's fair-access policy requires a declared contact address on every
request and no more than ten requests a second. Both are enforced here;
`--contact` is required and has no default.

Run it with `uv run python -m vpa.data.edgar --help`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from vpa.data.ingest import make_client, setup_logging
from vpa.data.massive import MassiveClient, MassiveError, results_object, ticker_path
from vpa.data.raw_store import DEFAULT_DATA_ROOT, new_run_id, read_partition, write_part
from vpa.data.secrets import DEFAULT_SECRETS_FILE

log = logging.getLogger("vpa.edgar")

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

#: The 8-K item that reports results (Section 6 / LEDGER-4).
EARNINGS_ITEM = "2.02"

#: SEC's fair-access limit is ten requests a second; stay under it.
REQUESTS_PER_SECOND = 8.0

CIK_DATASET = "security_cik"
FILINGS_DATASET = "earnings_filings"

#: A company reporting quarterly should show roughly four a year. Fewer
#: than this over its listed life means the mapping or the coverage
#: deserves a look, so it is logged rather than passed over.
EXPECTED_PER_YEAR = 3.0


def fetch_json(url: str, contact: str, opener: Callable | None = None) -> dict:
    """One EDGAR request, with the contact address their policy requires."""
    request = urllib.request.Request(url, headers={"User-Agent": contact})
    with (opener or urllib.request.urlopen)(request, timeout=60) as response:
        return json.load(response)


def earnings_filings(submissions: dict) -> list[dict]:
    """Every results announcement in one company's EDGAR submissions.

    `submissions` is the parsed JSON; only the block it carries is read,
    so additional pages are fetched by the caller and passed separately.
    """
    filings = submissions.get("filings", {}).get("recent", submissions)
    forms = filings.get("form") or []
    rows = []
    for position, form in enumerate(forms):
        items = filings.get("items", [])[position] or ""
        if form != "8-K" or EARNINGS_ITEM not in items.split(","):
            continue
        rows.append(
            {
                "accession_number": filings["accessionNumber"][position],
                "filing_date": date.fromisoformat(filings["filingDate"][position]),
                "accepted_utc": filings["acceptanceDateTime"][position],
                "items": items,
            }
        )
    return rows


def additional_pages(submissions: dict) -> list[str]:
    """Older filings live in separate files once a company has many."""
    return [f["name"] for f in submissions.get("filings", {}).get("files", [])]


def resolve_ciks(
    client: MassiveClient, securities: pd.DataFrame, run_id: str, data_root: Path
) -> pd.DataFrame:
    """Each security's SEC CIK, from Massive's point-in-time details.

    `securities` needs `security_key`, `ticker` and `as_of` - a date the
    ticker was valid, so renamed and delisted names resolve correctly.
    """
    stored = read_partition(data_root, CIK_DATASET, "all")
    known = set(stored["security_key"]) if not stored.empty else set()
    todo = securities[~securities["security_key"].isin(known)]
    if todo.empty:
        return stored

    rows = []
    for n, security in enumerate(todo.itertuples(index=False), 1):
        try:
            details = results_object(
                client.get(
                    f"/v3/reference/tickers/{ticker_path(security.ticker)}",
                    {"date": security.as_of.isoformat()},
                ),
                f"ticker details for {security.ticker}",
            )
        except MassiveError as exc:
            if exc.status_code != 404:
                raise
            details = {}
        cik = details.get("cik")
        rows.append(
            {
                "security_key": security.security_key,
                "ticker": security.ticker,
                "cik": int(cik) if cik else None,
                "name": details.get("name"),
            }
        )
        if n % 200 == 0:
            log.info("  CIKs resolved: %d of %d", n, len(todo))

    fetched = pd.DataFrame(rows)
    missing = int(fetched["cik"].isna().sum())
    if missing:
        log.warning("%d securities have no CIK on record and can have no earnings flag", missing)
    everything = pd.concat([stored, fetched], ignore_index=True) if not stored.empty else fetched
    write_part(data_root, CIK_DATASET, "all", run_id, fetched, {"dataset": CIK_DATASET})
    return everything


def download(
    data_root: Path,
    securities: pd.DataFrame,
    contact: str,
    run_id: str,
    opener: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    """Every results announcement for each security, from EDGAR."""
    with_cik = securities.dropna(subset=["cik"])
    log.info("Fetching filings for %d securities with a CIK", len(with_cik))
    rows, thin = [], []
    interval = 1.0 / REQUESTS_PER_SECOND

    for n, security in enumerate(with_cik.itertuples(index=False), 1):
        cik = int(security.cik)
        try:
            submissions = fetch_json(SUBMISSIONS_URL.format(cik=cik), contact, opener)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            log.warning("EDGAR has no submissions for %s (CIK %d)", security.ticker, cik)
            sleep(interval)
            continue
        found = earnings_filings(submissions)
        for page in additional_pages(submissions):
            sleep(interval)
            found += earnings_filings(
                fetch_json(f"https://data.sec.gov/submissions/{page}", contact, opener)
            )
        for filing in found:
            rows.append({"security_key": security.security_key, "ticker": security.ticker,
                         "cik": cik, **filing})  # fmt: skip
        _note_if_thin(found, security, thin)
        if n % 100 == 0:
            log.info("  %d of %d securities, %d filings so far", n, len(with_cik), len(rows))
        sleep(interval)

    filings = pd.DataFrame(
        rows,
        columns=["security_key", "ticker", "cik", "accession_number", "filing_date",
                 "accepted_utc", "items"],
    )  # fmt: skip
    if thin:
        log.warning(
            "%d securities report fewer than %.0f results announcements a year - "
            "check the CIK mapping for these: %s",
            len(thin), EXPECTED_PER_YEAR, ", ".join(t for t in thin[:10]),
        )  # fmt: skip
    write_part(
        data_root, FILINGS_DATASET, f"fetched={datetime.now(UTC).date()}", run_id, filings,
        {"dataset": FILINGS_DATASET, "securities": len(with_cik), "item": EARNINGS_ITEM},
    )  # fmt: skip
    log.info("Stored %d results announcements for %d securities", len(filings), len(with_cik))
    return filings


def _note_if_thin(found: list[dict], security, thin: list[str]) -> None:
    """Record a security whose filing rate looks too low to be quarterly."""
    if not found:
        thin.append(security.ticker)
        return
    dates = sorted(f["filing_date"] for f in found)
    years = max((dates[-1] - dates[0]).days / 365.25, 1.0)
    if len(found) / years < EXPECTED_PER_YEAR:
        thin.append(security.ticker)


def universe_securities(data_root: Path) -> pd.DataFrame:
    """Every security that has ever been in a universe snapshot, with a
    date its ticker was valid."""
    rows = []
    for snapshot in sorted((data_root / "universe").glob("????-??-??.parquet")):
        table = pd.read_parquet(snapshot, columns=["ticker", "composite_figi", "as_of_session"])
        rows.append(table)
    if not rows:
        raise FileNotFoundError(f"No universe snapshots in {data_root / 'universe'}")
    members = pd.concat(rows, ignore_index=True)
    members["security_key"] = [
        figi if isinstance(figi, str) and figi else f"TICKER:{ticker}"
        for ticker, figi in zip(members["ticker"], members["composite_figi"], strict=True)
    ]
    latest = members.sort_values("as_of_session").drop_duplicates("security_key", keep="last")
    return latest.rename(columns={"as_of_session": "as_of"})[["security_key", "ticker", "as_of"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.edgar",
        description="Download earnings announcement dates from SEC EDGAR (8-K item 2.02) "
        "for every universe member.",
    )
    parser.add_argument(
        "--contact",
        required=True,
        help="Contact address sent to the SEC, which their fair-access policy requires "
        "(e.g. 'vpa-scanner you@example.com'). No default: it must be yours.",
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    args = parser.parse_args(argv)

    setup_logging(args.data_root, "edgar")
    try:
        run_id = new_run_id()
        securities = universe_securities(args.data_root)
        log.info("Universe members ever selected: %d", len(securities))
        client = make_client(args.secrets_file, REQUESTS_PER_SECOND)
        with_cik = resolve_ciks(client, securities, run_id, args.data_root)
        download(args.data_root, with_cik, args.contact, run_id)
    except Exception as exc:
        log.exception("EDGAR DOWNLOAD FAILED: %s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
