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
from datetime import UTC, date, datetime, timedelta
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
EIGHT_K_DATASET = "eight_k_filings"

#: Items that carry news a company chose to put out, as opposed to a
#: required disclosure. 7.01 is Reg FD, 8.01 is "Other Events" - the two
#: a results release turns up under when it is not filed as 2.02.
VOLUNTARY_ITEMS = ("7.01", "8.01")

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
    return [f for f in eight_k_filings(submissions) if EARNINGS_ITEM in f["items"].split(",")]


def eight_k_filings(submissions: dict) -> list[dict]:
    """**Every** 8-K, whatever it reports.

    Item 2.02 is the designated code for results, but companies are not
    obliged to use it. Abercrombie filed its January 2025 sales update -
    the one the stock gapped 15% on - under item 7.01 at 07:10 ET, and
    our earnings flag read false for that day. Keeping every 8-K lets the
    size of that hole be measured instead of guessed (LEDGER-4).
    """
    filings = submissions.get("filings", {}).get("recent", submissions)
    forms = filings.get("form") or []
    rows = []
    for position, form in enumerate(forms):
        if form != "8-K":
            continue
        rows.append(
            {
                "accession_number": filings["accessionNumber"][position],
                "filing_date": date.fromisoformat(filings["filingDate"][position]),
                "accepted_utc": filings["acceptanceDateTime"][position],
                "items": filings.get("items", [])[position] or "",
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
    extract: Callable[[dict], list[dict]] = earnings_filings,
    dataset: str = FILINGS_DATASET,
) -> pd.DataFrame:
    """Every filing of interest for each security, from EDGAR.

    `extract` decides what counts: `earnings_filings` for item 2.02 only,
    `eight_k_filings` for every 8-K.
    """
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
        found = extract(submissions)
        for page in additional_pages(submissions):
            sleep(interval)
            found += extract(
                fetch_json(f"https://data.sec.gov/submissions/{page}", contact, opener)
            )
        for filing in found:
            rows.append({"security_key": security.security_key, "ticker": security.ticker,
                         "cik": cik, **filing})  # fmt: skip
        if dataset == FILINGS_DATASET:
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
        data_root, dataset, f"fetched={datetime.now(UTC).date()}", run_id, filings,
        {"dataset": dataset, "securities": len(with_cik)},
    )  # fmt: skip
    log.info("Stored %d filings for %d securities in %s", len(filings), len(with_cik), dataset)
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


def stored_ciks(data_root: Path) -> pd.DataFrame:
    """The CIKs resolved by an earlier run.

    Reusing them means a second download needs nothing but EDGAR - no
    vendor key, no vendor requests.
    """
    stored = read_partition(data_root, CIK_DATASET, "all")
    if stored.empty:
        raise FileNotFoundError(
            f"No CIKs stored in {data_root}. Run this without --all-items first, "
            "which resolves them from the vendor's point-in-time ticker details."
        )
    return stored


def missed_announcements(data_root: Path) -> pd.DataFrame:
    """8-Ks filed under a voluntary item that no results filing covers.

    An upper bound on what the earnings flag misses, not a count of
    missed earnings: items 7.01 and 8.01 also carry buybacks, dividend
    declarations and conference appearances. What the count does say is
    how much company news the flag is silent about (LEDGER-4).
    """
    every = read_dataset(data_root, EIGHT_K_DATASET)
    if every.empty:
        raise FileNotFoundError(
            f"No {EIGHT_K_DATASET} stored in {data_root}. Run with --all-items first."
        )
    every["day"] = pd.to_datetime(every["filing_date"]).dt.date
    voluntary = every[
        every["items"].apply(lambda i: any(v in str(i).split(",") for v in VOLUNTARY_ITEMS))
    ].copy()
    results = every[every["items"].apply(lambda i: EARNINGS_ITEM in str(i).split(","))]
    covered = {(k, d) for k, d in zip(results["security_key"], results["day"], strict=True)}
    # A results release and its 8-K exhibits sometimes land a day apart,
    # so a filing touching either neighbouring day counts as covered.
    near = voluntary.apply(
        lambda row: any(
            (row["security_key"], row["day"] + timedelta(days=offset)) in covered
            for offset in (-1, 0, 1)
        ),
        axis=1,
    )
    return voluntary[~near] if len(voluntary) else voluntary


def read_dataset(data_root: Path, dataset: str) -> pd.DataFrame:
    folder = data_root / "raw" / dataset
    if not folder.exists():
        return pd.DataFrame()
    parts = [read_partition(data_root, dataset, p.name) for p in sorted(folder.iterdir())]
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def report_item_codes(data_root: Path) -> None:
    """How much company news the item-2.02 flag is silent about."""
    every = read_dataset(data_root, EIGHT_K_DATASET)
    missed = missed_announcements(data_root)
    eastern = pd.to_datetime(missed["accepted_utc"], utc=True, format="mixed").dt.tz_convert(
        "America/New_York"
    )
    before_open = (eastern.dt.time < pd.Timestamp("09:30").time()).sum()
    after_close = (eastern.dt.time >= pd.Timestamp("16:00").time()).sum()

    log.info("=" * 70)
    log.info("WHAT THE ITEM-2.02 EARNINGS FLAG IS SILENT ABOUT")
    log.info("  8-Ks on record:                    %s", f"{len(every):,}")
    log.info("  under item 7.01 or 8.01, with no")
    log.info("  results filing within a day:       %s", f"{len(missed):,}")
    log.info("  ...across securities:              %d", missed["security_key"].nunique())
    log.info("")
    log.info("  Of those, filed outside market hours - the shape of a")
    log.info("  news release rather than routine housekeeping:")
    log.info("    before the open:  %s (%.0f%%)", f"{before_open:,}",
             100 * before_open / max(len(missed), 1))  # fmt: skip
    log.info("    after the close:  %s (%.0f%%)", f"{after_close:,}",
             100 * after_close / max(len(missed), 1))  # fmt: skip
    log.info("")
    log.info("  This is an upper bound on missed announcements, not a count")
    log.info("  of missed earnings: these items also carry buybacks, dividend")
    log.info("  declarations and conference appearances.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.edgar",
        description="Download filing dates from SEC EDGAR for every universe member: "
        "results announcements (8-K item 2.02) by default, or every 8-K with --all-items.",
    )
    parser.add_argument(
        "--contact",
        required=True,
        help="Contact address sent to the SEC, which their fair-access policy requires "
        "(e.g. 'vpa-scanner you@example.com'). No default: it must be yours.",
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    parser.add_argument(
        "--all-items",
        action="store_true",
        help="Store every 8-K, not only results announcements, reusing the CIKs already "
        "resolved. Needs no vendor key.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Report how much company news the item-2.02 flag is silent about, and stop",
    )
    args = parser.parse_args(argv)

    setup_logging(args.data_root, "edgar")
    try:
        if args.report:
            report_item_codes(args.data_root)
            return 0
        run_id = new_run_id()
        if args.all_items:
            with_cik = stored_ciks(args.data_root)
            log.info("Reusing %d stored CIKs; no vendor requests needed", len(with_cik))
            download(args.data_root, with_cik, args.contact, run_id,
                     extract=eight_k_filings, dataset=EIGHT_K_DATASET)  # fmt: skip
            return 0
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
