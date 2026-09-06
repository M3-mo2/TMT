"""All user-visible Arabic strings and HTML renderers (RULES §7).

Handlers format values into these templates but never inline new Arabic
sentences. HTML parse mode everywhere; the only allowed decoration symbols are
``✓ ! × ? ›`` and ``=`` — no emoji (checked by tests/test_texts.py). Anything
user-controlled (titles, usernames, refs, raw input) must go through
:func:`esc` before interpolation.
"""

from __future__ import annotations

import html
from typing import Iterable

from app.core.models import Account, AccountStatus, Check, CheckStatus, Job, JobStatus

PARSE_MODE = "HTML"

__all__ = [
    "PARSE_MODE",
    "BUT_ACCOUNTS", "BUT_TRANSFER", "BUT_JOBS", "BUT_HELP", "BUT_MAIN",
    "BUT_ADD_ACCOUNT", "BUT_BACK", "BUT_CANCEL", "BUT_DELETE",
    "BUT_CONFIRM_DELETE", "BUT_REFRESH", "BUT_START_TRANSFER",
    "M_MAIN", "M_HELP", "M_BLOCKED", "M_PRIVATE_ONLY", "M_CANCELED",
    "M_ACCOUNTS_TITLE", "M_ACCOUNTS_EMPTY", "M_ADD_ACCOUNT_PROMPT",
    "M_ASK_CODE", "M_ASK_PASSWORD", "M_LOGIN_EXPIRED", "M_LOGIN_CANCELLED",
    "M_DELETED", "M_DELETE_CONFIRM", "M_NOT_FOUND", "M_ERR_GENERIC",
    "M_WIZARD_TITLE", "M_PICK_ACCOUNT", "M_ASK_SOURCE",
    "M_SOURCE_RESOLVED", "M_CHECKING", "M_SAME_GROUP",
    "M_WIZARD_CANCELLED", "M_TRANSFER_STARTED", "M_JOBS_EMPTY",
    "M_JOB_CANCELLED", "M_JOB_NOT_CANCELLABLE", "M_INTERRUPTED_NOTE",
    "M_DELETED_ACCOUNT",
    "esc", "mask_phone", "render_preflight", "render_account_card",
    "render_jobs_list", "render_job_card", "render_progress_card",
    "render_final_summary", "job_glyph", "job_label", "account_status_label",
    "skip_reason_label", "phase_label",
]

# ---------------------------------------------------------------- decorations

GLYPH_PASS = "✓"
GLYPH_WARN = "!"
GLYPH_FAIL = "×"
GLYPH_UNKNOWN = "?"
GLYPH_INFO = "›"
GLYPH_NEUTRAL = "="

# ---------------------------------------------------------------- buttons

BUT_ACCOUNTS = "› الحسابات"
BUT_TRANSFER = "› النقل"
BUT_JOBS = "› العمليات"
BUT_HELP = "› المساعدة"
BUT_MAIN = "› القائمة الرئيسية"
BUT_ADD_ACCOUNT = "› إضافة حساب"
BUT_BACK = "› رجوع"
BUT_CANCEL = "× إلغاء"
BUT_DELETE = "× حذف"
BUT_CONFIRM_DELETE = "× تأكيد الحذف"
BUT_REFRESH = "› تحديث"
BUT_START_TRANSFER = "› بدء النقل"

# ---------------------------------------------------------------- generic

M_MAIN = "<b>أهلاً بك في بوت نقل الأعضاء</b>\nاختر ما تريد من القائمة."
M_HELP = (
    "<b>طريقة الاستخدام</b>\n"
    "› أضف حسابك الشخصي من قسم الحسابات.\n"
    "› ابدأ عملية نقل واختر الحساب والمجموعة المصدر والهدف.\n"
    "› راجع تقرير الفحص ثم أكد لبدء النقل.\n"
    "› تابع التقدم من بطاقة العملية وألغِ في أي وقت.\n"
    "الرمز وكلمة المرور يُحذفان من المحادثة فور الإرسال."
)
M_BLOCKED = "لا يمكنك استخدام هذا البوت."
M_PRIVATE_ONLY = "افتح المحادثة الخاصة مع البوت."
M_CANCELED = "تم الإلغاء."
M_NOT_FOUND = "العنصر غير موجود."
M_ERR_GENERIC = "حدث خطأ غير متوقع، أعد المحاولة."
M_DELETED_ACCOUNT = "حساب محذوف"

# ---------------------------------------------------------------- accounts

