"""Tests for the universe-wide minute download (src/vpa/data/ingest_universe.py).

A small FAKE market served by FakeMassiveApi, July-September 2025, written
to a temporary folder. No network, no real data.
"""

from __future__ import annotations

import csv
from datetime import date

import pandas as pd

from tests.fakes import FakeMassiveApi
from tests.test_ingest import fake_bars, minute_day
from vpa.data.calendar import sessions_between
from vpa.data.ingest_universe import month_ends, run
from vpa.data.massive import MassiveClient
from vpa.data.raw_store import manifests

TODAY = date(2025, 10, 15)
JULY = sessions_between(date(2025, 7, 1), date(2025, 7, 31))
AUGUST = sessions_between(date(2025, 8, 1), date(2025, 8, 29))
SEPTEMBER = sessions_between(date(2025, 9, 1), date(2025, 9, 30))
RENAME = date(2025, 8, 15)  # FAKEOLDB -> FAKEB (on record with the vendor)

# Month-end ticker lists: which symbol each security was listed under.
LISTED = {
    "2025-06-30": {"F_A": "FAKEA", "F_B": "FAKEOLDB", "F_D": "FAKEDEL", "F_N": "FAKENOEVOLD"},
    "2025-07-31": {"F_A": "FAKEA", "F_B": "FAKEOLDB", "F_D": "FAKEDEL", "F_N": "FAKENOEVOLD"},
    # FAKEDEL was delisted in August. FAKENOEV renamed in August, but the
    # vendor has no ticker history for it.
    "2025-08-29": {"F_A": "FAKEA", "F_B": "FAKEB", "F_N": "FAKENOEV"},
    "2025-09-30": {"F_A": "FAKEA", "F_B": "FAKEB", "F_N": "FAKENOEV"},
}


def fake_market() -> FakeMassiveApi:
    api = FakeMassiveApi()
    for day, rows in LISTED.items():
        api.listed[day] = [{"ticker": t, "composite_figi": f} for f, t in rows.items()]
        api.listed[day].append({"ticker": "SPY", "composite_figi": "F_SPY"})
    for figi, ticker in [("F_A", "FAKEA"), ("F_SPY", "SPY"), ("F_D", "FAKEDEL")]:
        api.events[figi] = [
            {"type": "ticker_change", "date": "2015-01-02", "ticker_change": {"ticker": ticker}}
        ]
    api.events["F_B"] = [
        {"type": "ticker_change", "date": "2015-01-02", "ticker_change": {"ticker": "FAKEOLDB"}},
        {"type": "ticker_change", "date": RENAME.isoformat(), "ticker_change": {"ticker": "FAKEB"}},
    ]
    for ticker, figi in [("FAKEA", "F_A"), ("FAKEB", "F_B"), ("FAKEDEL", "F_D"),
                         ("FAKENOEV", "F_N"), ("SPY", "F_SPY")]:  # fmt: skip
        api.overviews[ticker] = {"composite_figi": figi, "name": f"{ticker} Inc"}

    everything = JULY + AUGUST + SEPTEMBER
    api.minutes["FAKEA"] = fake_bars(everything)
    api.minutes["SPY"] = fake_bars(everything)
    api.minutes["FAKEOLDB"] = fake_bars([d for d in everything if d < RENAME], price=40)
    api.minutes["FAKEB"] = fake_bars([d for d in everything if d >= RENAME])
    # FAKEDEL: delisted in August; the symbol was reused by someone else in September.
    api.minutes["FAKEDEL"] = fake_bars(JULY + SEPTEMBER, price=7)
    api.minutes["FAKENOEVOLD"] = fake_bars(JULY + AUGUST[:10])
    api.minutes["FAKENOEV"] = fake_bars(AUGUST[10:] + SEPTEMBER)
    return api


def write_universe(tmp_path) -> None:
    universe = tmp_path / "universe"
    universe.mkdir()
    pd.DataFrame(
        {"ticker": ["FAKEA", "FAKEB", "FAKEDEL", "FAKENOEVOLD"],
         "composite_figi": ["F_A", "F_B", "F_D", "F_N"]}
    ).to_parquet(universe / "2025-07-01.parquet")  # fmt: skip


