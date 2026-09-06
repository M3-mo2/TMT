"""Single mapping from Telethon/MTProto failures to domain error kinds.

Every component reports failures through `classify_error` — no raw exception
text ever reaches the user (master prompt §10, §17), and retry policy lives
only here (RULES §5).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from telethon import errors


class ErrorKind(str, Enum):
    FLOOD_WAIT = "flood_wait"
    PEER_FLOOD = "peer_flood"
    ADMIN_REQUIRED = "admin_required"
    ENTITY_PRIVATE = "entity_private"
    USER_PRIVACY = "user_privacy"
    USER_NOT_MUTUAL = "user_not_mutual"
    USER_TOO_MANY = "user_too_many"
    USER_KICKED = "user_kicked"
    USER_DELETED = "user_deleted"
    AUTH_REVOKED = "auth_revoked"
    ACCOUNT_RESTRICTED = "account_restricted"
    INPUT_INVALID = "input_invalid"
    TRANSIENT = "transient"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class ClassifiedError:
    kind: ErrorKind
    message: str  # Arabic, user-facing
    retryable: bool = False
    wait_seconds: int | None = None  # FloodWait only
    job_fatal: bool = False  # abort the whole job
    account_fatal: bool = False  # mark the account unhealthy
    technical: str = ""


#: Per-member failures: counted as skips, never abort the job.
_PER_MEMBER_KINDS = frozenset(
    {
        ErrorKind.USER_PRIVACY,
        ErrorKind.USER_NOT_MUTUAL,
        ErrorKind.USER_TOO_MANY,
        ErrorKind.USER_KICKED,
        ErrorKind.USER_DELETED,
    }
)


def is_per_member(kind: ErrorKind) -> bool:
    return kind in _PER_MEMBER_KINDS


_AR = {
    ErrorKind.FLOOD_WAIT: (
        "تلقّى الحساب طلب انتظار مؤقت من تيليجرام بسبب كثافة العمليات."
    ),
    ErrorKind.PEER_FLOOD: (
        "قام تيليجرام بتقييد هذا الحساب من إضافة أعضاء مؤقتاً."
    ),
    ErrorKind.ADMIN_REQUIRED: (
        "العملية تتطلب صلاحيات إدارية لا يملكها الحساب في هذه المجموعة."
    ),
    ErrorKind.ENTITY_PRIVATE: (
        "لا يستطيع الحساب الوصول إلى هذه المجموعة (خاصة أو محجوبة عنه)."
    ),
    ErrorKind.USER_PRIVACY: (
        "إعدادات الخصوصية لدى العضو تمنع إضافته إلى المجموعة."
    ),
    ErrorKind.USER_NOT_MUTUAL: (
        "تيليجرام يمنع إضافة مستخدمين لا يوجد بينكم تواصل متبادل."
    ),
    ErrorKind.USER_TOO_MANY: (
        "المستخدم عضو في عدد أقصى من المجموعات ولا يمكن إضافته."
    ),
    ErrorKind.USER_KICKED: "لا يمكن إضافة هذا المستخدم لأنه محظور في المجموعة الهدف.",
    ErrorKind.USER_DELETED: "هذا الحساب محذوف أو غير متاح، تم تخطيه.",
    ErrorKind.AUTH_REVOKED: (
        "انتهت صلاحية تسجيل دخول الحساب أو تم إلغاؤه، يجب إعادة إضافة الحساب."
    ),
    ErrorKind.ACCOUNT_RESTRICTED: "حساب تيليجرام نفسه مقيّد ولا يمكنه تنفيذ العملية.",
    ErrorKind.INPUT_INVALID: "البيانات المُدخلة غير مقبولة من تيليجرام.",
    ErrorKind.TRANSIENT: "حدث خطأ شبكة مؤقت أثناء الاتصال بتيليجرام.",
    ErrorKind.UNEXPECTED: "حدث خطأ غير متوقع أثناء التنفيذ.",
}

# Login-specific Arabic messages (only used by the login flow).
_LOGIN_AR = {
    "PHONE_INVALID": "رقم الهاتف غير صحيح 🚶\nاعد ادخاله بالصيغه العالميه • ",
    "PHONE_BANNED": "رقم الهاتف محظور من تيليجرام.",
    "PHONE_UNOCCUPIED": "لا يوجد حساب تيليجرام مرتبط بهذا الرقم.",
    "CODE_INVALID": "الرمز غير صحيح، تحقق من الرمز وأعد إدخاله.",
    "CODE_EXPIRED": "انتهت صلاحية الرمز، اطلب رمزاً جديداً.",
    "CODE_EMPTY": "لم يتم إدخال الرمز.",
    "PASSWORD_INVALID": "كلمة مرور التحقق بخطوتين غير صحيحة.",
    "PHONE_FLOOD": "طلب رمز الدخول مرات كثيرة، انتظر قليلاً قبل المحاولة مجدداً.",
}


def login_message(name: str) -> str:
    return _LOGIN_AR.get(name, _AR[ErrorKind.UNEXPECTED])


def classify_error(exc: BaseException) -> ClassifiedError:
    technical = f"{type(exc).__name__}: {exc}"

    if isinstance(exc, errors.FloodWaitError):
        wait = int(getattr(exc, "seconds", 0) or 0)
        return ClassifiedError(
            ErrorKind.FLOOD_WAIT, _AR[ErrorKind.FLOOD_WAIT],
            wait_seconds=max(wait, 1), technical=technical,
        )

    if isinstance(exc, errors.PeerFloodError):
        return ClassifiedError(
            ErrorKind.PEER_FLOOD, _AR[ErrorKind.PEER_FLOOD],
            job_fatal=True, account_fatal=True, technical=technical,
        )

    if isinstance(
        exc,
        (
            errors.ChatAdminRequiredError,
            errors.ChatWriteForbiddenError,
            errors.ChatSendMediaForbiddenError,
        ),
    ):
        return ClassifiedError(
            ErrorKind.ADMIN_REQUIRED, _AR[ErrorKind.ADMIN_REQUIRED],
            job_fatal=True, technical=technical,
        )

    if isinstance(
        exc,
        (
            errors.ChannelPrivateError,
            errors.ChatForbiddenError,
            errors.ChannelInvalidError,
        ),
    ):
        return ClassifiedError(
            ErrorKind.ENTITY_PRIVATE, _AR[ErrorKind.ENTITY_PRIVATE],
            job_fatal=True, technical=technical,
        )

    if isinstance(exc, errors.UserPrivacyRestrictedError):
        return ClassifiedError(ErrorKind.USER_PRIVACY, _AR[ErrorKind.USER_PRIVACY])

    if isinstance(exc, errors.UserNotMutualContactError):
        return ClassifiedError(ErrorKind.USER_NOT_MUTUAL, _AR[ErrorKind.USER_NOT_MUTUAL])

    if isinstance(exc, errors.UserChannelsTooMuchError):
        return ClassifiedError(ErrorKind.USER_TOO_MANY, _AR[ErrorKind.USER_TOO_MANY])

    if isinstance(exc, errors.UserKickedError):
        return ClassifiedError(ErrorKind.USER_KICKED, _AR[ErrorKind.USER_KICKED])

    if isinstance(
        exc,
        (
            errors.UserDeactivatedBanError,
            errors.UserDeactivatedError,
            errors.InputUserDeactivatedError,
        ),
    ):
        return ClassifiedError(ErrorKind.USER_DELETED, _AR[ErrorKind.USER_DELETED])

    if isinstance(
        exc,
        (
            errors.AuthKeyUnregisteredError,
            errors.AuthKeyDuplicatedError,
            errors.SessionExpiredError,
            errors.SessionRevokedError,
            errors.UnauthorizedError,
        ),
    ):
        return ClassifiedError(
            ErrorKind.AUTH_REVOKED, _AR[ErrorKind.AUTH_REVOKED],
            account_fatal=True, technical=technical,
        )

    if isinstance(exc, errors.UserIsBlockedError):
        return ClassifiedError(
            ErrorKind.ACCOUNT_RESTRICTED, _AR[ErrorKind.ACCOUNT_RESTRICTED],
            technical=technical,
        )

    if isinstance(
        exc,
        (
            errors.PeerIdInvalidError,
            errors.UserIdInvalidError,
            errors.ChatIdInvalidError,
            errors.InviteHashExpiredError,
            errors.InviteHashInvalidError,
        ),
    ):
        return ClassifiedError(ErrorKind.INPUT_INVALID, _AR[ErrorKind.INPUT_INVALID])

    if isinstance(
        exc, (errors.RPCError, ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)
    ):
        # Generic RPC / network trouble: retryable per-item, bounded by caller.
        return ClassifiedError(
            ErrorKind.TRANSIENT, _AR[ErrorKind.TRANSIENT], retryable=True,
            technical=technical,
        )

    return ClassifiedError(ErrorKind.UNEXPECTED, _AR[ErrorKind.UNEXPECTED], technical=technical)


# --- login-specific classification -------------------------------------------


@dataclass(frozen=True, slots=True)
class LoginFailure(Exception):
    """A classified, user-presentable login failure (Arabic message)."""

    key: str
    message: str
    wait_seconds: int | None = None

    def __str__(self) -> str:  # pragma: no cover - repr convenience
        return f"LoginFailure({self.key})"


def classify_login_error(exc: BaseException) -> LoginFailure:
    if isinstance(exc, errors.FloodWaitError):
        return LoginFailure(
            "PHONE_FLOOD", login_message("PHONE_FLOOD"), wait_seconds=int(getattr(exc, "seconds", 0) or 0)
        )
    if isinstance(exc, errors.PhoneNumberInvalidError):
        return LoginFailure("PHONE_INVALID", login_message("PHONE_INVALID"))
    if isinstance(exc, errors.PhoneNumberBannedError):
        return LoginFailure("PHONE_BANNED", login_message("PHONE_BANNED"))
    if isinstance(exc, errors.PhoneNumberUnoccupiedError):
        return LoginFailure("PHONE_UNOCCUPIED", login_message("PHONE_UNOCCUPIED"))
    if isinstance(exc, errors.PhoneCodeInvalidError):
        return LoginFailure("CODE_INVALID", login_message("CODE_INVALID"))
    if isinstance(exc, errors.PhoneCodeExpiredError):
        return LoginFailure("CODE_EXPIRED", login_message("CODE_EXPIRED"))
    if isinstance(exc, errors.PhoneCodeEmptyError):
        return LoginFailure("CODE_EMPTY", login_message("CODE_EMPTY"))
    if isinstance(exc, errors.PhoneNumberFloodError):
        return LoginFailure("PHONE_FLOOD", login_message("PHONE_FLOOD"))
    if isinstance(exc, errors.PasswordHashInvalidError):
        return LoginFailure("PASSWORD_INVALID", login_message("PASSWORD_INVALID"))
    if isinstance(exc, errors.SessionPasswordNeededError):
        return LoginFailure("PASSWORD_REQUIRED", login_message("CODE_EMPTY"))
    technical = f"{type(exc).__name__}: {exc}"
    return LoginFailure("UNEXPECTED", _AR[ErrorKind.UNEXPECTED])
