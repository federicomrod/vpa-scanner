"""Tests for building universe snapshots from vendor data
(src/vpa/data/build_universe.py, src/vpa/data/universe_inputs.py).

A small FAKE market is served by FakeMassiveApi (tests/fakes.py) and
everything is written to a temporary folder. No network, no real data.
"""

from __future__ import annotations

import csv
import logging
from datetime import date

import pandas as pd
import pytest

from tests.fakes import FakeMassiveApi
from vpa.data.build_universe import rebalance_dates, run
from vpa.data.calendar import sessions_ending
from vpa.data.massive import MassiveClient
from vpa.data.universe import read_snapshot

SEPT = date(2025, 9, 2)  # measured at the Fri 29 Aug close
OCT = date(2025, 10, 1)  # measured at the Tue 30 Sep close
SEPT_SHARES = "2025-05-16"  # 29 Aug minus 105 days
OCT_SHARES = "2025-06-17"  # 30 Sep minus 105 days
RENAME = date(2025, 8, 15)  # FAKEOLDB became FAKEB
DAYS = sessions_ending(date(2025, 9, 30), 90)

# ticker -> (type, exchange, close, volume, shares). Shares are chosen so
# market cap = shares x close.
STOCKS = {
    "FAKEA": ("CS", "XNYS", 50.0, 1e6, 2e8),  # $10bn - selected
    "FAKEB": ("CS", "XNAS", 20.0, 1e6, 5e8),  # $10bn, renamed mid-window - selected
    "FAKEETF": ("ETF", "XNYS", 50.0, 1e6, 2e8),  # not common stock
    "FAKEBIG": ("CS", "XNYS", 100.0, 1e6, 1e9),  # $100bn - too big
    "FAKENOSH": ("CS", "XNYS", 50.0, 1e6, None),  # no share count on record
    "FAKETHIN": ("CS", "XNYS", 50.0, 1e4, 2e8),  # far too little trading
}


def fake_market() -> FakeMassiveApi:
    api = FakeMassiveApi()
    for day in DAYS:
        rows = []
        for ticker, (_, _, close, volume, _) in STOCKS.items():
            symbol = "FAKEOLDB" if ticker == "FAKEB" and day < RENAME else ticker
            rows.append({"T": symbol, "o": close, "h": close, "l": close, "c": close,
                         "v": volume, "vw": close, "n": 10})  # fmt: skip
        api.grouped[day.isoformat()] = rows
    for as_of in ("2025-08-29", "2025-09-30"):
        api.listed[as_of] = [
            {"ticker": t, "type": typ, "primary_exchange": ex, "composite_figi": f"FIGI_{t}",
             "name": f"{t} Inc", "market": "stocks", "active": True}
            for t, (typ, ex, *_rest) in STOCKS.items()
        ]  # fmt: skip
    for ticker, (_, _, close, _, shares) in STOCKS.items():
        figi = f"FIGI_{ticker}"
        # The vendor's listing date for FAKEB reset on the rename.
        list_date = RENAME.isoformat() if ticker == "FAKEB" else "2015-01-02"
        api.overviews[ticker] = {"ticker": ticker, "composite_figi": figi,
                                 "name": f"{ticker} Inc", "list_date": list_date}  # fmt: skip
        api.events[figi] = [
            {"type": "ticker_change", "date": "2015-01-02", "ticker_change": {"ticker": ticker}}
        ]
        then = "FAKEOLDB" if ticker == "FAKEB" else ticker
        for shares_date in (SEPT_SHARES, OCT_SHARES):
            api.overviews_on[(then, shares_date)] = (
                {
                    "composite_figi": figi,
                    "share_class_shares_outstanding": shares,
                    "weighted_shares_outstanding": shares,
                }
                if shares
                else None
            )
        for as_of in ("2025-08-29", "2025-09-30"):
            api.overviews_on[(ticker, as_of)] = {
                **api.overviews[ticker],
                "market_cap": (shares or 0) * close,
            }
    api.events["FIGI_FAKEB"] = [
        {"type": "ticker_change", "date": "2015-01-02", "ticker_change": {"ticker": "FAKEOLDB"}},
        {"type": "ticker_change", "date": RENAME.isoformat(), "ticker_change": {"ticker": "FAKEB"}},
    ]
    return api


def client_for(api) -> MassiveClient:
    return MassiveClient("FAKE-KEY", session=api, requests_per_second=1e9, sleep=lambda s: None)


def build(api, tmp_path, months=(SEPT,)):
    return run(client_for(api), tmp_path, tmp_path / "universe", list(months))


def read_log(tmp_path, name) -> list[dict]:
    path = tmp_path / "logs" / name
    if not path.exists():
        return []
    with path.open() as handle:
        return list(csv.DictReader(handle))


