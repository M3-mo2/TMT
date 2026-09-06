"""Tests for the SecretRedactionFilter in app.logging_setup (RULES §3)."""

from __future__ import annotations

import logging

from app.logging_setup import SecretRedactionFilter


def make_record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=message, args=None, exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    filter_ = SecretRedactionFilter()
    assert filter_.filter(record) is True
    return record


def test_session_like_token_is_redacted() -> None:
    token = "1" + "A" * 100  # 101 chars, session-string shaped
    record = make_record(f"loaded session {token} for account")
    assert token not in record.msg
    assert "[REDACTED]" in record.msg


def test_short_tokens_are_left_alone() -> None:
    record = make_record("job 12 finished, invited 45 members")
    assert record.msg == "job 12 finished, invited 45 members"


def test_sensitive_extra_is_masked() -> None:
    record = make_record(
        "adding account", session="1Secret", password="hunter2", code="12345"
    )
    assert record.session == "[REDACTED]"
    assert record.password == "[REDACTED]"
    assert record.code == "[REDACTED]"


def test_nonsensitive_extra_is_untouched() -> None:
    record = make_record("progress", job_id=7, invited=10)
    assert record.job_id == 7
    assert record.invited == 10
