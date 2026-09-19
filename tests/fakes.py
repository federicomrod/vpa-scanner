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
    - `overviews_on`: (ticker, "YYYY-MM-DD") -> ticker-overview for that
      date only; `None` means "not found". Falls back to `overviews`.
    - `grouped`: "YYYY-MM-DD" -> that day's whole-market daily bars
    - `listed`: "YYYY-MM-DD" -> every ticker listed that day
    """

    def __init__(self, page_size: int = 50_000) -> None:
        self.overviews: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.minutes: dict[str, list[dict]] = {}
        self.closes: dict[tuple[str, str], float] = {}
        self.splits: dict[str, list[dict]] = {}
        self.dividends: dict[str, list[dict]] = {}
        self.fail: dict[str, int] = {}
        self.overviews_on: dict[tuple[str, str], dict | None] = {}
        self.grouped: dict[str, list[dict]] = {}
        self.listed: dict[str, list[dict]] = {}
        self.page_size = page_size
        self.calls: list[tuple[str, dict | None, dict]] = []

    def get(self, url: str, *, params=None, headers=None, timeout=None) -> FakeResponse:
        from urllib.parse import parse_qs, urlparse

        self.calls.append((url, params, headers))
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        params = {**(params or {}), **query}

        if parts == ["v3", "reference", "tickers"]:
            return FakeResponse(200, {"results": self.listed.get(params["date"], [])})
        if parts[:3] == ["v3", "reference", "tickers"]:
            ticker = parts[3]
            dated = (ticker, params.get("date"))
            if dated in self.overviews_on:
                found = self.overviews_on[dated]
                if found is None:
                    return FakeResponse(404, {"message": "Ticker not found"})
                return FakeResponse(200, {"results": found})
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
        if parts[:6] == ["v2", "aggs", "grouped", "locale", "us", "market"]:
            return FakeResponse(200, {"results": self.grouped.get(parts[7], []), "adjusted": False})
        if parts[:3] == ["stocks", "v1", "splits"] and "ticker" not in params:
            every = [r for rows in self.splits.values() for r in rows]
            return FakeResponse(
                200,
                {
                    "results": [
                        r
                        for r in every
                        if params["execution_date.gte"]
                        <= r["execution_date"]
                        <= params["execution_date.lte"]
                    ]
                },
            )
        if parts[:3] == ["stocks", "v1", "splits"]:
            return FakeResponse(200, {"results": self.splits.get(params["ticker"], [])})
        if parts[:3] == ["stocks", "v1", "dividends"]:
            return FakeResponse(200, {"results": self.dividends.get(params["ticker"], [])})
        return FakeResponse(404, {"message": f"fake API has no route for {url}"})

    def overview_calls(self, ticker: str) -> list[str | None]:
        """The `date` of every ticker-overview request for `ticker`."""
        return [
            (p or {}).get("date")
            for url, p, _ in self.calls
            if url.rstrip("/").endswith(f"/v3/reference/tickers/{ticker}")
        ]

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
