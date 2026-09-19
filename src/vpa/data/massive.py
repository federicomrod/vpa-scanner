"""A small client for the Massive (formerly Polygon.io) REST API.

- The API key is sent in a request header, never in the URL, so it can't
  leak into logs or error messages that quote a URL.
- Requests are spaced out to a steady rate (`requests_per_second`). Paid
  Stocks plans have no hard call limit, but the vendor asks for
  reasonable use, and a steady pace makes the run time predictable.
- Temporary failures (rate limiting, server errors, dropped connections)
  are retried with exponential backoff. Anything else - a bad request,
  or a plan that doesn't include the data - fails immediately and loudly.

The HTTP session, sleep and clock are injectable so tests never touch
the network (CLAUDE.md rule 4).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.massive.com"

#: Status codes worth retrying: rate limited, or a temporary server problem.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpSession(Protocol):
    def get(self, url: str, *, params: Any, headers: Any, timeout: float) -> Any: ...


class MassiveError(Exception):
    """Raised when the API can't give us what we asked for."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MassiveClient:
    def __init__(
        self,
        api_key: str,
        *,
        session: HttpSession | None = None,
        requests_per_second: float = 10.0,
        max_attempts: int = 8,
        max_backoff_seconds: float = 120.0,
        timeout_seconds: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._session = session or requests.Session()
        self._min_interval = 1.0 / requests_per_second
        self._max_attempts = max_attempts
        self._max_backoff = max_backoff_seconds
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None
        self.request_count = 0

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET one page and return its JSON body."""
        url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
        for attempt in range(1, self._max_attempts + 1):
            self._wait_for_rate_limit()
            try:
                response = self._session.get(
                    url, params=params, headers=self._headers, timeout=self._timeout
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                problem = f"network error ({type(exc).__name__})"
                retry_after = None
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in RETRYABLE_STATUS:
                    raise MassiveError(_describe_failure(url, response), response.status_code)
                problem = f"HTTP {response.status_code}"
                retry_after = _retry_after_seconds(response)

            if attempt == self._max_attempts:
                raise MassiveError(f"Giving up on {url} after {attempt} attempts: {problem}")
            delay = retry_after or min(self._max_backoff, 2.0 ** (attempt - 1))
            log.warning(
                "%s from %s - retrying in %.0fs (attempt %d of %d)",
                problem,
                url,
                delay,
                attempt,
                self._max_attempts,
            )
            self._sleep(delay)
        raise AssertionError("unreachable")

    def get_all(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Follow `next_url` pagination, yielding every item in `results`."""
        page = self.get(path, params)
        while True:
            yield from page.get("results") or []
            next_url = page.get("next_url")
            if not next_url:
                return
            page = self.get(next_url)

    def _wait_for_rate_limit(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            wait = self._last_request_at + self._min_interval - now
            if wait > 0:
                self._sleep(wait)
                now += wait
        self._last_request_at = now
        self.request_count += 1


def _retry_after_seconds(response: Any) -> float | None:
    try:
        return float(response.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


def _describe_failure(url: str, response: Any) -> str:
    hint = ""
    if response.status_code in (401, 403):
        hint = " - check the API key, and that the current plan includes this data"
    try:
        detail = response.json().get("message") or response.json().get("error") or ""
    except ValueError:
        detail = ""
    return f"HTTP {response.status_code} from {url}{hint}. {detail}".strip()
