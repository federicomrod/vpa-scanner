"""Pinging a dead-man's-switch monitoring service.

A dead-man's-switch is a URL you (or a service like healthchecks.io)
ping every time the scan runs. If a ping doesn't arrive when expected,
the service alerts you that something went silently wrong - the
opposite of the scan itself alerting you. See README.md for how to set
one up.

The convention here (ping the base URL on success, and `<base URL>/fail`
on failure) matches healthchecks.io. The base URL contains a secret
token and must come from an environment variable
(`VPA_DEADMAN__BASE_URL`), never from config.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import requests

from vpa.config import DeadManConfig


class DeadManSwitch(Protocol):
    """Anything that can be pinged to report success or failure.
    Implement this in tests with a fake that just records the ping."""

    def ping_success(self) -> None: ...

    def ping_fail(self) -> None: ...


class DeadManConfigError(Exception):
    """Raised when dead-man's-switch configuration is missing."""


@dataclass
class HttpDeadManSwitch:
    """Pings a dead-man's-switch URL over plain HTTP GET."""

    base_url: str

    def ping_success(self) -> None:
        requests.get(self.base_url, timeout=10).raise_for_status()

    def ping_fail(self) -> None:
        requests.get(f"{self.base_url}/fail", timeout=10).raise_for_status()


def build_deadman_switch(config: DeadManConfig) -> HttpDeadManSwitch:
    """Build a real HttpDeadManSwitch from configuration.

    Raises DeadManConfigError with a plain-language explanation if the
    base URL is missing.
    """
    if not config.base_url:
        raise DeadManConfigError(
            "Missing dead-man's-switch setting: base_url. This must be "
            "set as an environment variable (VPA_DEADMAN__BASE_URL), "
            "never in config.yaml - see README.md."
        )
    return HttpDeadManSwitch(base_url=config.base_url)
