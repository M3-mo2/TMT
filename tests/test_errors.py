"""Tests for app.tg.errors: the failure classification table (RULES §5)."""

from __future__ import annotations

from dataclasses import fields

import pytest
from telethon import errors

from app.tg.errors import ClassifiedError, ErrorKind, classify_error, is_per_member

# (exception, expected kind, expected flags) — representative of every rule.
_CASES: list[tuple[BaseException | type[BaseException], ErrorKind, dict[str, object]]] = [
    (errors.FloodWaitError(request=None, capture=30), ErrorKind.FLOOD_WAIT, {}),
    (
        errors.PeerFloodError(request=None),
        ErrorKind.PEER_FLOOD,
        {"job_fatal": True, "account_fatal": True},
    ),
    (
        errors.ChatAdminRequiredError(request=None),
        ErrorKind.ADMIN_REQUIRED,
        {"job_fatal": True},
    ),
    (
        errors.ChannelPrivateError(request=None),
        ErrorKind.ENTITY_PRIVATE,
        {"job_fatal": True},
    ),
    (errors.UserPrivacyRestrictedError(request=None), ErrorKind.USER_PRIVACY, {}),
    (errors.UserNotMutualContactError(request=None), ErrorKind.USER_NOT_MUTUAL, {}),
    (errors.UserChannelsTooMuchError(request=None), ErrorKind.USER_TOO_MANY, {}),
    (errors.UserKickedError(request=None), ErrorKind.USER_KICKED, {}),
    (errors.InputUserDeactivatedError(request=None), ErrorKind.USER_DELETED, {}),
    (
        errors.AuthKeyUnregisteredError(request=None),
        ErrorKind.AUTH_REVOKED,
        {"account_fatal": True},
    ),
    (errors.PeerIdInvalidError(request=None), ErrorKind.INPUT_INVALID, {}),
    (ConnectionError("connection reset"), ErrorKind.TRANSIENT, {"retryable": True}),
    (TimeoutError("timed out"), ErrorKind.TRANSIENT, {"retryable": True}),
    (RuntimeError("kaboom"), ErrorKind.UNEXPECTED, {}),
]


@pytest.mark.parametrize(
    ("exc", "kind", "flags"),
    [(case[0], case[1], case[2]) for case in _CASES],
    ids=[case[1].value for case in _CASES],
)
def test_classification_table(
    exc: BaseException, kind: ErrorKind, flags: dict[str, object]
) -> None:
    result = classify_error(exc)
    assert result.kind is kind
    for flag, expected in flags.items():
        assert getattr(result, flag) is expected, flag
    assert result.message  # every classified error carries a user-facing message


def test_flood_wait_carries_wait_seconds() -> None:
    result = classify_error(errors.FloodWaitError(request=None, capture=30))
    assert result.kind is ErrorKind.FLOOD_WAIT
    assert result.wait_seconds == 30


def test_per_member_failures_never_abort_the_job() -> None:
    for exc, kind, _ in _CASES:
        result = classify_error(exc)
        if is_per_member(result.kind):
            assert not result.job_fatal


def test_is_per_member_kinds() -> None:
    per_member = {
        ErrorKind.USER_PRIVACY,
        ErrorKind.USER_NOT_MUTUAL,
        ErrorKind.USER_TOO_MANY,
        ErrorKind.USER_KICKED,
        ErrorKind.USER_DELETED,
    }
    for kind in ErrorKind:
        assert is_per_member(kind) is (kind in per_member), kind


def test_technical_field_contains_exception_identity() -> None:
    result = classify_error(RuntimeError("kaboom"))
    assert "RuntimeError" in result.technical
    assert "kaboom" in result.technical


def test_classified_error_has_core_flags() -> None:
    names = {f.name for f in fields(ClassifiedError)}
    assert {"kind", "message", "retryable", "wait_seconds", "job_fatal",
            "account_fatal", "technical"} <= names
