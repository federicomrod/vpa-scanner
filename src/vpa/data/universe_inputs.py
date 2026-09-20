"""Downloading the whole-market inputs the monthly universe needs.

Concept v2 Section 2 picks ~400 stocks out of the entire US market, so
unlike `vpa.data.ingest` (which fetches the stocks we already chose) this
fetches data for *every* stock, but only at daily resolution:

- `daily_grouped`: one unadjusted daily bar for every US stock, per
  trading day - one API call per day.
- `tickers_listed`: every stock listed on a given day, including ones
  since delisted (keeps the universe free of survivorship bias).
- `splits_market`: every split in a date range.
- `security_info`: per security, fetched once and reused - the vendor's
  listing date, and its ticker-change history (LEDGER-1 amendment 2:
  listing date = the earlier of the two).
- `share_counts_v2`: per security and share-count date - the vendor's
  share-class shares outstanding (what market cap uses, LEDGER-1
  amendment 4) **and** its weighted count (kept for audit only, since it
  is frozen before 2022). Both are stored so a future change of field
  never needs another download.

All of it lands in the write-once raw store, and anything already stored
is reused rather than downloaded again.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from vpa.data.massive import MassiveClient, MassiveError, results_object, ticker_path
from vpa.data.raw_store import manifests, read_partition, write_part
from vpa.data.tickers import Identity, build_identity, fetch_ticker_events, segments

log = logging.getLogger("vpa.universe_inputs")

#: How many per-stock lookups run at once. The client still keeps the
#: overall request rate; this just stops one slow reply holding up the rest.
LOOKUP_THREADS = 8

#: Share counts are stored under their own dataset name: the earlier
#: `share_counts` holds only the weighted count and is kept as history.
SHARE_COUNTS_DATASET = "share_counts_v2"

SHARES_FOUND = "found"
SHARES_NOT_FOUND = "not_found"
SHARES_WRONG_SECURITY = "wrong_security"
SHARES_MISSING = "missing"


def _stored(data_root: Path, dataset: str, partition: str) -> bool:
    return bool(manifests(data_root, dataset, partition))


# --- whole-market daily bars and ticker lists --------------------------------


def ensure_grouped_daily(
    client: MassiveClient, data_root: Path, days: list[date], run_id: str
) -> None:
    """Download any missing whole-market daily bars for `days`."""
    missing = [d for d in days if not _stored(data_root, "daily_grouped", f"date={d}")]
    for n, day in enumerate(missing, 1):
        response = client.get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}", {"adjusted": "false"}
        )
        if response.get("adjusted") is not False:
            raise MassiveError(f"Daily bars for {day} did not come back unadjusted")
        rows = response.get("results") or []
        if not rows:
            raise MassiveError(f"Vendor returned no daily bars at all for trading day {day}")
        table = pd.DataFrame(rows).rename(
            columns={
                "T": "ticker",
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
                "vw": "vwap",
                "n": "transactions",
            }  # fmt: skip
        )
        table = table.drop(columns=["t", "otc"], errors="ignore").assign(date=day)
        write_part(
            data_root, "daily_grouped", f"date={day}", run_id, table,
            {"dataset": "daily_grouped", "date": day.isoformat(), "adjusted": False},
        )  # fmt: skip
        if n % 20 == 0 or n == len(missing):
            log.info("  whole-market daily bars: %d of %d days downloaded", n, len(missing))


def load_grouped_daily(data_root: Path, days: list[date]) -> pd.DataFrame:
    return pd.concat(
        [read_partition(data_root, "daily_grouped", f"date={d}") for d in days],
        ignore_index=True,
    )


def ensure_listed_tickers(
    client: MassiveClient, data_root: Path, day: date, run_id: str
) -> pd.DataFrame:
    """Every US stock listed on `day` (as the vendor saw it then)."""
    partition = f"date={day}"
    if not _stored(data_root, "tickers_listed", partition):
        rows = list(
            client.get_all(
                "/v3/reference/tickers",
                {"market": "stocks", "date": day.isoformat(), "active": "true", "limit": 1000},
            )
        )
        if not rows:
            raise MassiveError(f"Vendor returned no listed tickers for {day}")
        write_part(
            data_root, "tickers_listed", partition, run_id, pd.DataFrame(rows),
            {"dataset": "tickers_listed", "date": day.isoformat()},
        )  # fmt: skip
        log.info("  listed tickers on %s: %d", day, len(rows))
    listed = read_partition(data_root, "tickers_listed", partition)
    listed = listed.reindex(
        columns=list(
            dict.fromkeys([*listed.columns, "composite_figi", "name", "type", "primary_exchange"])
        )  # fmt: skip
    )
    if listed["ticker"].duplicated().any():
        raise MassiveError(f"Vendor listed a ticker twice on {day}")
    return listed


def fetch_market_splits(
    client: MassiveClient, data_root: Path, start: date, end: date, run_id: str
) -> pd.DataFrame:
    """Every split executed from `start` to `end`, stored under today's
    date (the vendor can add or correct splits later)."""
    rows = list(
        client.get_all(
            "/stocks/v1/splits",
            {
                "execution_date.gte": start.isoformat(),
                "execution_date.lte": end.isoformat(),
                "limit": 5000,
            },
        )  # fmt: skip
    )
    table = pd.DataFrame(
        rows, columns=["ticker", "id", "execution_date", "split_from", "split_to",
                       "adjustment_type", "historical_adjustment_factor"],
    )  # fmt: skip
    write_part(
        data_root, "splits_market", f"fetched={datetime.now(UTC).date()}", run_id, table,
        {"dataset": "splits_market", "from": start.isoformat(), "to": end.isoformat()},
    )  # fmt: skip
    log.info("  splits %s to %s: %d", start, end, len(table))
    return table


# --- per-security information ------------------------------------------------


@dataclass(frozen=True)
class SecurityInfo:
    key: str
    ticker: str
    composite_figi: str | None
    name: str | None
    list_date: date | None
    identity: Identity

    @property
    def first_event_date(self) -> date | None:
        return self.identity.changes[0].date if self.identity.changes else None


class SecurityInfoStore:
    """Listing dates and ticker histories, one lookup per security ever.

    Everything fetched is saved (dataset `security_info`) and reloaded on
    the next run, so a security is only ever looked up once.
    """

    def __init__(self, client: MassiveClient, data_root: Path) -> None:
        self._client = client
        self._data_root = data_root
        self._rows: dict[str, dict] = {}
        self._new: list[dict] = []
        self._saves = 0
        root = data_root / "raw" / "security_info"
        for folder in sorted(root.glob("*")) if root.exists() else []:
            for row in read_partition(data_root, "security_info", folder.name).to_dict("records"):
                self._rows[row["key"]] = row

    def get_many(self, listed: pd.DataFrame, as_of: date) -> dict[str, SecurityInfo]:
        """Info for each row of `listed` (ticker, composite_figi), keyed by ticker."""
        wanted = [(r.ticker, _figi(r.composite_figi)) for r in listed.itertuples()]
        todo = [(t, f) for t, f in wanted if _key(t, f, as_of) not in self._rows]
        with ThreadPoolExecutor(LOOKUP_THREADS) as pool:
            fetched = list(pool.map(lambda tf: self._fetch(*tf, as_of), todo))
        for row in fetched:
            self._rows[row["key"]] = row
            self._new.append(row)
        return {t: self._info(self._rows[_key(t, f, as_of)], t, as_of) for t, f in wanted}

    def save(self, run_id: str) -> None:
        """Store anything fetched since the last save, as a new part (one
        run can save several times, e.g. once per month)."""
        if self._new:
            self._saves += 1
            write_part(
                self._data_root, "security_info", f"fetched={datetime.now(UTC).date()}",
                f"{run_id}-{self._saves}", pd.DataFrame(self._new), {"dataset": "security_info"},
            )  # fmt: skip
            self._new = []

    def _fetch(self, ticker: str, figi: str | None, as_of: date) -> dict:
        try:
            overview = results_object(
                self._client.get(
                    f"/v3/reference/tickers/{ticker_path(ticker)}", {"date": as_of.isoformat()}
                ),
                f"ticker details for {ticker}",
            )
        except MassiveError as exc:
            if exc.status_code != 404:
                raise
            # Listed that day but no details on record: listing date unknown,
            # so the stock can't pass the history check. Recorded, not guessed.
            log.warning("No vendor details for %s on %s - listing date unknown", ticker, as_of)
            overview = {}
        events = fetch_ticker_events(self._client, figi) if figi else []
        return {
            "key": _key(ticker, figi, as_of),
            "ticker": ticker,
            "composite_figi": figi,
            "name": overview.get("name"),
            "list_date": overview.get("list_date"),
            "events": json.dumps(events),
            "fetched_for": as_of.isoformat(),
        }

    @staticmethod
    def _info(row: dict, ticker: str, as_of: date) -> SecurityInfo:
        events = json.loads(row["events"])
        identity = build_identity(ticker, as_of, row["composite_figi"], row["name"], events)
        list_date = row["list_date"]
        return SecurityInfo(
            key=row["key"],
            ticker=ticker,
            composite_figi=row["composite_figi"],
            name=row["name"],
            list_date=date.fromisoformat(list_date) if isinstance(list_date, str) else None,
            identity=identity,
        )


def _key(ticker: str, figi: str | None, as_of: date) -> str:
    """A security's permanent ID. Without one, a ticker alone isn't safe to
    reuse across months (symbols get reassigned), so it's keyed by month."""
    return figi or f"TICKER:{ticker}@{as_of.isoformat()}"


