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
