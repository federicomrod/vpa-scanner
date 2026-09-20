"""Tests for the Massive API client (src/vpa/data/massive.py) and secret
loading (src/vpa/data/secrets.py). All HTTP is faked - no network calls."""

from __future__ import annotations

import logging

import pytest
import requests

from tests.fakes import FakeResponse
from vpa.data.massive import MassiveClient, MassiveError, results_object, ticker_path
from vpa.data.secrets import RedactSecrets, SecretError, load_secret

FAKE_KEY = "FAKE-KEY-not-a-real-secret-123"


class ScriptedSession:
    """Returns the given responses (or raises the given exceptions) in order."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append((url, params, headers))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(session, fake_time=None, **kwargs) -> MassiveClient:
    fake_time = fake_time or FakeTime()
    return MassiveClient(
        FAKE_KEY, session=session, sleep=fake_time.sleep, clock=fake_time.clock, **kwargs
    )


OK = FakeResponse(200, {"results": [1]})


# --- requests and authentication --------------------------------------------


def test_key_is_sent_in_a_header_never_in_the_url_or_params():
    session = ScriptedSession(OK)
    make_client(session).get("/v3/thing", {"a": 1})
    url, params, headers = session.calls[0]
    assert url == "https://api.massive.com/v3/thing"
    assert headers == {"Authorization": f"Bearer {FAKE_KEY}"}
    assert FAKE_KEY not in url and FAKE_KEY not in str(params)


def test_requests_are_spaced_to_the_rate_limit():
    fake_time = FakeTime()
    client = make_client(ScriptedSession(OK, OK, OK), fake_time, requests_per_second=4)
    for _ in range(3):
        client.get("/x")
    assert fake_time.sleeps == [0.25, 0.25]
    assert client.request_count == 3


def test_pagination_follows_next_url():
    session = ScriptedSession(
        FakeResponse(200, {"results": [1, 2], "next_url": "https://api.massive.com/x?cursor=a"}),
        FakeResponse(200, {"results": [3]}),
    )
    assert list(make_client(session).get_all("/x")) == [1, 2, 3]
    assert session.calls[1][0] == "https://api.massive.com/x?cursor=a"


# --- retries ----------------------------------------------------------------


def test_rate_limited_request_waits_as_told_then_succeeds():
    fake_time = FakeTime()
    session = ScriptedSession(FakeResponse(429, headers={"Retry-After": "7"}), OK)
    assert make_client(session, fake_time).get("/x") == {"results": [1]}
    assert 7 in fake_time.sleeps


def test_server_errors_and_dropped_connections_back_off_exponentially():
    fake_time = FakeTime()
    session = ScriptedSession(
        FakeResponse(500), requests.ConnectionError("fake"), FakeResponse(503), OK
    )
    make_client(session, fake_time, requests_per_second=1000).get("/x")
    backoffs = [s for s in fake_time.sleeps if s >= 1]
    assert backoffs == [1, 2, 4]


def test_gives_up_loudly_after_max_attempts():
    session = ScriptedSession(*[FakeResponse(500)] * 3)
    with pytest.raises(MassiveError, match="after 3 attempts"):
        make_client(session, max_attempts=3).get("/x")


def test_permission_errors_fail_immediately_without_retrying():
    session = ScriptedSession(FakeResponse(403, {"message": "NOT_AUTHORIZED"}))
    with pytest.raises(MassiveError, match="plan includes this data"):
        make_client(session).get("/x")
    assert len(session.calls) == 1


def test_the_key_never_appears_in_logs_or_errors(caplog):
    caplog.set_level(logging.DEBUG)
    session = ScriptedSession(FakeResponse(429), FakeResponse(401))
    with pytest.raises(MassiveError) as error:
        make_client(session).get("/x")
    assert FAKE_KEY not in caplog.text
    assert FAKE_KEY not in str(error.value)


# --- secrets file -----------------------------------------------------------


def write_secrets(tmp_path, text: str):
    path = tmp_path / ".env"
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_loads_the_named_key(tmp_path):
    path = write_secrets(
        tmp_path, "# comment\nOTHER=x\nexport MASSIVE_API_KEY='FAKE-abc'\nLAST=y\n"
    )
    assert load_secret("MASSIVE_API_KEY", path) == "FAKE-abc"


def test_missing_key_or_file_is_an_error_that_names_the_key_only(tmp_path):
    path = write_secrets(tmp_path, "OTHER=FAKE-value\n")
    with pytest.raises(SecretError, match="MASSIVE_API_KEY is not set") as error:
        load_secret("MASSIVE_API_KEY", path)
    assert "FAKE-value" not in str(error.value)
    with pytest.raises(SecretError, match="not found"):
        load_secret("MASSIVE_API_KEY", tmp_path / "missing.env")


def test_empty_value_is_an_error(tmp_path):
    with pytest.raises(SecretError, match="empty"):
        load_secret("MASSIVE_API_KEY", write_secrets(tmp_path, "MASSIVE_API_KEY=\n"))


def test_loose_file_permissions_are_warned_about(tmp_path, caplog):
    path = write_secrets(tmp_path, "MASSIVE_API_KEY=FAKE\n")
    path.chmod(0o644)
    load_secret("MASSIVE_API_KEY", path)
    assert "mode 600" in caplog.text


def test_redaction_filter_scrubs_secret_values():
    record = logging.LogRecord("x", logging.INFO, "", 0, "url %s failed", (FAKE_KEY,), None)
    RedactSecrets(FAKE_KEY).filter(record)
    assert record.getMessage() == "url [REDACTED] failed"


# --- guards against malformed identifiers and responses ----------------------


def test_a_blank_ticker_is_refused_before_it_becomes_a_url():
    for bad in ("", "   ", "FAKE ", None):
        with pytest.raises(MassiveError, match="usable ticker"):
            ticker_path(bad)


def test_unusual_ticker_characters_are_escaped():
    assert ticker_path("BRK.B") == "BRK.B"
    assert ticker_path("FAKE/X") == "FAKE%2FX"


def test_a_list_where_one_record_was_expected_is_a_clear_error():
    with pytest.raises(MassiveError, match="Expected one record for ticker details, got list"):
        results_object({"results": [{"ticker": "FAKEA"}]}, "ticker details")
    assert results_object({"results": {"ticker": "FAKEA"}}, "ticker details") == {"ticker": "FAKEA"}
    assert results_object({}, "ticker details") == {}