def test_rebalance_dates_are_first_trading_days():
    assert rebalance_dates("2025-08", "2025-10") == [
        date(2025, 8, 1),
        date(2025, 9, 2),
        date(2025, 10, 1),
    ]


def test_builds_the_expected_universe(tmp_path):
    build(fake_market(), tmp_path)
    snapshot = read_snapshot(tmp_path / "universe", SEPT)
    assert sorted(snapshot["ticker"]) == ["FAKEA", "FAKEB"]
    assert set(snapshot["market_cap"]) == {10e9}


def test_renamed_stock_keeps_its_full_history(tmp_path):
    build(fake_market(), tmp_path)
    snapshot = read_snapshot(tmp_path / "universe", SEPT).set_index("ticker")
    # Without stitching, FAKEB would have only 10 of 60 days: median $0.
    assert snapshot.loc["FAKEB", "median_dollar_volume_60"] == 20e6
    # Listed since 2015 (first ticker event), not since the rename.
    assert snapshot.loc["FAKEB", "listing_date"] == date(2015, 1, 2)
    stitches = read_log(tmp_path, "ticker_stitches.csv")
    assert [(r["requested_ticker"], r["fetched_as"]) for r in stitches] == [("FAKEB", "FAKEOLDB")]


def test_share_counts_are_asked_under_the_ticker_used_105_days_earlier(tmp_path):
    api = fake_market()
    build(api, tmp_path)
    assert SEPT_SHARES in api.overview_calls("FAKEOLDB")
    assert SEPT_SHARES not in api.overview_calls("FAKEB")


def test_only_stocks_passing_the_cheap_checks_are_looked_up(tmp_path):
    api = fake_market()
    build(api, tmp_path)
    assert api.overview_calls("FAKEETF") == []
    assert api.overview_calls("FAKETHIN") == []


def test_audit_logs_record_every_discrepancy(tmp_path):
    build(fake_market(), tmp_path)
    listing = read_log(tmp_path, "listing_date_discrepancies.csv")
    assert [(r["ticker"], r["listing_date_used"]) for r in listing] == [("FAKEB", "2015-01-02")]
    problems = read_log(tmp_path, "share_count_problems.csv")
    assert [(r["ticker"], r["status"]) for r in problems] == [("FAKENOSH", "not_found")]
    audit = read_log(tmp_path, "market_cap_audit.csv")
    assert sorted(r["ticker"] for r in audit) == ["FAKEA", "FAKEB"]
    assert all(float(r["ratio"]) == 1.0 for r in audit)


def test_a_second_month_reuses_stored_data(tmp_path):
    api = fake_market()
    build(api, tmp_path, months=(SEPT, OCT))
    assert read_snapshot(tmp_path / "universe", OCT)["ticker"].tolist() == ["FAKEA", "FAKEB"]
    # Each whole-market day downloaded once, though the windows overlap.
    grouped = [u for u, _, _ in api.calls if "/grouped/" in u]
    assert len(grouped) == len(set(grouped))
    # Listing dates and ticker histories fetched once per security.
    assert len([u for u, _, _ in api.calls if "/events" in u]) == 4


def test_rerunning_skips_months_already_built(tmp_path):
    build(fake_market(), tmp_path)
    api = fake_market()
    build(api, tmp_path)
    assert not any("/grouped/" in u or "/v3/reference/tickers/" in u for u, _, _ in api.calls)


def test_a_day_with_no_market_data_stops_the_build(tmp_path):
    from vpa.data.massive import MassiveError

    api = fake_market()
    api.grouped[date(2025, 8, 20).isoformat()] = []
    with pytest.raises(MassiveError, match="no daily bars"):
        build(api, tmp_path)
    assert not (tmp_path / "universe").exists()


def test_a_stock_with_no_ticker_history_is_flagged_not_fatal(tmp_path, caplog):
    # The real API answers 404 "No events found" for such stocks.
    caplog.set_level(logging.INFO)
    api = fake_market()
    del api.events["FIGI_FAKEA"]
    build(api, tmp_path)
    assert "FAKEA" in read_snapshot(tmp_path / "universe", SEPT)["ticker"].tolist()
    assert "'no_events': 1" in caplog.text
    listing = {r["ticker"]: r for r in read_log(tmp_path, "listing_date_discrepancies.csv")}
    assert listing["FAKEA"]["first_ticker_event"] == ""


