"""Tests for the raw store (src/vpa/data/raw_store.py), ingestion
(src/vpa/data/ingest.py) and the market-cap look-ahead check
(src/vpa/data/check_market_cap.py).

Everything runs against FakeMassiveApi (tests/fakes.py) serving FAKE data,
writing into a temporary folder - never the network, never ~/vpa-data.
"""

from __future__ import annotations

import csv
import os
from datetime import date

import pandas as pd
import pytest

from tests.fakes import FakeMassiveApi
from vpa.data import check_market_cap as mc
from vpa.data.calendar import sessions_between
from vpa.data.ingest import default_test_window, run
from vpa.data.massive import MassiveClient, MassiveError
from vpa.data.raw_store import (
    RawStoreError,
    manifests,
    partition_dir,
    read_partition,
    write_part,
)

# A window straddling a month end: Thu 28 Aug - Wed 3 Sep 2025 (Labor Day
# on Mon 1 Sep). Four trading days: 28 Aug, 29 Aug, 2 Sep, 3 Sep.
START, END = date(2025, 8, 28), date(2025, 9, 3)
SESSIONS = sessions_between(START, END)


def et_ms(day: date, hhmm: str) -> int:
    stamp = pd.Timestamp(f"{day} {hhmm}", tz="America/New_York")
    return int(stamp.tz_convert("UTC").timestamp() * 1000)


def fake_bars(days, price: float = 50.0) -> list[dict]:
    """Per day: a pre-market, an opening, a closing and an after-hours
    minute. The 19:59 ET bar is 23:59 UTC; 20:00 would be the next UTC day."""
    rows = []
    for day in days:
        for hhmm in ("07:00", "09:30", "15:59", "19:59"):
            rows.append(
                {
                    "t": et_ms(day, hhmm),
                    "o": price,
                    "h": price,
                    "l": price,
                    "c": price,
                    "v": 100,
                    "vw": price,
                    "n": 3,
                }  # fmt: skip
            )
    return rows


def fake_api() -> FakeMassiveApi:
    api = FakeMassiveApi()
    for n, ticker in enumerate(["FAKEA", "FAKEB"], 1):
        figi = f"FAKEFIGI000{n}"
        api.overviews[ticker] = {"ticker": ticker, "composite_figi": figi, "name": f"{ticker} Inc"}
        api.events[figi] = [
            {"type": "ticker_change", "date": "2015-01-02", "ticker_change": {"ticker": ticker}}
        ]
        api.minutes[ticker] = fake_bars(SESSIONS)
    api.splits["FAKEA"] = [
        {"ticker": "FAKEA", "id": "s1", "execution_date": "2020-06-01", "split_from": 1,
         "split_to": 4, "adjustment_type": "forward_split", "historical_adjustment_factor": 0.25}
    ]  # fmt: skip
    api.dividends["FAKEB"] = [
        {"ticker": "FAKEB", "id": "d1", "ex_dividend_date": "2025-08-29", "cash_amount": 0.5}
    ]
    return api


def client_for(api: FakeMassiveApi) -> MassiveClient:
    return MassiveClient(
        "FAKE-KEY", session=api, requests_per_second=1e9, sleep=lambda s: None, max_attempts=2
    )


def run_fake(api, tmp_path, tickers=("FAKEA", "FAKEB"), start=START, end=END):
    return run(client_for(api), tmp_path, list(tickers), start, end, reference_date=end)


def minute_day(tmp_path, day: date) -> pd.DataFrame:
    return read_partition(tmp_path, "minute", f"date={day.isoformat()}")


# --- raw store --------------------------------------------------------------


def test_parts_are_read_only_and_never_overwritten(tmp_path):
    table = pd.DataFrame({"x": [1, 2]})
    path = write_part(tmp_path, "demo", "date=2025-09-02", "run1", table, {"note": "FAKE"})
    assert not os.access(path, os.W_OK)
    with pytest.raises(RawStoreError, match="Refusing to overwrite"):
        write_part(tmp_path, "demo", "date=2025-09-02", "run1", table, {})
    [manifest] = manifests(tmp_path, "demo", "date=2025-09-02")
    assert manifest["rows"] == 2 and manifest["note"] == "FAKE" and len(manifest["sha256"]) == 64


