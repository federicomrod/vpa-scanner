"""Tests for the EDGAR earnings download (src/vpa/data/edgar.py).

EDGAR responses are faked; no network call is made. See CLAUDE.md rule 4.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from tests.fakes import FakeMassiveApi
from vpa.data.edgar import (
    CIK_DATASET,
    EARNINGS_ITEM,
    EIGHT_K_DATASET,
    FILINGS_DATASET,
    additional_pages,
    download,
    earnings_filings,
    eight_k_filings,
    missed_announcements,
    resolve_ciks,
    stored_ciks,
    universe_securities,
)
from vpa.data.massive import MassiveClient
from vpa.data.raw_store import read_partition, write_part


def submissions(*filings: tuple[str, str, str], pages: list[str] = ()) -> dict:
    """An EDGAR submissions block: (form, items, filing date)."""
    return {
        "filings": {
            "recent": {
                "form": [f[0] for f in filings],
                "items": [f[1] for f in filings],
                "filingDate": [f[2] for f in filings],
                "accessionNumber": [f"0000-{n:02d}" for n in range(len(filings))],
                "acceptanceDateTime": [f"{f[2]}T20:30:00.000Z" for f in filings],
            },
            "files": [{"name": page} for page in pages],
        }
    }


class FakeEdgar:
    """Serves prepared submissions by URL and records what was asked for."""

    def __init__(self, by_url: dict[str, dict]):
        self.by_url = by_url
        self.asked: list[str] = []
        self.agents: list[str] = []

    def __call__(self, request, timeout=None):
        self.asked.append(request.full_url)
        self.agents.append(request.get_header("User-agent"))
        body = self.by_url.get(request.full_url)
        if body is None:
            raise AssertionError(f"fake EDGAR has nothing for {request.full_url}")
        return _Response(body)


class _Response:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --- picking out the earnings filings ----------------------------------------


def test_only_eight_ks_reporting_results_are_kept():
    block = submissions(
        ("8-K", "2.02,9.01", "2025-02-27"),  # earnings
        ("8-K", "5.02", "2025-03-10"),  # a director resigned: not earnings
        ("10-Q", "", "2025-04-01"),  # a quarterly report is not an 8-K
        ("4", "", "2025-04-02"),  # insider transaction
    )
    found = earnings_filings(block)
    assert [f["filing_date"] for f in found] == [date(2025, 2, 27)]
    assert found[0]["items"] == "2.02,9.01"


def test_the_item_is_matched_whole_not_as_a_substring():
    # "12.02" or "2.020" must not count as item 2.02.
    block = submissions(("8-K", "12.02", "2025-02-27"), ("8-K", "2.02", "2025-03-27"))
    assert [f["filing_date"] for f in earnings_filings(block)] == [date(2025, 3, 27)]


def test_the_acceptance_time_is_kept_so_the_reacting_session_is_knowable():
    found = earnings_filings(submissions(("8-K", "2.02", "2025-02-27")))
    assert found[0]["accepted_utc"] == "2025-02-27T20:30:00.000Z"


def test_a_company_with_no_filings_at_all_is_not_an_error():
    assert earnings_filings(submissions()) == []


def test_older_pages_are_noticed():
    block = submissions(("8-K", "2.02", "2025-02-27"), pages=["CIK0000320193-submissions-001.json"])
    assert additional_pages(block) == ["CIK0000320193-submissions-001.json"]
    assert additional_pages(submissions()) == []


# --- the download ------------------------------------------------------------


def securities_frame(rows: list[tuple[str, str, int | None]]) -> pd.DataFrame:
    return pd.DataFrame([{"security_key": k, "ticker": t, "cik": c} for k, t, c in rows])


def test_a_companys_filings_are_stored_against_its_security(tmp_path):
    edgar = FakeEdgar(
        {
            "https://data.sec.gov/submissions/CIK0000001018.json": submissions(
                ("8-K", "2.02", "2025-02-27"), ("8-K", "2.02", "2025-05-28")
            )
        }
    )
    download(
        tmp_path,
        securities_frame([("FIGI_FAKEA", "FAKEA", 1018)]),
        contact="vpa-scanner test@example.com",
        run_id="run-1",
        opener=edgar,
        sleep=lambda s: None,
    )
    stored = read_partition(tmp_path, FILINGS_DATASET, f"fetched={date.today()}")
    assert list(stored["filing_date"]) == [date(2025, 2, 27), date(2025, 5, 28)]
    assert set(stored["security_key"]) == {"FIGI_FAKEA"}
    assert set(stored["cik"]) == {1018}


def test_the_contact_address_the_sec_requires_is_sent(tmp_path):
    edgar = FakeEdgar(
        {
            "https://data.sec.gov/submissions/CIK0000001018.json": submissions(
                ("8-K", "2.02", "2025-02-27")
            )
        }
    )
    download(tmp_path, securities_frame([("F", "FAKEA", 1018)]), "vpa-scanner me@example.com",
             "run-1", opener=edgar, sleep=lambda s: None)  # fmt: skip
    assert edgar.agents == ["vpa-scanner me@example.com"]


def test_older_pages_are_fetched_too(tmp_path):
    edgar = FakeEdgar(
        {
            "https://data.sec.gov/submissions/CIK0000001018.json": submissions(
                ("8-K", "2.02", "2025-02-27"), pages=["CIK0000001018-submissions-001.json"]
            ),
            "https://data.sec.gov/submissions/CIK0000001018-submissions-001.json": submissions(
                ("8-K", "2.02", "2016-11-18")
            ),
        }
    )
    download(tmp_path, securities_frame([("F", "FAKEA", 1018)]), "c", "run-1",
             opener=edgar, sleep=lambda s: None)  # fmt: skip
    stored = read_partition(tmp_path, FILINGS_DATASET, f"fetched={date.today()}")
    assert sorted(stored["filing_date"]) == [date(2016, 11, 18), date(2025, 2, 27)]


def test_a_security_with_no_cik_is_skipped_not_guessed(tmp_path):
    edgar = FakeEdgar({})
    download(tmp_path, securities_frame([("F", "FAKEA", None)]), "c", "run-1",
             opener=edgar, sleep=lambda s: None)  # fmt: skip
    assert edgar.asked == []  # nothing invented for it
    assert read_partition(tmp_path, FILINGS_DATASET, f"fetched={date.today()}").empty


def test_a_company_edgar_does_not_know_is_reported_not_fatal(tmp_path, caplog):
    import urllib.error

    class NotFound(FakeEdgar):
        def __call__(self, request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    download(tmp_path, securities_frame([("F", "FAKEGONE", 9999)]), "c", "run-1",
             opener=NotFound({}), sleep=lambda s: None)  # fmt: skip
    assert "no submissions for FAKEGONE" in caplog.text


def test_a_suspiciously_low_filing_rate_is_flagged(tmp_path, caplog):
    # Two filings ten years apart is nothing like quarterly: the usual
    # cause is a wrong CIK, which returns another company's filings.
    edgar = FakeEdgar(
        {
            "https://data.sec.gov/submissions/CIK0000001018.json": submissions(
                ("8-K", "2.02", "2016-11-18"), ("8-K", "2.02", "2026-02-27")
            )
        }
    )
    download(tmp_path, securities_frame([("F", "FAKETHIN", 1018)]), "c", "run-1",
             opener=edgar, sleep=lambda s: None)  # fmt: skip
    assert "fewer than" in caplog.text and "FAKETHIN" in caplog.text


# --- CIK resolution ----------------------------------------------------------


def test_ciks_come_from_the_vendors_point_in_time_details(tmp_path):
    api = FakeMassiveApi()
    api.overviews["FAKEA"] = {"cik": "0000001018", "name": "Fake A Inc"}
    client = MassiveClient("FAKE-KEY", session=api, requests_per_second=1e9, sleep=lambda s: None)
    securities = pd.DataFrame(
        [{"security_key": "FIGI_A", "ticker": "FAKEA", "as_of": date(2025, 1, 31)}]
    )
    resolved = resolve_ciks(client, securities, "run-1", tmp_path)
    assert list(resolved["cik"]) == [1018]
    # Asked as of a date the ticker was valid, so renames resolve.
    assert api.overview_calls("FAKEA") == ["2025-01-31"]


def test_a_second_run_does_not_look_up_ciks_again(tmp_path):
    api = FakeMassiveApi()
    api.overviews["FAKEA"] = {"cik": "0000001018"}
    client = MassiveClient("FAKE-KEY", session=api, requests_per_second=1e9, sleep=lambda s: None)
    securities = pd.DataFrame(
        [{"security_key": "FIGI_A", "ticker": "FAKEA", "as_of": date(2025, 1, 31)}]
    )
    resolve_ciks(client, securities, "run-1", tmp_path)
    again = FakeMassiveApi()
    resolve_ciks(
        MassiveClient("FAKE-KEY", session=again, requests_per_second=1e9, sleep=lambda s: None),
        securities, "run-2", tmp_path,
    )  # fmt: skip
    assert again.calls == []
    assert len(read_partition(tmp_path, CIK_DATASET, "all")) == 1


def test_a_security_the_vendor_cannot_identify_gets_no_cik(tmp_path, caplog):
    api = FakeMassiveApi()  # knows nothing: every lookup is a 404
    client = MassiveClient("FAKE-KEY", session=api, requests_per_second=1e9, sleep=lambda s: None)
    securities = pd.DataFrame(
        [{"security_key": "FIGI_A", "ticker": "FAKEGONE", "as_of": date(2025, 1, 31)}]
    )
    resolved = resolve_ciks(client, securities, "run-1", tmp_path)
    assert resolved["cik"].isna().all()
    assert "no CIK on record" in caplog.text


# --- which securities are downloaded -----------------------------------------


def test_every_security_ever_in_the_universe_is_included(tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    for day, tickers in (("2025-01-02", ["FAKEA", "FAKEB"]), ("2025-02-03", ["FAKEA", "FAKEC"])):
        pd.DataFrame(
            {
                "ticker": tickers,
                "composite_figi": [f"FIGI_{t}" for t in tickers],
                "as_of_session": [date.fromisoformat(day)] * len(tickers),
            }
        ).to_parquet(universe / f"{day}.parquet")
    found = universe_securities(tmp_path)
    # FAKEB left the universe in February; it must still be downloaded,
    # or its history would thin out exactly where survivorship bias lives.
    assert sorted(found["ticker"]) == ["FAKEA", "FAKEB", "FAKEC"]
    # Each is asked for as of the last date it was a member.
    assert found[found.ticker == "FAKEA"]["as_of"].iloc[0] == date(2025, 2, 3)
    assert found[found.ticker == "FAKEB"]["as_of"].iloc[0] == date(2025, 1, 2)


def test_no_snapshots_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="No universe snapshots"):
        universe_securities(tmp_path)


def test_the_item_code_is_the_one_section_6_means():
    assert EARNINGS_ITEM == "2.02"


# --- every 8-K, not only the results ones ------------------------------------


def test_all_eight_ks_are_kept_whatever_they_report():
    block = submissions(
        ("8-K", "2.02,9.01", "2025-02-27"),
        ("8-K", "7.01", "2025-01-13"),  # ANF's sales update shape
        ("8-K", "5.02", "2025-03-10"),
        ("10-Q", "", "2025-04-01"),  # still not an 8-K
    )
    found = eight_k_filings(block)
    assert [f["items"] for f in found] == ["2.02,9.01", "7.01", "5.02"]
    # ...and the narrower view still sees only the results filing.
    assert [f["items"] for f in earnings_filings(block)] == ["2.02,9.01"]


def test_an_eight_k_with_no_items_recorded_is_still_kept():
    assert len(eight_k_filings(submissions(("8-K", "", "2025-02-27")))) == 1


def test_the_download_can_store_every_eight_k(tmp_path):
    edgar = FakeEdgar(
        {
            "https://data.sec.gov/submissions/CIK0000001018.json": submissions(
                ("8-K", "2.02", "2025-02-27"), ("8-K", "7.01", "2025-01-13")
            )
        }
    )
    download(tmp_path, securities_frame([("F", "FAKEA", 1018)]), "c", "run-1",
             opener=edgar, sleep=lambda s: None,
             extract=eight_k_filings, dataset=EIGHT_K_DATASET)  # fmt: skip
    stored = read_partition(tmp_path, EIGHT_K_DATASET, f"fetched={date.today()}")
    assert sorted(stored["items"]) == ["2.02", "7.01"]


# --- what the 2.02 flag is silent about --------------------------------------


def eight_ks(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    """(security, filing date, items) as the stored dataset holds them."""
    return pd.DataFrame(
        [
            {
                "security_key": key,
                "ticker": key,
                "cik": 1,
                "accession_number": f"0000-{n:02d}",
                "filing_date": date.fromisoformat(day),
                "accepted_utc": f"{day}T12:10:00.000Z",
                "items": items,
            }
            for n, (key, day, items) in enumerate(rows)
        ]
    )


def store_eight_ks(tmp_path, table) -> None:
    write_part(tmp_path, EIGHT_K_DATASET, "fetched=2026-01-01", "run-1", table,
               {"dataset": EIGHT_K_DATASET})  # fmt: skip


def test_a_voluntary_filing_with_no_results_near_it_is_counted(tmp_path):
    # The ANF case: a 7.01 in January, quarterly results nowhere near it.
    store_eight_ks(tmp_path, eight_ks([
        ("FIGI_A", "2025-01-13", "7.01,9.01"),
        ("FIGI_A", "2025-03-05", "2.02,9.01"),
    ]))  # fmt: skip
    missed = missed_announcements(tmp_path)
    assert list(missed["filing_date"]) == [date(2025, 1, 13)]


def test_a_voluntary_filing_alongside_the_results_is_not_counted(tmp_path):
    # Companies routinely file an 8.01 with, or a day either side of,
    # their results. Those are the same announcement, already flagged.
    store_eight_ks(tmp_path, eight_ks([
        ("FIGI_A", "2025-03-05", "2.02"),
        ("FIGI_A", "2025-03-04", "8.01"),
        ("FIGI_A", "2025-03-06", "7.01"),
    ]))  # fmt: skip
    assert missed_announcements(tmp_path).empty


def test_other_item_codes_are_not_counted(tmp_path):
    # A director resigning is not a results announcement by any reading.
    store_eight_ks(tmp_path, eight_ks([("FIGI_A", "2025-01-13", "5.02")]))
    assert missed_announcements(tmp_path).empty


def test_one_companys_results_do_not_cover_anothers_filing(tmp_path):
    store_eight_ks(tmp_path, eight_ks([
        ("FIGI_A", "2025-01-13", "7.01"),
        ("FIGI_B", "2025-01-13", "2.02"),
    ]))  # fmt: skip
    missed = missed_announcements(tmp_path)
    assert list(missed["security_key"]) == ["FIGI_A"]


def test_nothing_downloaded_yet_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="--all-items"):
        missed_announcements(tmp_path)


def test_ciks_must_already_be_resolved_before_an_all_items_run(tmp_path):
    with pytest.raises(FileNotFoundError, match="No CIKs stored"):
        stored_ciks(tmp_path)
