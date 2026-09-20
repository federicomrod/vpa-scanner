"""Check: is the vendor's historical market cap free of look-ahead?

The universe (Concept v2 Section 2) filters on market cap "as of" each
rebalance date, using the vendor's figure. The vendor documents that
figure as "the most recent close price multiplied by weighted outstanding
shares". If, for a past date, "most recent close" means *today's* price,
every historical universe would be chosen with knowledge of the future.

This check asks for the market cap on several past dates (under the
ticker the company used on each date - see `vpa.data.tickers`) and divides it by
the share count returned alongside it, giving the price the vendor used.
That price is compared with the actual close on that date, the close the
trading day before, and the latest close.

It only reports. It never adjusts or works around anything: if it finds
look-ahead, the universe design goes back to the project owner.

Run it with `uv run python -m vpa.data.check_market_cap --help`.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from vpa.data.calendar import previous_session, sessions_ending
from vpa.data.ingest import TEST_RUN_TICKERS, make_client, resolve_identity, setup_logging
from vpa.data.massive import MassiveClient, MassiveError, results_object, ticker_path
from vpa.data.raw_store import DEFAULT_DATA_ROOT
from vpa.data.secrets import DEFAULT_SECRETS_FILE
from vpa.data.tickers import segments

log = logging.getLogger("vpa.check_market_cap")

#: Two prices within 0.5% of each other are treated as the same price.
TOLERANCE = 0.005

POINT_IN_TIME = "OK: uses the close on that date"
POINT_IN_TIME_PREVIOUS = "OK: uses the previous day's close"
LOOK_AHEAD = "LOOK-AHEAD: uses the latest close"
CANNOT_TELL = "CANNOT TELL: price barely moved since"
UNCLEAR = "UNCLEAR: matches none of the closes"
MISSING = "MISSING: vendor returned no market cap or share count"
NOT_FOUND = "NOT FOUND: vendor has no record of this ticker on this date"


def verdict(
    implied: float | None,
    close_on_date: float | None,
    close_previous: float | None,
    close_latest: float | None,
) -> str:
    """Classify which close the vendor used to compute its market cap."""
    if implied is None:
        return MISSING

    def same(a: float | None, b: float | None) -> bool:
        return a is not None and b is not None and abs(a / b - 1) <= TOLERANCE

    historical = same(implied, close_on_date) or same(implied, close_previous)
    if historical and same(implied, close_latest):
        return CANNOT_TELL
    if same(implied, close_on_date):
        return POINT_IN_TIME
    if same(implied, close_previous):
        return POINT_IN_TIME_PREVIOUS
    if same(implied, close_latest):
        return LOOK_AHEAD
    return UNCLEAR


def is_acceptable(result: str) -> bool:
    return result in (POINT_IN_TIME, POINT_IN_TIME_PREVIOUS)


def daily_close(client: MassiveClient, ticker: str, day: date) -> float | None:
    """The unadjusted vendor daily close for `ticker` on `day`."""
    response = client.get(
        f"/v2/aggs/ticker/{ticker_path(ticker)}/range/1/day/"
        f"{day.isoformat()}/{day.isoformat()}",
        {"adjusted": "false"},
    )
    results = response.get("results") or []
    return results[0]["c"] if results else None


def check_one(
    client: MassiveClient, ticker: str, day: date, latest: date, latest_ticker: str | None = None
) -> dict:
    """Check one ticker on one date. `ticker` is the symbol in use on
    `day`; `latest_ticker` the symbol on `latest` (if it has changed)."""
    latest_ticker = latest_ticker or ticker
    row = {"ticker": latest_ticker, "ticker_on_date": ticker, "date": day}
    try:
        response = client.get(
            f"/v3/reference/tickers/{ticker_path(ticker)}", {"date": day.isoformat()}
        )
    except MassiveError as exc:
        if exc.status_code != 404:
            raise
        return {**row, "verdict": NOT_FOUND}
    overview = results_object(response, f"ticker details for {ticker}")
    market_cap = overview.get("market_cap")
    shares = overview.get("weighted_shares_outstanding")
    implied = market_cap / shares if market_cap and shares else None
    closes = {
        "close_on_date": daily_close(client, ticker, day),
        "close_previous": daily_close(client, ticker, previous_session(day)),
        "close_latest": daily_close(client, latest_ticker, latest),
    }
    return {
        **row,
        "market_cap": market_cap,
        "weighted_shares": shares,
        "implied_price": implied,
        **closes,
        "verdict": verdict(implied, **closes),
    }


def check_ticker(client: MassiveClient, ticker: str, days: list[date], latest: date) -> list[dict]:
    """Check `ticker` (a symbol valid on `latest`) on each of `days`,
    following any renames back in time."""
    identity = resolve_identity(client, ticker, latest)
    return [
        check_one(client, segments(identity, day, day)[0].ticker, day, latest, ticker)
        for day in days
    ]


def default_check_dates(latest: date) -> list[date]:
    """The latest session, plus roughly 1, 2 and 3 years (252 trading
    days each) before it - far enough back that prices will have moved."""
    back = sessions_ending(latest, 3 * 252 + 1)
    return [back[0], back[252], back[504], latest]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vpa.data.check_market_cap",
        description="Check whether the vendor's historical market cap uses future prices.",
    )
    parser.add_argument("--tickers", default=",".join(TEST_RUN_TICKERS))
    parser.add_argument(
        "--dates",
        help="Comma-separated YYYY-MM-DD dates. Default: latest session, and ~1/2/3 years back.",
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--secrets-file", type=Path, default=DEFAULT_SECRETS_FILE)
    args = parser.parse_args(argv)

    latest = previous_session(date.today())
    days = (
        [date.fromisoformat(d) for d in args.dates.split(",")]
        if args.dates
        else default_check_dates(latest)
    )
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    run_log = setup_logging(args.data_root, "check-market-cap")
    try:
        client = make_client(args.secrets_file, requests_per_second=10.0)
        results = pd.DataFrame(
            [row for t in tickers for row in check_ticker(client, t, days, latest)]
        )
    except Exception as exc:
        log.exception("MARKET CAP CHECK FAILED: %s: %s", type(exc).__name__, exc)
        return 1

    out = args.data_root / "logs" / f"market-cap-check-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.csv"
    results.to_csv(out, index=False)
    with pd.option_context("display.width", 200, "display.max_columns", None):
        log.info(
            "\n%s",
            results.drop(columns=["market_cap", "weighted_shares"], errors="ignore").round(2),
        )
    counts = results["verdict"].value_counts()
    log.info("Verdicts:\n%s", counts.to_string())
    log.info("Full results: %s   Log: %s", out, run_log)

    bad = results[~results["verdict"].map(is_acceptable) & (results["date"] != latest)]
    if not bad.empty:
        log.error(
            "STOP: %d historical check(s) did not clearly use the price on that date. "
            "Do not build universes from vendor market cap until the owner has reviewed this.",
            len(bad),
        )
        return 2
    log.info("PASS: every historical market cap used the price on (or just before) that date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
