"""Logging setup with defense-in-depth secret redaction.

Primary protection is the RULES.md §3 rule (never pass secrets to a logger).
This module adds a secondary filter that scrubs messages shaped like Telethon
session strings and neutralizes extra fields with sensitive names.
"""

from __future__ import annotations

import logging
import re
import sys

# Telethon StringSession payloads are long url-safe base64 blobs, typically
# prefixed by a version digit. Conservative: only scrub 100+ char runs.
_SESSION_LIKE = re.compile(r"\b[0-9][A-Za-z0-9_-]{99,}\b")

_SENSITIVE_EXTRA_KEYS = {
    "session",
    "session_string",
    "phone",
    "code",
    "login_code",
    "phone_code_hash",
    "password",
    "api_hash",
    "token",
    "bot_token",
    "key",
    "master_key",
}


class SecretRedactionFilter(logging.Filter):
    """Scrub session-like tokens from messages; mask sensitive extra keys."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if msg:
            record.msg = _SESSION_LIKE.sub("[REDACTED]", msg)
            record.args = None
        for key in list(record.__dict__):
            if key.lower() in _SENSITIVE_EXTRA_KEYS:
                record.__dict__[key] = "[REDACTED]"
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(SecretRedactionFilter())

    root = logging.getLogger("app")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
    root.propagate = False

    # Third-party noise down a notch unless debugging.
    logging.getLogger("telethon").setLevel(
        logging.DEBUG if level == "DEBUG" else logging.WARNING
    )
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
