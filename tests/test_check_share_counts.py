"""Tests for the share-count diagnostic (src/vpa/data/check_share_counts.py).
FAKE vendor data, temporary folders, no network."""

from __future__ import annotations

import csv
from datetime import date

import pandas as pd

from tests.fakes import FakeMassiveApi
from vpa.data import check_share_counts as sc
from vpa.data.massive import MassiveClient
from vpa.data.raw_store import write_part

DAY = date(2024, 9, 17)


def client_for(api) -> MassiveClient:
    return MassiveClient("FAKE-KEY", session=api, requests_per_second=1e9, sleep=lambda s: None)


def write_problems(tmp_path, rows: list[dict]) -> None:
    path = tmp_path / "logs" / "share_count_problems.csv"
    path.parent.mkdir(parents=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_reports_which_fields_the_vendor_has():
    api = FakeMassiveApi()
    api.overviews_on[("FAKEA", DAY.isoformat())] = {
        "type": "CS",
        "share_class_shares_outstanding": 2e8,
        "market_cap": 1e10,
    }  # no weighted count
    api.closes[("FAKEA", DAY.isoformat())] = 50.0
    row = sc.share_fields(client_for(api), "FAKEA", DAY)
    assert row["found"] is True
    assert row["weighted_shares"] is None
    assert row["share_class_shares"] == 2e8
    assert row["shares_from_market_cap"] == 2e8  # 1e10 / 50


def test_reports_when_both_counts_exist_and_how_they_compare():
    api = FakeMassiveApi()
    api.overviews_on[("FAKEB", DAY.isoformat())] = {
        "type": "CS",
        "weighted_shares_outstanding": 1e8,
        "share_class_shares_outstanding": 9e7,
    }
    row = sc.share_fields(client_for(api), "FAKEB", DAY)
    assert row["share_class_over_weighted"] == 0.9


def test_a_ticker_the_vendor_does_not_know_is_reported_not_fatal():
    api = FakeMassiveApi()
    api.overviews_on[("FAKEGONE", DAY.isoformat())] = None  # 404
    assert sc.share_fields(client_for(api), "FAKEGONE", DAY) == {
        "ticker": "FAKEGONE",
        "date": DAY,
        "found": False,
    }


def test_cases_are_taken_from_the_log_and_spread_out(tmp_path):
    write_problems(
        tmp_path,
        [
            {
                "shares_date": f"2024-0{1 + i // 3}-1{i % 3}",
                "ticker_on_date": f"FAKE{i}",
                "status": "missing" if i % 2 == 0 else "not_found",
            }
            for i in range(9)
        ],  # fmt: skip
    )
    cases = sc.missing_cases(tmp_path, sample=2)
    assert len(cases) == 2
    assert all(t in {"FAKE0", "FAKE2", "FAKE4", "FAKE6", "FAKE8"} for t, _ in cases)


def test_comparison_cases_come_from_stored_share_counts(tmp_path):
    for day, ticker in (("2024-09-17", "FAKEOK"), ("2024-10-18", "FAKEOK2")):
        write_part(
            tmp_path, "share_counts", f"date={day}", f"run-{day}",
            pd.DataFrame([
                {"key": "F1", "ticker_on_date": ticker, "weighted_shares": 1e8, "status": "found"},
                {"key": "F2", "ticker_on_date": "FAKEBAD", "weighted_shares": None,
                 "status": "missing"},
            ]),
            {},
        )  # fmt: skip
    cases = sc.working_cases(tmp_path, sample=5)
    assert ("FAKEOK", date(2024, 9, 17)) in cases
    assert all(t != "FAKEBAD" for t, _ in cases)