def test_a_second_run_adds_a_part_alongside_the_first(tmp_path):
    write_part(tmp_path, "demo", "p", "run1", pd.DataFrame({"x": [1]}), {})
    write_part(tmp_path, "demo", "p", "run2", pd.DataFrame({"x": [2]}), {})
    assert sorted(read_partition(tmp_path, "demo", "p")["x"]) == [1, 2]


def test_a_changed_file_is_detected_by_its_checksum(tmp_path):
    path = write_part(tmp_path, "demo", "p", "run1", pd.DataFrame({"x": [1]}), {})
    path.chmod(0o644)
    pd.DataFrame({"x": [999]}).to_parquet(path)
    with pytest.raises(RawStoreError, match="Checksum mismatch"):
        read_partition(tmp_path, "demo", "p")


def test_a_part_without_a_manifest_is_ignored_and_reported(tmp_path, caplog):
    write_part(tmp_path, "demo", "p", "run1", pd.DataFrame({"x": [1]}), {})
    pd.DataFrame({"x": [2]}).to_parquet(partition_dir(tmp_path, "demo", "p") / "part-crash.parquet")
    assert list(read_partition(tmp_path, "demo", "p")["x"]) == [1]
    assert "no manifest" in caplog.text


# --- minute ingestion -------------------------------------------------------


def test_minute_bars_are_stored_unadjusted_one_partition_per_trading_day(tmp_path):
    run_fake(fake_api(), tmp_path)
    for day in SESSIONS:
        bars = minute_day(tmp_path, day)
        assert sorted(bars["ticker"].unique()) == ["FAKEA", "FAKEB"]
        # Pre-market and after-hours minutes are kept (Section 3.2).
        assert len(bars) == 8
        assert (bars["date_et"] == day).all()
        [manifest] = manifests(tmp_path, "minute", f"date={day.isoformat()}")
        assert manifest["adjusted"] is False
        assert set(manifest["securities"]) == {"FAKEFIGI0001", "FAKEFIGI0002"}
    assert not partition_dir(tmp_path, "minute", "date=2025-09-01").exists()  # Labor Day


def test_the_api_is_asked_for_unadjusted_data_one_month_at_a_time(tmp_path):
    api = fake_api()
    run_fake(api, tmp_path)
    minute_urls = api.minute_calls()
    assert len(minute_urls) == 4  # 2 tickers x 2 months
    assert any(u.endswith("/FAKEA/range/1/minute/2025-08-28/2025-08-29") for u in minute_urls)
    assert any(u.endswith("/FAKEA/range/1/minute/2025-09-02/2025-09-03") for u in minute_urls)
    params = [p for u, p, _ in api.calls if "/minute/" in u]
    assert all(p["adjusted"] == "false" for p in params)


def test_rerunning_downloads_nothing_already_stored(tmp_path):
    run_fake(fake_api(), tmp_path)
    api = fake_api()
    run_fake(api, tmp_path)
    assert api.minute_calls() == []
    assert len(manifests(tmp_path, "minute", "date=2025-09-02")) == 1


def test_adding_a_ticker_later_writes_a_new_part_for_just_that_ticker(tmp_path):
    run_fake(fake_api(), tmp_path, tickers=["FAKEA"])
    api = fake_api()
    run_fake(api, tmp_path)
    assert all("/FAKEB/" in u for u in api.minute_calls())
    day = manifests(tmp_path, "minute", "date=2025-09-02")
    assert [set(m["securities"]) for m in day] == [{"FAKEFIGI0001"}, {"FAKEFIGI0002"}]


def test_a_failure_writes_nothing_for_that_month_and_stops_the_run(tmp_path):
    api = fake_api()
    api.fail["FAKEB"] = 500
    with pytest.raises(MassiveError):
        run_fake(api, tmp_path)
    for day in SESSIONS:  # not even FAKEA's successful downloads are written
        assert not partition_dir(tmp_path, "minute", f"date={day.isoformat()}").exists()


def test_pages_are_followed_so_no_bars_are_lost(tmp_path):
    api = fake_api()
    api.page_size = 3
    run_fake(api, tmp_path)
    assert len(minute_day(tmp_path, date(2025, 8, 29))) == 8