def test_new_securities_in_a_later_month_are_saved_too(tmp_path):
    # A stock first shortlisted in October means a second save of listing
    # info within one run - which must not collide with the first.
    api = fake_market()
    api.listed["2025-09-30"].append(
        {"ticker": "FAKEOCT", "type": "CS", "primary_exchange": "XNYS",
         "composite_figi": "FIGI_FAKEOCT", "name": "FAKEOCT Inc"}
    )  # fmt: skip
    for day in DAYS:
        api.grouped[day.isoformat()].append(
            {"T": "FAKEOCT", "o": 30.0, "h": 30.0, "l": 30.0, "c": 30.0, "v": 1e6, "n": 10}
        )
    api.overviews["FAKEOCT"] = {"composite_figi": "FIGI_FAKEOCT", "list_date": "2015-01-02"}
    api.overviews_on[("FAKEOCT", OCT_SHARES)] = {
        "composite_figi": "FIGI_FAKEOCT",
        "share_class_shares_outstanding": 2e8,
        "weighted_shares_outstanding": 2e8,
    }
    build(api, tmp_path, months=(SEPT, OCT))
    assert "FAKEOCT" in read_snapshot(tmp_path / "universe", OCT)["ticker"].tolist()
    saved = list((tmp_path / "raw" / "security_info").rglob("*.manifest.json"))
    assert len(saved) == 2


def test_stocks_without_usable_ticker_history_are_logged(tmp_path):
    api = fake_market()
    del api.events["FIGI_FAKEA"]
    build(api, tmp_path)
    gaps = read_log(tmp_path, "ticker_history_gaps.csv")
    assert [(r["ticker"], r["status"]) for r in gaps] == [("FAKEA", "no_events")]


def test_a_blank_symbol_in_the_ticker_history_does_not_stop_the_build(tmp_path, caplog):
    # Real case (Talen Energy): one ticker-change event has a blank symbol.
    caplog.set_level(logging.INFO)
    api = fake_market()
    api.events["FIGI_FAKEB"] = [
        {"type": "ticker_change", "date": "2015-01-02", "ticker_change": {"ticker": ""}},
        {"type": "ticker_change", "date": RENAME.isoformat(), "ticker_change": {"ticker": "FAKEB"}},
    ]
    build(api, tmp_path)
    assert "cannot join up FAKEB" in caplog.text
    gaps = read_log(tmp_path, "ticker_history_gaps.csv")
    assert [(r["ticker"], r["status"]) for r in gaps] == [("FAKEB", "invalid_events")]
    # Not stitched, so its short history keeps it out - flagged, never guessed.
    assert read_snapshot(tmp_path / "universe", SEPT)["ticker"].tolist() == ["FAKEA"]


def test_market_cap_uses_the_share_class_count_not_the_weighted_one(tmp_path):
    # The weighted count is frozen before 2022, so it must not be used.
    api = fake_market()
    api.overviews_on[("FAKEA", SEPT_SHARES)] = {
        "composite_figi": "FIGI_FAKEA",
        "share_class_shares_outstanding": 2e8,  # $10bn at $50 - qualifies
        "weighted_shares_outstanding": 1e9,  # $50bn+ - would be excluded
    }
    build(api, tmp_path)
    snapshot = read_snapshot(tmp_path / "universe", SEPT).set_index("ticker")
    assert snapshot.loc["FAKEA", "market_cap"] == 10e9


def test_companies_with_more_than_one_share_class_are_logged(tmp_path):
    # Both counts trustworthy (2022+) and different: two share classes.
    api = fake_market()
    for shares_date in (SEPT_SHARES, OCT_SHARES):
        api.overviews_on[("FAKEA", shares_date)] = {
            "composite_figi": "FIGI_FAKEA",
            "share_class_shares_outstanding": 2e8,
            "weighted_shares_outstanding": 3.2e8,
        }
    build(api, tmp_path)
    logged = read_log(tmp_path, "multi_class_securities.csv")
    assert [(r["ticker"], round(float(r["share_class_fraction"]), 3)) for r in logged] == [
        ("FAKEA", 0.625)
    ]


def test_rebuilding_keeps_the_previous_list_instead_of_deleting_it(tmp_path):
    build(fake_market(), tmp_path)
    first = read_snapshot(tmp_path / "universe", SEPT)["ticker"].tolist()

    # Without --rebuild the month is left alone; with it, the list is
    # written afresh and the old one is moved aside, never deleted.
    api = fake_market()
    run(client_for(api), tmp_path, tmp_path / "universe", [SEPT])
    assert not (tmp_path / "universe" / "superseded").exists()

    run(client_for(api), tmp_path, tmp_path / "universe", [SEPT], rebuild=True)
    kept = list((tmp_path / "universe" / "superseded").rglob("2025-09-02.parquet"))
    assert len(kept) == 1
    assert pd.read_parquet(kept[0])["ticker"].tolist() == first
    assert read_snapshot(tmp_path / "universe", SEPT)["ticker"].tolist() == first
