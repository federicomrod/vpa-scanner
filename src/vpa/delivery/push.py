"""Sending a short push notification via ntfy.sh.

ntfy.sh needs no account or sign-up: you pick a private "topic" name
(effectively a password - keep it hard to guess), subscribe to it in
the free ntfy app, and anything posted to that topic's URL shows up as
a notification. See README.md for setup steps.

The topic name is a secret and must come from an environment variable
(`VPA_PUSH__NTFY_TOPIC`), never from config.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import requests

from vpa.config import PushConfig

#: The public ntfy.sh service. Self-hosting your own is possible but out
#: of scope for this milestone.
DEFAULT_NTFY_BASE_URL = "https://ntfy.sh"


class PushSender(Protocol):
    """Anything that can send a short push notification. Implement this
    in tests with a fake that just records what would have been sent."""

    def send(self, message: str) -> None: ...


class PushConfigError(Exception):
    """Raised when push configuration is missing or incomplete."""


@dataclass
class NtfyPushSender:
    """Sends a plain-text push notification via ntfy.sh."""

    topic: str
    base_url: str = DEFAULT_NTFY_BASE_URL

    def send(self, message: str) -> None:
        response = requests.post(
            f"{self.base_url}/{self.topic}",
            data=message.encode("utf-8"),
            timeout=10,
        )
        response.raise_for_status()


def build_push_sender(config: PushConfig) -> NtfyPushSender:
    """Build a real NtfyPushSender from configuration.

    Raises PushConfigError with a plain-language explanation if the
    topic name is missing.
    """
    if not config.ntfy_topic:
        raise PushConfigError(
            "Missing push setting: ntfy_topic. This must be set as an "
            "environment variable (VPA_PUSH__NTFY_TOPIC), never in "
            "config.yaml - see README.md."
        )
    return NtfyPushSender(topic=config.ntfy_topic)