def test_a_day_with_no_trades_is_recorded_as_covered_with_zero_bars(tmp_path, caplog):
    api = fake_api()
    api.minutes["FAKEB"] = fake_bars([d for d in SESSIONS if d != date(2025, 8, 29)])
    run_fake(api, tmp_path)
    [manifest] = manifests(tmp_path, "minute", "date=2025-08-29")
    assert manifest["securities"]["FAKEFIGI0002"]["bars"] == 0
    assert "FAKEB has NO bars on 1 trading day(s), first 2025-08-29" in caplog.text


# --- ticker changes ---------------------------------------------------------


def renamed_api() -> FakeMassiveApi:
    # FAKEB used to be FAKEOLDB, and was renamed on Tuesday 2 Sep 2025.
    api = fake_api()
    api.events["FAKEFIGI0002"] = [
        {"type": "ticker_change", "date": "2015-01-02", "ticker_change": {"ticker": "FAKEOLDB"}},
        {"type": "ticker_change", "date": "2025-09-02", "ticker_change": {"ticker": "FAKEB"}},
    ]
    api.minutes["FAKEOLDB"] = fake_bars(SESSIONS[:2], price=40)
    api.minutes["FAKEB"] = fake_bars(SESSIONS[2:])
    # A different company later reused the old symbol - must never be fetched
    # for dates after the rename.
    api.minutes["FAKEOLDB"] += fake_bars(SESSIONS[2:], price=1)
    return api


def test_history_is_stitched_across_a_rename(tmp_path):
    api = renamed_api()
    run_fake(api, tmp_path)
    before = minute_day(tmp_path, date(2025, 8, 29))
    after = minute_day(tmp_path, date(2025, 9, 2))
    old = before[before["security_key"] == "FAKEFIGI0002"]
    new = after[after["security_key"] == "FAKEFIGI0002"]
    assert set(old["ticker"]) == {"FAKEOLDB"} and set(old["close"]) == {40}
    assert set(new["ticker"]) == {"FAKEB"} and set(new["close"]) == {50}
    assert (old["requested_ticker"] == "FAKEB").all()
    assert not any("/FAKEOLDB/range/1/minute/2025-09" in u for u in api.minute_calls())


