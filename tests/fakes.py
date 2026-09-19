"""Fake delivery senders shared across tests.

These implement the same interfaces as the real email/push/dead-man's-
switch senders (src/vpa/delivery/), but only record what would have
been sent - no network call is ever made. See CLAUDE.md, rule 4.
"""

from __future__ import annotations


class FakeEmailSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, *, subject: str, body: str) -> None:
        self.sent.append((subject, body))


class FakePushSender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message: str) -> None:
        self.sent.append(message)


class FakeDeadManSwitch:
    def __init__(self) -> None:
        self.pinged_success = False
        self.pinged_fail = False

    def ping_success(self) -> None:
        self.pinged_success = True

    def ping_fail(self) -> None:
        self.pinged_fail = True


class FakeResponse:
    """Stands in for a `requests.Response`."""

    def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._body


class FakeMassiveApi:
    """A fake Massive REST API serving FAKE data. Implements the one
    method MassiveClient uses (`get`) and records every request. No
    network call is ever made.

    - `overviews`: ticker -> ticker-overview `results` dict
    - `events`: composite FIGI -> list of ticker-change events
    - `minutes`: ticker -> list of {"t": epoch ms, "o", "h", "l", "c", "v", ...}
    - `closes`: (ticker, "YYYY-MM-DD") -> daily close
    - `splits`, `dividends`: ticker -> list of rows
    - `fail`: ticker -> HTTP status to return for that ticker's minute bars
    """

    def __init__(self, page_size: int = 50_000) -> None:
        self.overviews: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.minutes: dict[str, list[dict]] = {}
        self.closes: dict[tuple[str, str], float] = {}
        self.splits: dict[str, list[dict]] = {}
        self.dividends: dict[str, list[dict]] = {}
        self.fail: dict[str, int] = {}
        self.page_size = page_size
        self.calls: list[tuple[str, dict | None, dict]] = []

    def get(self, url: str, *, params=None, headers=None, timeout=None) -> FakeResponse:
        from urllib.parse import parse_qs, urlparse

        self.calls.append((url, params, headers))
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        params = {**(params or {}), **query}

        if parts[:3] == ["v3", "reference", "tickers"]:
            ticker = parts[3]
            if ticker not in self.overviews:
                return FakeResponse(404, {"message": "Ticker not found"})
            return FakeResponse(200, {"results": self.overviews[ticker]})
        if parts[:3] == ["vX", "reference", "tickers"]:
            return FakeResponse(200, {"results": {"events": self.events.get(parts[3], [])}})
        # /v2/aggs/ticker/{ticker}/range/1/{timespan}/{from}/{to}
        if parts[:3] == ["v2", "aggs", "ticker"] and parts[6] == "minute":
            return self._minutes(parts[3], parts[7], parts[8], int(params.get("cursor", 0)), url)
        if parts[:3] == ["v2", "aggs", "ticker"] and parts[6] == "day":
            close = self.closes.get((parts[3], parts[7]))
            return FakeResponse(200, {"results": [{"c": close}] if close is not None else []})
        if parts[:3] == ["stocks", "v1", "splits"]:
            return FakeResponse(200, {"results": self.splits.get(params["ticker"], [])})
        if parts[:3] == ["stocks", "v1", "dividends"]:
            return FakeResponse(200, {"results": self.dividends.get(params["ticker"], [])})
        return FakeResponse(404, {"message": f"fake API has no route for {url}"})

    def minute_calls(self) -> list[str]:
        return [url for url, _, _ in self.calls if "/minute/" in url]

    def _minutes(self, ticker, start, end, cursor, url) -> FakeResponse:
        import pandas as pd

        if ticker in self.fail:
            return FakeResponse(self.fail[ticker], {"message": "fake failure"})
        rows = [
            r
            for r in self.minutes.get(ticker, [])
            if start
            <= pd.Timestamp(r["t"], unit="ms", tz="UTC")
            .tz_convert("America/New_York")
            .date()
            .isoformat()
            <= end
        ]
        page = rows[cursor : cursor + self.page_size]
        body: dict = {"results": page, "adjusted": False}
        if cursor + self.page_size < len(rows):
            base = url.split("?")[0]
            body["next_url"] = f"{base}?cursor={cursor + self.page_size}"
        return FakeResponse(200, body)
