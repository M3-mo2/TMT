"""All user-visible Arabic strings and HTML renderers (RULES §7).

Handlers format values into these templates but never inline new Arabic
sentences. HTML parse mode everywhere; the only allowed decoration symbols are
``✓ ! × ? ›`` and ``=`` — no emoji (checked by tests/test_texts.py). Anything
user-controlled (titles, usernames, refs, raw input) must go through
:func:`esc` before interpolation.
"""

from __future__ import annotations

import html
from typing import Any, Iterable

from app.core.models import Account, AccountStatus, Check, CheckStatus, Job, JobStatus, UserStats
from app.core.broadcast_models import AudienceFilter

PARSE_MODE = "HTML"

__all__ = [
    "PARSE_MODE",
    "BUT_ACCOUNTS", "BUT_TRANSFER", "BUT_JOBS", "BUT_SETTINGS", "BUT_HELP", "BUT_MAIN",
    "BUT_ADD_ACCOUNT", "BUT_BACK", "BUT_CANCEL", "BUT_DELETE",
    "BUT_CONFIRM_DELETE", "BUT_REFRESH", "BUT_START_TRANSFER",
    "M_HELP", "M_BLOCKED", "M_PRIVATE_ONLY", "M_CANCELED",
    "M_ACCOUNTS_TITLE", "M_ACCOUNTS_EMPTY", "M_ADD_ACCOUNT_PROMPT",
    "M_ASK_CODE", "M_ASK_PASSWORD",     "M_LOGIN_EXPIRED", "M_LOGIN_CANCELLED", "M_RETRY_AFTER", "M_NO_ACTIVE_ACCOUNTS",
    "M_INVALID_PHONE",
    "M_DELETED", "M_DELETE_CONFIRM", "M_NOT_FOUND", "M_ERR_GENERIC", "M_ACCOUNT_BUSY",
    "M_WIZARD_TITLE", "M_PICK_ACCOUNT", "M_ASK_SOURCE",
    "M_SOURCE_RESOLVED", "M_CHECKING", "M_SAME_GROUP",
    "M_WIZARD_CANCELLED", "M_TRANSFER_STARTED", "M_JOBS_EMPTY",
    "M_JOB_CANCELLED", "M_JOB_NOT_CANCELLABLE", "M_INTERRUPTED_NOTE",
    "M_DELETED_ACCOUNT",
    "M_SETTINGS_TITLE", "M_SETTINGS_SUMMARY", "M_SETTING_PROMPT",
    "M_SETTING_INVALID", "M_SETTING_SAVED", "render_settings",
    "esc", "mask_phone", "normalize_phone", "render_main_menu", "render_admin_menu", "render_user_card", "render_preflight", "render_account_card",
    "render_jobs_list", "render_job_card", "render_progress_card",
    "render_final_summary", "job_glyph", "job_label", "account_status_label",
    "skip_reason_label", "phase_label",
    "render_broadcast_center", "render_audience_builder",
    "render_bcast_preview", "render_bcast_progress", "render_bcast_summary",
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
BUT_SETTINGS = "› الإعدادات"
BUT_RESET = "↺ إعادة تعيين"

# ---------------------------------------------------------------- generic

def render_main_menu(display_name: str, account_count: int, job_count: int, owner_id: int) -> str:
    name_line = f"↢ <b>أهلا بك يا <a href=\"tg://user?id={owner_id}\">{esc(display_name)}</a></b> 👋"
    return "\n".join([
        name_line,
        "",
        f"👤|عدد حساباتك المضافه ↼ <code>{account_count}</code>",
        f"🔰|عدد عمليات النقل ↼ <code>{job_count}</code>",
        f"💳|ايدي ↼ <code>{owner_id}</code>",
        "――――――――――――――――――――",
        "",
        "اختر ما تريد من القائمه ↓",
    ])


def render_admin_menu(user_count: int, jobs_count: int, completed_count: int) -> str:
    return "\n".join([
        "أهلا بك في لوحه التحكم .",
        "",
        f"👤|عدد مستخدمين الكلي ↼ <code>{user_count}</code>",
        f"🔰|عدد النقل الكلي ↼ <code>{jobs_count}</code>",
        f"🏷|عدد العمليات المكتمله ↼ <code>{completed_count}</code>",
        "――――――――――――――――――――",
        "",
        "استخدم الازرار للتنقل ↓",
    ])


def user_full_name(user: dict[str, Any]) -> str:
    """Best-effort public name for a user row: ``first last``, else ``#id``."""
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join(p for p in parts if p)
    return name or f"#{user['id']}"


def render_user_card(user: dict[str, Any], accounts: list[Account], stats: UserStats) -> str:
    """Admin user detail view: identity, last-seen, activity stats, accounts."""
    blocked = bool(user["is_blocked"])
    uname = user.get("username")
    lines = [
        f"<b>👤 {esc(user_full_name(user))}</b>",
        f"›|معرف التيليجرام ↼ <code>{user['id']}</code>",
        f"›|اسم المستخدم ↼ {f'@{esc(uname)}' if uname else '—'}",
        f"›|تاريخ الانضمام ↼ <code>{esc(user.get('created_at') or '')}</code>",
        f"›|آخر ظهور ↼ <code>{esc(user.get('updated_at') or '')}</code>",
        f"{GLYPH_FAIL if blocked else GLYPH_PASS}|الحالة ↼ <code>{'محظور' if blocked else 'نشط'}</code>",
        "",
        "⟡ إحصاءات المستخدم:",
        f"👤|الحسابات المضافة ↼ <code>{stats.accounts}</code>",
        f"🔰|إجمالي العمليات ↼ <code>{stats.jobs}</code>",
        f"✅|مكتملة ↼ <code>{stats.completed}</code>",
        f"{GLYPH_FAIL}|فاشلة ↼ <code>{stats.failed}</code>",
        f"›|نشطة الأن ↼ <code>{stats.active}</code>",
        "",
        "⟡ الحسابات المضافة له:",
    ]
    if accounts:
        for a in accounts:
            handle = f"@{esc(a.tg_username)}" if a.tg_username else f"<code>{a.tg_user_id}</code>"
            lim = f"، محدود حتى {esc(a.limited_until)}" if a.limited_until else ""
            lines.append(f"• {handle} — {esc(a.display_name)} ({account_status_label(a.status)}{lim})")
    else:
        lines.append("• لا توجد حسابات مضافة.")
    lines.append("")
    lines.append("استخدم الازرار للتنقل ↓")
    return "\n".join(lines)
M_HELP = (
    "📚|<b>طريقة الاستخدام</b>\n"
    "ــــــــــــــــــــــــــــــــــ\n\n"
    "› <code>1</code>. أضف حسابك الشخصي من قسم الحسابات.\n"
    "› <code>2</code>. ابدأ عملية نقل واختر الحساب والمجموعة المصدر والهدف.\n"
    "› <code>3</code>. راجع تقرير الفحص ثم أكد لبدء النقل.\n"
    "› <code>4</code>. تابع التقدم من بطاقة العملية وألغِ في أي وقت.\n"
    "ــــــــــــــــــــــــــــــــــ\n"
    "🔐|<b>الخصوصيه</b>\n"
    "الرمز وكلمة المرور يُحذفان من المحادثة فور الإرسال."
)
M_SETTINGS_TITLE = "⟡ الإعدادات"
M_SETTINGS_SUMMARY = "← اضغط على أي إعداد لتعديله أو احفظ القيم الافتراضية."
M_SETTING_PROMPT = "<b>{label}</b>\n↢ أرسل القيمة الجديدة.\n⋆<code>{current}</code>"
M_SETTING_INVALID = "× القيمة غير صالحة. أرسل عدداً صحيحاً."
M_SETTING_SAVED = "تم حفظ الإعداد."


#: (display_label, unit_label) — one entry per overridable config key.
_SETTING_SPECS: dict[str, tuple[str, str]] = {
    "max_members_per_job": ("⟡ الحد الأقصى للأعضاء في العملية", "عدد الأعضاء"),
    "invite_delay_seconds": ("⟡ التأخير بين الدعوات", "ثانية"),
    "flood_wait_max_seconds": ("⟡ الحد الأقصى لانتظار FloodWait", "ثانية"),
    "job_timeout_seconds": ("⟡ مهلة العملية", "ثانية"),
}


def render_settings(current: Config | None = None, values: dict[str, object] | None = None) -> str:
    lines = [M_SETTINGS_TITLE]
    lines.append("")
    if current is not None:
        for key, (label, unit) in _SETTING_SPECS.items():
            val = getattr(current, key)
            lines.append(f"{label} ↼ <code>{val} {unit}</code>")
    elif values:
        for key, (label, unit) in _SETTING_SPECS.items():
            val = values.get(key, "—")
            lines.append(f"{label} ↼ <code>{val} {unit}</code>")
    lines.append("")
    lines.append(M_SETTINGS_SUMMARY)
    return "\n".join(lines)
M_BLOCKED = "انت محظور من استخدام البوت، اذا كنت تعتقد ان هذا خطأ تواصل مع المالك @M3_mo2"
M_PRIVATE_ONLY = "افتح المحادثة الخاصة مع البوت."
M_CANCELED = "تم الإلغاء."
M_NOT_FOUND = "العنصر غير موجود."
M_ERR_GENERIC = "حدث خطأ غير متوقع، أعد المحاولة."
M_ACCOUNT_BUSY = "× الحساب مشغول الآن — عملية نقل جارية عليه، ألغِها أولاً."
M_DELETED_ACCOUNT = "حساب محذوف"

# ---------------------------------------------------------------- accounts

M_ACCOUNTS_TITLE = "<b>حساباتك المضافه فالبوت ↓</b>"
M_ACCOUNTS_EMPTY = "لا توجد حسابات مضافة بعد.\nأضف حسابك الأول للبدء."
M_ADD_ACCOUNT_PROMPT = (
    "⤸ أرسل رقم هاتف الحساب بالصيغة الدولية \n\n⋆مثال ← <code>+201xxxxxxxxx</code> "
    "(يمكن إدخال المسافات أو الشرطات، سيتم توحيده تلقائياً)"
)
M_ASK_CODE = "أرسل رمز التحقق الذي وصلك في تيليجرام.\nسيُحذف من المحادثة فور الإرسال."
M_ASK_PASSWORD = (
    "الحساب محمي بتحقق بخطوتين، أرسل كلمة المرور.\nسيُحذف من المحادثة فور الإرسال."
)
M_LOGIN_EXPIRED = "انتهت صلاحية عملية الدخول، ابدأ من جديد."
M_LOGIN_CANCELLED = "أُلغيت عملية الدخول."
M_RETRY_AFTER = "أعد المحاولة بعد {seconds} ثانية."
M_NO_ACTIVE_ACCOUNTS = "لا يوجد حساب نشط، أضف حساباً أو أعد تسجيل الدخول."
M_INVALID_PHONE = (
    "× الرقم غير صالح. أرسل رقم الهاتف بالصيغة الدولية، مثال: +201xxxxxxxxx"
)
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


def normalize_phone(raw: str) -> str:
    """Accept human-friendly international phone formats such as
    ``+20 10 0307 2694`` or ``+1 (202) 555-0123`` and collapse to a
    canonical ``+E.164`` form (digits only, leading ``+``)."""
    cleaned = raw.strip()
    sign = "+" if cleaned.startswith("+") else ""
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if not digits:
        return sign
    return sign + digits


def render_account_card(account: Account) -> str:
    handle = f"@{esc(account.tg_username)}" if account.tg_username else (
        f"<code>{esc(str(account.tg_user_id))}</code>"
    )
    return (
        f"<a href=\"tg://user?id={account.tg_user_id}\">{esc(account.display_name)}</a> ✿\n"
        f"👤|الحساب ↼ {handle}\n"
        f"📱|الهاتف ↼ <code>{esc(mask_phone(account.phone))}</code>\n"
        f"› الحالة ↼ {account_status_label(account.status)} ✓"
    )


# ---------------------------------------------------------------- transfers

M_WIZARD_TITLE = "<b>⇜ عملية نقل جديدة .</b>"
M_PICK_ACCOUNT = "اختر الحساب الذي سينفذ النقل ↓"
M_ASK_SOURCE = "ارسل يوزر او رابط المجموعه اللتي سيتم النقل منها الاعضاء \n𑗁"
M_SOURCE_RESOLVED = "تم تحديد المجموعه اللتي سيتم النقل منها ← <b>{title}</b>\n\nارسل يوزر او رابط المجموعه اللتي سيتم النقل اليها ⤾"
M_CHECKING = "<b>جارٍ الفحص…</b>"
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


def render_job_card(job: Job, account_name: str | None) -> str:
    glyph, label = _JOB_VIEW[job.status]
    account_line = esc(account_name) if account_name else M_DELETED_ACCOUNT
    lines = [
        f"<b>العملية <code>#{job.id}</code></b> — {glyph} {label}",
        f"👤|الحساب ↼ <b>{account_line}</b>",
        f"🔰|مسار عمليه النقل ↼ {esc(job.source_title)} ← {esc(job.dest_title)}",
        "",
        f"✅|تم اضافه ↼ <code>{job.invited}</code>",
        f"- تخطي ↼ <code>{job.skipped}</code>",
        f"- فشل ↼ <code>{job.failed}</code> من <code>{job.total}</code>",
    ]
    for reason, count in sorted(job.skip_reasons.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"› {skip_reason_label(reason)}: <code>{count}</code>")
    if job.error:
        lines.append(f"السبب: {esc(job.error)}")
    if job.status is JobStatus.COMPLETED:
        lines.append("")
        lines.append("✨|عمليه ناجحه .")
    elif job.status is JobStatus.FAILED:
        lines.append("")
        lines.append("✨|العمليه فشلت .")
    elif job.status is JobStatus.CANCELLED:
        lines.append("")
        lines.append("✨|تم الغي العمليه .")
    elif job.status is JobStatus.INTERRUPTED:
        lines.append("")
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
    glyph = GLYPH_INFO
    phase_ar = phase_label(phase)
    lines = [
        f"<b>العملية <code>#{job_id}</code></b> — {glyph} {phase_ar}",
        f"👤|الحاله ↼ <code>{done}/{total}</code>",
        f"🔰|مسار عمليه النقل ↼",
        "",
        f"✅|تم اضافه ↼ <code>{invited}</code>",
        f"- تخطي ↼ <code>{skipped}</code>",
        f"- فشل ↼ <code>{failed}</code>",
    ]
    if wait_left > 0:
        lines.append(f"› بانتظار تيليجرام: <code>{wait_left}</code> ثانية")
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
        f"✨|العمليه تمت .",
        f"<b>العملية <code>#{job_id}</code></b> — {glyph} {label}",
        f"✅|تم اضافه ↼ <code>{invited}</code>",
        f"› تخطي ↼ <code>{skipped}</code>",
        f"› فشل ↼ <code>{failed}</code>",
        "ــــــــــــــــــــــــــــــ",
    ]
    for reason, count in sorted((skip_reasons or {}).items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"› {skip_reason_label(reason)}: <code>{count}</code>")
    if error:
        lines.append(f"السبب: {esc(error)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- helpers


def esc(value: str) -> str:
    """Escape user-provided text for HTML interpolation (RULES §7)."""
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------- broadcasts

_BCAST_STATUS_VIEW: dict[str, tuple[str, str]] = {
    "draft": (GLYPH_INFO, "مسودة"),
    "scheduled": (GLYPH_INFO, "مجدولة"),
    "running": (GLYPH_INFO, "جارية"),
    "completed": (GLYPH_PASS, "مكتملة"),
    "cancelled": (GLYPH_NEUTRAL, "ملغاة"),
    "failed": (GLYPH_FAIL, "فاشلة"),
    "interrupted": (GLYPH_NEUTRAL, "متوقفة"),
}


def bcast_status_label(status: str) -> str:
    glyph, label = _BCAST_STATUS_VIEW.get(status, (GLYPH_WARN, status))
    return f"{glyph} {label}"


def _format_campaign_line(campaign: dict[str, Any]) -> str:
    glyph = _BCAST_STATUS_VIEW.get(campaign.get("status", ""), (GLYPH_INFO,))[0]
    label = campaign.get("label") or "بدون عنوان"
    return f"{glyph} <code>#{campaign['id']}</code> {esc(label)}"


def render_broadcast_center(
    drafts: list[dict[str, Any]],
    scheduled: list[dict[str, Any]],
    running: list[dict[str, Any]],
    completed: list[dict[str, Any]],
) -> str:
    lines = ["⟡ لوحة البث"]
    lines.append("")

    if drafts:
        lines.append("⟡ المسودات:")
        for b in drafts:
            lines.append(f"› {_format_campaign_line(b)}")
    else:
        lines.append("⟡ لا توجد مسودات.")
    lines.append("")

    if scheduled:
        lines.append("= المجدولة:")
        for b in scheduled:
            lines.append(f"› {_format_campaign_line(b)}")
    else:
        lines.append("= لا توجد مجدولة.")
    lines.append("")

    if running:
        lines.append("⋆ الجارية:")
        for b in running:
            lines.append(f"› {_format_campaign_line(b)}")
    else:
        lines.append("⋆ لا توجد جارية.")
    lines.append("")

    if completed:
        lines.append("✓ المكتملة:")
        for b in completed:
            lines.append(f"› {_format_campaign_line(b)}")
    else:
        lines.append("✓ لا توجد مكتملة.")
    lines.append("")
    lines.append("استخدم الأزرار للتنقل ↓")
    return "\n".join(lines)


def render_audience_builder(filters: AudienceFilter, user_count: int) -> str:
    def _flag(val: bool) -> str:
        return "✓" if val else "×"

    lines = ["⟡ بناء الجمهور المستهدف:"]
    lines.append(f"› الهدف ↼ <code>{esc(filters.target)}</code>")
    lines.append(f"{_flag(filters.with_accounts)} لديهم حسابات")
    lines.append(f"{_flag(filters.without_accounts)} بلا حسابات")
    lines.append(
        f"› حسابات >= <code>{filters.account_count_min if filters.account_count_min is not None else '—'}</code>"
    )
    lines.append(
        f"› حسابات <= <code>{filters.account_count_max if filters.account_count_max is not None else '—'}</code>"
    )
    lines.append(
        f"› مسجل منذ <code>{f'{filters.registered_days_ago} يوم' if filters.registered_days_ago else '—'}</code>"
    )
    lines.append(
        f"› آخر ظهور <code>{f'{filters.last_seen_days_ago} يوم' if filters.last_seen_days_ago else '—'}</code>"
    )
    lines.append(f"{_flag(filters.exclude_admins)} إخفاء المدراء")
    lines.append(f"{_flag(filters.exclude_previously_contacted)} استبعد المرسل إليهم")
    lines.append("")
    lines.append(f"⟡ عدد المستخدمين المستهدفين ↼ <code>{user_count}</code>")
    return "\n".join(lines)


def render_bcast_preview(
    campaign: dict[str, Any], recipient_count: int, est_duration: float
) -> str:
    mode_label = "نسخة" if campaign.get("mode") == "copy" else "مخصصة"
    lines = [
        "<b>⟡ معاينة البث</b>",
        "",
        f"› العنوان ↼ <code>{esc(campaign.get('label') or 'بدون عنوان')}</code>",
        f"› الوضع ↼ <code>{mode_label}</code>",
        f"› المستلمون ↼ <code>{recipient_count}</code>",
        f"› التقدير الزمني ↼ <code>{est_duration:.0f}</code> ثانية",
    ]
    lines.append("")
    lines.append("⇜ اضغط إرسال الآن للبدء أو جدولة للموعد اللاحق.")
    return "\n".join(lines)


def render_bcast_progress(campaign: dict[str, Any], rate: float) -> str:
    total = campaign.get("total_recipients", 0) or 0
    sent = campaign.get("sent", 0) or 0
    blocked = campaign.get("blocked", 0) or 0
    failed = campaign.get("failed", 0) or 0
    skipped = campaign.get("skipped", 0) or 0
    processed = sent + blocked + failed + skipped
    pct = f"{(processed / total * 100):.0f}%" if total else "0%"
    lines = [
        f"⟡ بث: <b>#{campaign.get('id', '?')}</b>",
        f"› التقدم ↼ <code>{processed}</code>/<code>{total}</code> ({pct})",
        f"✓ أرسلت ↼ <code>{sent}</code>",
        f"× محظور ↼ <code>{blocked}</code>",
        f"! فشل ↼ <code>{failed}</code>",
        f"› تم تخطيه ↼ <code>{skipped}</code>",
        f"= السرعة ↼ <code>{rate:.1f}</code>/ث",
    ]
    return "\n".join(lines)


def render_bcast_summary(campaign: dict[str, Any]) -> str:
    glyph, status_label = _BCAST_STATUS_VIEW.get(
        campaign.get("status", ""), (GLYPH_WARN, campaign.get("status", ""))
    )
    lines = [
        f"<b>⟡ الحملة #{campaign.get('id', '?')}</b>",
        "",
        f"› العنوان ↼ <code>{esc(campaign.get('label') or 'بدون عنوان')}</code>",
        f"{glyph} الحالة ↼ <code>{status_label}</code>",
        f"✓ أرسلت ↼ <code>{campaign.get('sent', 0) or 0}</code>",
        f"× محظور ↼ <code>{campaign.get('blocked', 0) or 0}</code>",
        f"! فشل ↼ <code>{campaign.get('failed', 0) or 0}</code>",
        f"› تم تخطيه ↼ <code>{campaign.get('skipped', 0) or 0}</code>",
        f"⟡ الإجمالي ↼ <code>{campaign.get('total_recipients', 0) or 0}</code>",
        f"= متوسط السرعة ↼ <code>{campaign.get('avg_rate') or '—'}</code>/ث",
    ]
    if campaign.get("scheduled_for"):
        lines.append(f"› مواعيد ↼ <code>{esc(campaign['scheduled_for'])}</code>")
    if campaign.get("error"):
        lines.append(f"› السبب ↼ {esc(campaign['error'])}")
    lines.append("")
    lines.append("استخدم الأزرار للتنقل ↓")
    return "\n".join(lines)