def _figi(value) -> str | None:
    return value if isinstance(value, str) and value else None


# --- share counts ------------------------------------------------------------


def fetch_share_counts(
    client: MassiveClient,
    data_root: Path,
    infos: list[SecurityInfo],
    shares_date: date,
    run_id: str,
) -> pd.DataFrame:
    """Share counts as of `shares_date` for each security, asked under the
    ticker it used on that date. Reuses stored answers.

    Stores both the share-class count (used for market cap) and the
    weighted count (audit only - see the module docstring).

    Never guesses: if the vendor doesn't know the ticker on that date, or
    answers about a different security, the share count is left empty
    (so the stock can't qualify) and the reason is recorded.
    """
    partition = f"date={shares_date}"
    stored = read_partition(data_root, SHARE_COUNTS_DATASET, partition)
    done = set(stored["key"]) if not stored.empty else set()
    todo = [i for i in infos if i.key not in done]

    def lookup(info: SecurityInfo) -> dict:
        ticker_then = segments(info.identity, shares_date, shares_date)[0].ticker
        row = {"key": info.key, "ticker": info.ticker, "ticker_on_date": ticker_then}
        try:
            overview = results_object(
                client.get(
                    f"/v3/reference/tickers/{ticker_path(ticker_then)}",
                    {"date": shares_date.isoformat()},
                ),
                f"ticker details for {ticker_then}",
            )
        except MassiveError as exc:
            if exc.status_code != 404:
                raise
            return {
                **row,
                "share_class_shares": None,
                "weighted_shares": None,
                "status": SHARES_NOT_FOUND,
            }
        figi_then = _figi(overview.get("composite_figi"))
        empty = {"share_class_shares": None, "weighted_shares": None}
        if info.composite_figi and figi_then and figi_then != info.composite_figi:
            return {**row, **empty, "status": SHARES_WRONG_SECURITY}
        share_class = overview.get("share_class_shares_outstanding")
        weighted = overview.get("weighted_shares_outstanding")
        return {
            **row,
            "share_class_shares": share_class or None,
            "weighted_shares": weighted or None,
            "status": SHARES_FOUND if share_class else SHARES_MISSING,
        }

    if todo:
        with ThreadPoolExecutor(LOOKUP_THREADS) as pool:
            fetched = pd.DataFrame(list(pool.map(lookup, todo)))
        for column in ("share_class_shares", "weighted_shares"):
            fetched[column] = fetched[column].astype("float64")
        write_part(
            data_root, SHARE_COUNTS_DATASET, partition, run_id, fetched,
            {"dataset": SHARE_COUNTS_DATASET, "shares_date": shares_date.isoformat()},
        )  # fmt: skip
        stored = pd.concat([stored, fetched], ignore_index=True) if not stored.empty else fetched
    wanted = {i.key for i in infos}
    return stored[stored["key"].isin(wanted)]