def test_every_stitch_is_written_to_the_audit_log(tmp_path):
    run_fake(renamed_api(), tmp_path)
    with (tmp_path / "logs" / "ticker_stitches.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["requested_ticker"] == "FAKEB"
    assert rows[0]["fetched_as"] == "FAKEOLDB"
    assert (rows[0]["start"], rows[0]["end"]) == ("2025-08-28", "2025-08-29")


def test_corporate_actions_are_fetched_under_every_symbol_used(tmp_path):
    api = renamed_api()
    run_fake(api, tmp_path)
    split_calls = [p["ticker"] for u, p, _ in api.calls if "/splits" in u]
    assert sorted(split_calls) == ["FAKEA", "FAKEB", "FAKEOLDB"]


def test_missing_figi_is_reported_and_not_stitched(tmp_path, caplog):
    api = renamed_api()
    api.overviews["FAKEB"]["composite_figi"] = None
    run_fake(api, tmp_path)
    assert "COVERAGE FAKEB (FAKEB Inc): no_figi" in caplog.text
    assert not any("/vX/" in u and "FAKEFIGI0002" in u for u, _, _ in api.calls)
    status = read_partition(tmp_path, "identity_status", _fetched_partition(tmp_path))
    assert dict(zip(status["requested_ticker"], status["status"], strict=True)) == {
        "FAKEA": "ok",
        "FAKEB": "no_figi",
    }


def test_an_unknown_ticker_stops_the_run(tmp_path):
    with pytest.raises(MassiveError, match="404"):
        run_fake(fake_api(), tmp_path, tickers=["FAKEA", "FAKENOPE"])


def test_the_same_security_twice_is_refused(tmp_path):
    api = fake_api()
    api.overviews["FAKEB"]["composite_figi"] = "FAKEFIGI0001"
    with pytest.raises(ValueError, match="same security"):
        run_fake(api, tmp_path)


# --- corporate actions ------------------------------------------------------


def _fetched_partition(tmp_path) -> str:
    [folder] = (tmp_path / "raw" / "splits").iterdir()
    return folder.name


def test_splits_and_dividends_are_stored_with_split_price_factors(tmp_path):
    run_fake(fake_api(), tmp_path)
    partition = _fetched_partition(tmp_path)
    splits = read_partition(tmp_path, "splits", partition)
    assert splits.loc[0, "price_factor"] == 0.25  # 1-for-4: old prices x 0.25
    dividends = read_partition(tmp_path, "dividends", partition)
    assert dividends.loc[0, "cash_amount"] == 0.5
    events = read_partition(tmp_path, "ticker_events", partition)
    assert set(events["ticker"]) == {"FAKEA", "FAKEB"}


def test_default_test_window_is_the_last_30_completed_sessions():
    start, end = default_test_window(date(2026, 9, 19))  # a Saturday
    assert end == date(2026, 9, 18)
    assert len(sessions_between(start, end)) == 30


# --- market-cap look-ahead check --------------------------------------------


@pytest.mark.parametrize(
    ("implied", "on_date", "previous", "latest", "expected"),
    [
        (50.0, 50.0, 48.0, 90.0, mc.POINT_IN_TIME),
        (48.0, 50.0, 48.0, 90.0, mc.POINT_IN_TIME_PREVIOUS),
        (90.0, 50.0, 48.0, 90.0, mc.LOOK_AHEAD),
        (50.1, 50.0, 48.0, 50.0, mc.CANNOT_TELL),
        (70.0, 50.0, 48.0, 90.0, mc.UNCLEAR),
        (None, 50.0, 48.0, 90.0, mc.MISSING),
    ],
)
def test_market_cap_verdicts(implied, on_date, previous, latest, expected):
    assert mc.verdict(implied, on_date, previous, latest) == expected


def test_market_cap_check_detects_look_ahead_from_fake_vendor_data():
    api = FakeMassiveApi()
    # Vendor (fake) computes a 2023 market cap with the latest price: $90.
    api.overviews["FAKEA"] = {"market_cap": 90.0 * 1e6, "weighted_shares_outstanding": 1e6}
    api.closes[("FAKEA", "2023-09-01")] = 50.0
    api.closes[("FAKEA", "2023-08-31")] = 49.0
    api.closes[("FAKEA", "2026-09-18")] = 90.0
    result = mc.check_one(client_for(api), "FAKEA", date(2023, 9, 1), date(2026, 9, 18))
    assert result["implied_price"] == 90.0
    assert result["verdict"] == mc.LOOK_AHEAD
    assert not mc.is_acceptable(result["verdict"])


def test_command_line_run_fails_loudly_without_the_secrets_file(tmp_path, caplog):
    from vpa.data.ingest import main

    code = main(["--secrets-file", str(tmp_path / "missing.env"), "--data-root", str(tmp_path)])
    assert code == 1
    assert "INGEST FAILED" in caplog.text and "Secrets file not found" in caplog.text


def test_market_cap_check_uses_the_ticker_in_use_on_each_date():
    # FAKEB was FAKEOLDB until 2 Sep 2025 (see renamed_api). A 2024 check
    # must ask about FAKEOLDB; asking about FAKEB would get "not found".
    api = renamed_api()
    api.overviews["FAKEOLDB"] = {"market_cap": 40.0 * 1e6, "weighted_shares_outstanding": 1e6}
    api.overviews["FAKEB"].update({"market_cap": 90.0 * 1e6, "weighted_shares_outstanding": 1e6})
    api.closes[("FAKEOLDB", "2024-09-03")] = 40.0
    api.closes[("FAKEOLDB", "2024-08-30")] = 39.0
    api.closes[("FAKEB", "2025-09-03")] = 90.0
    old, latest = mc.check_ticker(
        client_for(api), "FAKEB", [date(2024, 9, 3), date(2025, 9, 3)], date(2025, 9, 3)
    )
    assert (old["ticker"], old["ticker_on_date"]) == ("FAKEB", "FAKEOLDB")
    assert old["close_latest"] == 90.0  # today's close, under today's ticker
    assert old["verdict"] == mc.POINT_IN_TIME
    assert latest["ticker_on_date"] == "FAKEB"


def test_market_cap_check_records_not_found_instead_of_stopping():
    api = fake_api()
    del api.overviews["FAKEA"]
    row = mc.check_one(client_for(api), "FAKEA", date(2023, 9, 1), date(2026, 9, 18))
    assert row["verdict"] == mc.NOT_FOUND
    assert not mc.is_acceptable(row["verdict"])
