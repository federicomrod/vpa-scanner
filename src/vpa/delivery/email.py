"""Sending the report by email, over SMTP.

Written to work with Gmail out of the box: `smtp_host`/`smtp_port`
default to Gmail's settings, and the username Gmail expects is your
full email address. Gmail requires an "App Password" rather than your
normal password - see README.md for how to create one. That password
is a secret and must come from an environment variable
(`VPA_EMAIL__SMTP_PASSWORD`), never from config.yaml.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from vpa.config import EmailConfig


class EmailSender(Protocol):
    """Anything that can send an email. Implement this in tests with a
    fake that just records what would have been sent."""

    def send(self, *, subject: str, body: str) -> None: ...


class EmailConfigError(Exception):
    """Raised when email configuration is missing or incomplete."""


@dataclass
class SmtpEmailSender:
    """Sends email over SMTP with STARTTLS - works with Gmail."""

    smtp_host: str
    smtp_port: int
    username: str
    password: str
    from_addr: str
    to_addr: str

    def send(self, *, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.from_addr
        message["To"] = self.to_addr
        message.set_content(body)

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.username, self.password)
            smtp.send_message(message)


def build_email_sender(config: EmailConfig) -> SmtpEmailSender:
    """Build a real SmtpEmailSender from configuration.

    Raises EmailConfigError with a plain-language explanation if
    anything required is missing.
    """
    missing = [
        name
        for name, value in (
            ("from_addr", config.from_addr),
            ("to_addr", config.to_addr),
            ("smtp_password", config.smtp_password),
        )
        if not value
    ]
    if missing:
        raise EmailConfigError(
            "Missing email settings: "
            + ", ".join(missing)
            + ". These must be set as environment variables (e.g. "
            "VPA_EMAIL__SMTP_PASSWORD), never in config.yaml - see README.md."
        )
    return SmtpEmailSender(
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        username=config.from_addr,  # Gmail logs in with the full address.
        password=config.smtp_password,
        from_addr=config.from_addr,
        to_addr=config.to_addr,
    )