M_ACCOUNTS_TITLE = "<b>حساباتك</b>"
M_ACCOUNTS_EMPTY = "لا توجد حسابات مضافة بعد.\nأضف حسابك الأول للبدء."
M_ADD_ACCOUNT_PROMPT = (
    "أرسل رقم هاتف الحساب بالصيغة الدولية، مثال:\n<code>+201xxxxxxxxx</code>"
)
M_ASK_CODE = "أرسل رمز التحقق الذي وصلك في تيليجرام.\nسيُحذف من المحادثة فور الإرسال."
M_ASK_PASSWORD = (
    "الحساب محمي بتحقق بخطوتين، أرسل كلمة المرور.\nسيُحذف من المحادثة فور الإرسال."
)
M_LOGIN_EXPIRED = "انتهت صلاحية عملية الدخول، ابدأ من جديد."
M_LOGIN_CANCELLED = "أُلغيت عملية الدخول."
M_RETRY_AFTER = "أعد المحاولة بعد {seconds} ثانية."
M_NO_ACTIVE_ACCOUNTS = "لا يوجد حساب نشط، أضف حساباً أو أعد تسجيل الدخول."
M_DELETED = "حُذف الحساب."
M_DELETE_CONFIRM = "هل تريد حذف الحساب <b>{name}</b>؟"
M_SAVED = "تم حفظ الحساب."

_ACCOUNT_STATUS_AR: dict[AccountStatus, str] = {
    AccountStatus.ACTIVE: "نشط",
    AccountStatus.UNAUTHORIZED: "بحاجة إلى إعادة تسجيل الدخول",
    AccountStatus.LIMITED: "محدود من تيليجرام",
}


def account_status_label(status: AccountStatus) -> str:
    return _ACCOUNT_STATUS_AR[status]


