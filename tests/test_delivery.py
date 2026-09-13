"""Tests for src/vpa/delivery/. No network calls - smtplib and requests
are replaced with fakes/mocks in every test here."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from vpa.config import DeadManConfig, EmailConfig, PushConfig
from vpa.delivery.deadman import (
    DeadManConfigError,
    HttpDeadManSwitch,
    build_deadman_switch,
)
from vpa.delivery.email import EmailConfigError, SmtpEmailSender, build_email_sender
from vpa.delivery.push import NtfyPushSender, PushConfigError, build_push_sender

# --- email ---------------------------------------------------------------


def test_build_email_sender_raises_when_incomplete():
    config = EmailConfig()  # nothing set

    with pytest.raises(EmailConfigError, match="from_addr"):
        build_email_sender(config)


def test_build_email_sender_succeeds_when_complete():
    config = EmailConfig(
        from_addr="me@example.com", to_addr="me@example.com", smtp_password="secret"
    )

    sender = build_email_sender(config)

    assert sender.from_addr == "me@example.com"
    assert sender.smtp_host == "smtp.gmail.com"


def test_smtp_email_sender_sends_over_starttls(monkeypatch):
    fake_smtp = MagicMock()
    fake_smtp.__enter__.return_value = fake_smtp
    smtp_class = MagicMock(return_value=fake_smtp)
    monkeypatch.setattr("smtplib.SMTP", smtp_class)

    sender = SmtpEmailSender(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        username="me@example.com",
        password="secret",
        from_addr="me@example.com",
        to_addr="me@example.com",
    )
    sender.send(subject="Hello", body="World")

    smtp_class.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
    fake_smtp.starttls.assert_called_once()
    fake_smtp.login.assert_called_once_with("me@example.com", "secret")
    fake_smtp.send_message.assert_called_once()
    sent_message = fake_smtp.send_message.call_args[0][0]
    assert sent_message["Subject"] == "Hello"
    assert sent_message.get_content().strip() == "World"


# --- push ------------------------------------------------------------------


def test_build_push_sender_raises_when_missing_topic():
    with pytest.raises(PushConfigError, match="ntfy_topic"):
        build_push_sender(PushConfig())


def test_build_push_sender_succeeds_when_configured():
    sender = build_push_sender(PushConfig(ntfy_topic="my-private-topic"))

    assert sender.topic == "my-private-topic"


def test_ntfy_push_sender_posts_to_topic_url(monkeypatch):
    mock_post = MagicMock()
    mock_post.return_value.raise_for_status = MagicMock()
    monkeypatch.setattr("requests.post", mock_post)

    sender = NtfyPushSender(topic="my-private-topic")
    sender.send("hello there")

    mock_post.assert_called_once_with(
        "https://ntfy.sh/my-private-topic", data=b"hello there", timeout=10
    )
    mock_post.return_value.raise_for_status.assert_called_once()


# --- dead-man's-switch -------------------------------------------------------


def test_build_deadman_switch_raises_when_missing_url():
    with pytest.raises(DeadManConfigError, match="base_url"):
        build_deadman_switch(DeadManConfig())


def test_build_deadman_switch_succeeds_when_configured():
    switch = build_deadman_switch(DeadManConfig(base_url="https://hc-ping.com/abc123"))

    assert switch.base_url == "https://hc-ping.com/abc123"


def test_deadman_switch_ping_success_hits_base_url(monkeypatch):
    mock_get = MagicMock()
    mock_get.return_value.raise_for_status = MagicMock()
    monkeypatch.setattr("requests.get", mock_get)

    HttpDeadManSwitch(base_url="https://hc-ping.com/abc123").ping_success()

    mock_get.assert_called_once_with("https://hc-ping.com/abc123", timeout=10)


def test_deadman_switch_ping_fail_hits_fail_url(monkeypatch):
    mock_get = MagicMock()
    mock_get.return_value.raise_for_status = MagicMock()
    monkeypatch.setattr("requests.get", mock_get)

    HttpDeadManSwitch(base_url="https://hc-ping.com/abc123").ping_fail()

    mock_get.assert_called_once_with("https://hc-ping.com/abc123/fail", timeout=10)
