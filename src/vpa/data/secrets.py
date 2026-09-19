"""Reading API keys from the production secrets file at run time.

This is the one place in the codebase that reads a `.env`-style file, and
it reads only `~/vpa-secrets/.env` (Architecture v2 Section 8), only when a
data-ingestion command is actually run, and only the named keys it needs.
The project owner approved this exception to CLAUDE.md rule 3 for the
production data loader (Milestone 2, LEDGER-1 discussion). Tests never
touch the real file - they pass a temporary one.

Secret values are never printed or logged. Errors name the key, never
its value.
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SECRETS_FILE = Path.home() / "vpa-secrets" / ".env"


class SecretError(Exception):
    """Raised when a required secret cannot be loaded."""


def load_secret(name: str, secrets_file: Path = DEFAULT_SECRETS_FILE) -> str:
    """Return the value of `name` from a KEY=VALUE secrets file."""
    if not secrets_file.is_file():
        raise SecretError(f"Secrets file not found: {secrets_file}")
    mode = secrets_file.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning(
            "Secrets file %s is readable by other users; it should be mode 600", secrets_file
        )

    for line in secrets_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if key == name:
            value = value.strip().strip("'\"")
            if not value:
                raise SecretError(f"{name} is empty in {secrets_file}")
            return value
    raise SecretError(f"{name} is not set in {secrets_file}")


class RedactSecrets(logging.Filter):
    """Belt and braces: replaces any secret value that somehow ends up in
    a log message with [REDACTED]."""

    def __init__(self, *secrets: str) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if any(s in message for s in self._secrets):
            for s in self._secrets:
                message = message.replace(s, "[REDACTED]")
            record.msg, record.args = message, None
        return True