def mask_phone(phone: str) -> str:
    """Keep the leading plus and country prefix, mask all but the last 4
    digits: ``+20•••••1234`` style."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) <= 4:
        return "•" * len(digits)
    head = digits[:2]
    tail = digits[-4:]
    return "+" + head + "•" * max(1, len(digits) - len(head) - len(tail)) + tail


def render_account_card(account: Account) -> str:
    handle = f"@{esc(account.tg_username)}" if account.tg_username else (
        f"<code>{esc(str(account.tg_user_id))}</code>"
    )
    return (
        f"<b>{esc(account.display_name)}</b>\n"
        f"الحساب: {handle}\n"
        f"الهاتف: <code>{esc(mask_phone(account.phone))}</code>\n"
        f"الحالة: {account_status_label(account.status)}"
    )


# ---------------------------------------------------------------- transfers

M_WIZARD_TITLE = "<b>عملية نقل جديدة</b>"
M_PICK_ACCOUNT = "اختر الحساب الذي سينفذ النقل:"
M_ASK_SOURCE = "أرسل مرجع المجموعة المصدر: @اسم أو رابط أو معرف رقمي."
M_SOURCE_RESOLVED = "تم تحديد المصدر: <b>{title}</b>\nأرسل مرجع المجموعة الهدف: @اسم أو رابط أو معرف رقمي."
M_CHECKING = "جارٍ الفحص…"
M_SAME_GROUP = "المصدر والهدف هما نفس المجموعة، اختر هدفاً مختلفاً."
M_WIZARD_CANCELLED = "أُلغيت العملية، يمكنك البدء من جديد."
M_TRANSFER_STARTED = (
    "<b>بدأت العملية</b>\nستجد بطاقة التقدم هنا ويمكنك الإلغاء في أي وقت."
)
PREFLIGHT_TITLE = "<b>نتيجة الفحص</b>"
PREFLIGHT_BLOCKED = "لا يمكن البدء قبل معالجة الإخفاقات أعلاه."

_CHECK_GLYPH: dict[CheckStatus, str] = {
    CheckStatus.PASS: GLYPH_PASS,
    CheckStatus.WARN: GLYPH_WARN,
    CheckStatus.FAIL: GLYPH_FAIL,
    CheckStatus.UNKNOWN: GLYPH_UNKNOWN,
    CheckStatus.INFO: GLYPH_INFO,
}


def render_preflight(checks: Iterable[Check]) -> str:
    lines = [PREFLIGHT_TITLE]
    lines.extend(f"{_CHECK_GLYPH[c.status]} {esc(c.message)}" for c in checks)
    return "\n".join(lines)


# ---------------------------------------------------------------- jobs

M_JOBS_EMPTY = "لا توجد عمليات بعد."
M_JOB_CANCELLED = "أُلغيت العملية."
M_JOB_NOT_CANCELLABLE = "لا يمكن إلغاء العملية في حالتها الحالية."
M_INTERRUPTED_NOTE = (
    "توقفت العملية بسبب إعادة تشغيل الخادم، يمكنك بدء عملية جديدة بنفس الإعدادات."
)

_JOB_VIEW: dict[JobStatus, tuple[str, str]] = {
    JobStatus.CREATED: (GLYPH_INFO, "قيد التهيئة"),
    JobStatus.VALIDATING: (GLYPH_INFO, "قيد الفحص"),
    JobStatus.QUEUED: (GLYPH_INFO, "في الانتظار"),
    JobStatus.RUNNING: (GLYPH_INFO, "جارية"),
    JobStatus.COMPLETED: (GLYPH_PASS, "مكتملة"),
    JobStatus.FAILED: (GLYPH_FAIL, "فاشلة"),
    JobStatus.CANCELLED: (GLYPH_NEUTRAL, "ملغاة"),
    JobStatus.INTERRUPTED: (GLYPH_NEUTRAL, "متوقفة"),
}


def job_glyph(status: JobStatus) -> str:
    return _JOB_VIEW[status][0]


def job_label(status: JobStatus) -> str:
    return _JOB_VIEW[status][1]


SKIP_BOT_AR = "بوت"
SKIP_ALREADY_MEMBER_AR = "عضو بالفعل"
SKIP_PRIVACY_AR = "خصوصية المستخدم"
SKIP_NOT_MUTUAL_AR = "لا يوجد تابع متبادل"
SKIP_TOO_MANY_AR = "وصل حد المجموعات للمستخدم"
SKIP_KICKED_AR = "محظور من المجموعة"
SKIP_DELETED_AR = "حساب محذوف"
SKIP_OTHER_AR = "أخرى"

_SKIP_REASONS_AR: dict[str, str] = {
    "bot": SKIP_BOT_AR,
    "already_member": SKIP_ALREADY_MEMBER_AR,
    "privacy": SKIP_PRIVACY_AR,
    "not_mutual": SKIP_NOT_MUTUAL_AR,
    "channels_too_much": SKIP_TOO_MANY_AR,
    "kicked": SKIP_KICKED_AR,
    "deleted_account": SKIP_DELETED_AR,
    "other": SKIP_OTHER_AR,
}


def skip_reason_label(reason: str) -> str:
    return _SKIP_REASONS_AR.get(reason, SKIP_OTHER_AR)


PHASE_FETCHING_DEST_AR = "جلب أعضاء المجموعة الهدف"
PHASE_FETCHING_SOURCE_AR = "جلب قائمة الأعضاء"
PHASE_INVITING_AR = "إرسال الدعوات"
PHASE_WAITING_AR = "انتظار حد تيليجرام"

_PHASES_AR: dict[str, str] = {
    "fetching_dest": PHASE_FETCHING_DEST_AR,
    "fetching_source": PHASE_FETCHING_SOURCE_AR,
    "inviting": PHASE_INVITING_AR,
    "waiting": PHASE_WAITING_AR,
}


def phase_label(phase: str) -> str:
    return _PHASES_AR.get(phase, phase)


def render_jobs_list(jobs: list[Job]) -> str:
    lines = ["<b>آخر العمليات</b>"]
    for job in jobs:
        glyph, label = _JOB_VIEW[job.status]
        lines.append(f"{glyph} <code>#{job.id}</code> {label} — {esc(job.source_title)} ← {esc(job.dest_title)}")
    return "\n".join(lines)


def _skip_lines(skip_reasons: dict[str, int]) -> list[str]:
    ordered = sorted(skip_reasons.items(), key=lambda kv: kv[1], reverse=True)
    return [f"{GLYPH_INFO} {skip_reason_label(reason)}: {count}" for reason, count in ordered]


def render_job_card(job: Job, account_name: str | None) -> str:
    glyph, label = _JOB_VIEW[job.status]
    account_line = esc(account_name) if account_name else M_DELETED_ACCOUNT
    lines = [
        f"<b>العملية <code>#{job.id}</code></b> — {glyph} {label}",
        f"الحساب: {account_line}",
        f"المسار: {esc(job.source_title)} ← {esc(job.dest_title)}",
        f"دُعي: {job.invited} | تخطي: {job.skipped} | فشل: {job.failed} من {job.total}",
    ]
    lines.extend(_skip_lines(job.skip_reasons))
    if job.error:
        lines.append(f"السبب: {esc(job.error)}")
    if job.status is JobStatus.INTERRUPTED:
        lines.append(M_INTERRUPTED_NOTE)
    return "\n".join(lines)


def render_progress_card(
    job_id: int,
    phase: str,
    done: int,
    total: int,
    invited: int,
    skipped: int,
    failed: int,
    wait_left: int = 0,
    note: str = "",
) -> str:
    lines = [
        f"<b>العملية <code>#{job_id}</code></b> — {esc(phase_label(phase))}",
        f"تم: {done}/{total}",
        f"دُعي: {invited} | تخطي: {skipped} | فشل: {failed}",
    ]
    if wait_left > 0:
        lines.append(f"بانتظار تيليجرام: {wait_left} ثانية")
    if note:
        lines.append(esc(note))
    return "\n".join(lines)


def render_final_summary(
    job_id: int,
    status: JobStatus,
    invited: int,
    skipped: int,
    failed: int,
    error: str | None,
    skip_reasons: dict[str, int] | None = None,
) -> str:
    glyph, label = _JOB_VIEW[status]
    lines = [
        f"<b>انتهت العملية <code>#{job_id}</code></b> — {glyph} {label}",
        f"دُعي: {invited} | تخطي: {skipped} | فشل: {failed}",
    ]
    lines.extend(_skip_lines(skip_reasons or {}))
    if error:
        lines.append(f"السبب: {esc(error)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- helpers


def esc(value: str) -> str:
    """Escape user-provided text for HTML interpolation (RULES §7)."""
    return html.escape(str(value), quote=True)
