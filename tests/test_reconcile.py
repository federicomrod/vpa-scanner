"""Tests for the reconciliation report (src/vpa/data/reconcile.py).
FAKE bars in a temporary folder, no network."""

from __future__ import annotations

from datetime import date

import pandas as pd

from tests.test_bars import minutes_for, store_minutes
from vpa.data import reconcile
from vpa.data.bars import build
from vpa.data.raw_store import write_part

NORMAL = date(2025, 9, 2)
HALF = date(2025, 11, 28)
CASE = reconcile.Case("FAKEA", NORMAL, "Normal trading day")


def store_vendor_daily(tmp_path, day: date, row: dict) -> None:
    folder = tmp_path / "raw" / "daily_grouped" / f"date={day.isoformat()}"
    folder.mkdir(parents=True)
    pd.DataFrame([row]).to_parquet(folder / "part-run.parquet")


def prepared(tmp_path, day: date = NORMAL, **kwargs) -> None:
    store_minutes(tmp_path, day, minutes_for(day, **kwargs))
    build(tmp_path, [day])


def test_parse_cases():
    cases = reconcile.parse_cases("G:2025-12-02; NVDA:2024-06-10:split day, first one")
    assert cases[0] == reconcile.Case("G", date(2025, 12, 2), "")
    assert cases[1].why == "split day, first one"  # commas are fine in the reason


def test_default_cases_cover_what_section_3_3_asks_for():
    whys = " ".join(c.why.lower() for c in reconcile.DEFAULT_CASES)
    for kind in ("normal", "split", "ex-dividend", "gap", "half day"):
        assert kind in whys


def test_report_shows_every_hourly_bar_and_the_daily_bar(tmp_path):
    prepared(tmp_path)
    report = reconcile.report_case(tmp_path, CASE)
    assert "1-HOUR BARS, as traded" in report
    assert report.count("\n  0 ") == 1  # slot 0 row
    assert "09:30-10:30" in report and "15:30-16:00 (30m)" in report
    assert "normal day" in report
    assert "1-DAY BAR" in report and "ours" in report


def test_report_compares_our_daily_bar_with_the_vendors(tmp_path):
    prepared(tmp_path, price=50.0, volume=100)
    # Vendor's day includes after-hours: more volume, a higher high.
    store_vendor_daily(
        tmp_path, NORMAL,
        {"ticker": "FAKEA", "open": 50.0, "high": 55.0, "low": 49.0, "close": 50.5,
         "volume": 46800.0},
    )  # fmt: skip
    report = reconcile.report_case(tmp_path, CASE)
    assert "vendor" in report
    assert "-16.67%" in report  # our 39,000 vs vendor 46,800 volume
    assert "Section 4.2" in report


def test_report_flags_a_half_day(tmp_path):
    prepared(tmp_path, day=HALF, end="12:59")
    report = reconcile.report_case(tmp_path, reconcile.Case("FAKEA", HALF, "Half day"))
    assert "half day" in report
    assert "09:30-13:00 ET" in report
    assert "12:30-13:00 (30m)" in report


def test_report_lists_splits_and_dividends_near_the_date(tmp_path):
    prepared(tmp_path)
    write_part(
        tmp_path, "splits", "fetched=2025-09-10", "run-1",
        pd.DataFrame([{"requested_ticker": "FAKEA", "ticker": "FAKEA",
                       "execution_date": "2025-09-08", "split_from": 1, "split_to": 2}]),
        {},
    )  # fmt: skip
    write_part(
        tmp_path, "dividends", "fetched=2025-09-10", "run-1",
        pd.DataFrame([{"requested_ticker": "FAKEA", "ticker": "FAKEA",
                       "ex_dividend_date": "2025-09-04", "cash_amount": 0.26}]),
        {},
    )  # fmt: skip
    report = reconcile.report_case(tmp_path, CASE, today=date(2025, 9, 30))
    assert "SPLIT 2025-09-08: 1-for-2" in report
    assert "DIVIDEND ex-date 2025-09-04: 0.26 per share" in report
    # The split is after this date, so the TradingView-basis table is shown too.
    assert "adjusted to today's share basis" in report


def test_report_says_what_to_run_when_data_is_missing(tmp_path):
    missing = reconcile.report_case(tmp_path, CASE)
    assert "NO BARS BUILT" in missing and "vpa.data.bars" in missing

    prepared(tmp_path)
    other = reconcile.report_case(tmp_path, reconcile.Case("FAKEZZ", NORMAL, "not downloaded"))
    assert "NO MINUTE DATA STORED for FAKEZZ" in other
    assert "--tickers FAKEZZ" in other