def download(api, tmp_path):
    client = MassiveClient("FAKE-KEY", session=api, requests_per_second=1e9, sleep=lambda s: None)
    return run(client, tmp_path, tmp_path / "universe", "2025-07", "2025-09", TODAY, threads=2)


def keys_on(tmp_path, day: date) -> set[str]:
    return set(minute_day(tmp_path, day)["security_key"]) if day else set()


def read_log(tmp_path, name) -> list[dict]:
    with (tmp_path / "logs" / name).open() as handle:
        return list(csv.DictReader(handle))


def test_month_ends_are_the_last_trading_day_before_each_month_and_after():
    assert month_ends("2025-07", "2025-09", TODAY) == [
        date(2025, 6, 30),
        date(2025, 7, 31),
        date(2025, 8, 29),
        date(2025, 9, 30),
    ]
    # The end of the current month hasn't happened yet.
    assert month_ends("2025-09", "2025-10", TODAY)[-1] == date(2025, 9, 30)


def test_every_universe_member_and_spy_is_downloaded_while_listed(tmp_path):
    write_universe(tmp_path)
    download(fake_market(), tmp_path)
    assert keys_on(tmp_path, JULY[5]) == {"F_A", "F_B", "F_D", "F_N", "F_SPY"}
    assert keys_on(tmp_path, SEPTEMBER[5]) == {"F_A", "F_B", "F_N", "F_SPY"}


def test_a_delisted_symbol_reused_by_someone_else_is_never_fetched(tmp_path):
    write_universe(tmp_path)
    api = fake_market()
    download(api, tmp_path)
    assert "F_D" not in keys_on(tmp_path, SEPTEMBER[0])
    assert not any("/FAKEDEL/range/1/minute/2025-09" in u for u in api.minute_calls())


def test_a_rename_on_record_is_stitched(tmp_path):
    write_universe(tmp_path)
    download(fake_market(), tmp_path)
    early = minute_day(tmp_path, AUGUST[0])
    assert set(early.loc[early["security_key"] == "F_B", "ticker"]) == {"FAKEOLDB"}
    stitched = {
        (r["requested_ticker"], r["fetched_as"]) for r in read_log(tmp_path, "ticker_stitches.csv")
    }
    assert ("FAKEB", "FAKEOLDB") in stitched


def test_a_rename_with_no_history_uses_the_lists_and_skips_the_ambiguous_month(tmp_path):
    write_universe(tmp_path)
    download(fake_market(), tmp_path)
    july = minute_day(tmp_path, JULY[0])
    assert set(july.loc[july["security_key"] == "F_N", "ticker"]) == {"FAKENOEVOLD"}
    assert "F_N" not in keys_on(tmp_path, AUGUST[12])  # the month it changed: skipped
    september = minute_day(tmp_path, SEPTEMBER[0])
    assert set(september.loc[september["security_key"] == "F_N", "ticker"]) == {"FAKENOEV"}
    skipped = [
        (r["requested_ticker"], r["month"]) for r in read_log(tmp_path, "minute_months_skipped.csv")
    ]
    assert skipped == [("FAKENOEV", "2025-08")]


def test_corporate_actions_are_fetched_under_every_listed_symbol(tmp_path):
    write_universe(tmp_path)
    api = fake_market()
    download(api, tmp_path)
    split_tickers = {p["ticker"] for u, p, _ in api.calls if "/splits" in u}
    assert {"FAKENOEV", "FAKENOEVOLD", "FAKEB", "FAKEOLDB", "SPY"} <= split_tickers


def test_rerunning_downloads_nothing_already_stored(tmp_path):
    write_universe(tmp_path)
    download(fake_market(), tmp_path)
    api = fake_market()
    download(api, tmp_path)
    assert api.minute_calls() == []
    assert len(manifests(tmp_path, "minute", f"date={JULY[0]}")) == 1
