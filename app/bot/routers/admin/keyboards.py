"""Admin keyboards — builder functions returning InlineKeyboardMarkup."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.bot.routers.admin import callbacks as C
from app.core.broadcast_models import AudienceFilter
from app.bot.texts import esc as _esc, user_full_name


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def admin_menu_kb() -> InlineKeyboardMarkup:
    """Top-level admin menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("👥 المستخدمون", C.USERS),
                _btn("↢ الاشتراك الإجباري", C.CH_SUBSCRIPTION),
            ],
            [
                _btn("📊 الإحصاءات", C.STATS),
                _btn("📢 البث", C.BCAST),
            ],
            [
                _btn("🔔 الإشعارات", C.NOTIFY),
            ],
            [
                _btn("⚙️ الإعدادات", C.SETTINGS),
            ],
        ]
    )


def users_list_kb(users: list[dict], page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Paginated list of users (9 per page) — label by name/username, not id.

    Clicking a name opens the user's detail card (RULES §4 isolation is enforced
    server-side; callback data is input, not authorization)."""
    rows = []
    for u in users:
        label = f"👤 {user_full_name(u)}"
        if u.get("username"):
            label += f" (@{u['username']})"
        rows.append([_btn(label, f"{C.USER_OPEN}{u['id']}")])
    nav = []
    if page > 0:
        nav.append(_btn("← السابق", f"{C.USER_PAGE}{page - 1}"))
    if page < total_pages - 1:
        nav.append(_btn("التالي →", f"{C.USER_PAGE}{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("› رجوع", C.MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_detail_kb(user: dict, accounts: list[dict]) -> InlineKeyboardMarkup:
    """Controls shown under a user's detail card."""
    uid = user["id"]
    rows = []
    for a in accounts:
        label = a.get("tg_username") or f"#{a['tg_user_id']}"
        rows.append([_btn(f"× حذف {label}", f"{C.USER_DEL_ACC_CONFIRM}{a['id']}")])
    action = "🔓 إلغاء الحظر" if user.get("is_blocked") else "🔒 حظر"
    rows.append([
        _btn(action, f"{C.USER_TOGGLE_BLOCK}{uid}"),
        _btn("📨 رسالة", f"{C.USER_NOTIFY}{uid}"),
    ])
    rows.append([_btn("› رجوع للقائمة", C.USERS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_del_confirm_kb(account_id: int, uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("× تأكيد الحذف النهائي", f"{C.USER_DEL_ACC_OK}{account_id}")],
            [_btn("› رجوع للبطاقة", f"{C.USER_OPEN}{uid}")],
        ]
    )


def subscription_tabs_kb() -> InlineKeyboardMarkup:
    """Mandatory-subscription root: pick a tab (channels or groups)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📢 القنوات", C.CH_TAB_CHANNELS), _btn("👥 المجموعات", C.CH_TAB_GROUPS)],
            [_btn("› رجوع", C.MENU)],
        ]
    )


def entries_kb(entries: list[dict], entry_type: str) -> InlineKeyboardMarkup:
    """List entries of one type with toggle + delete per row, plus add."""
    rows = []
    for e in entries:
        icon = "📢" if entry_type == "channel" else "👥"
        status = "✅" if e["is_active"] else "❌"
        rows.append([
            _btn(f"{status} {icon} {_esc(e['title'])}", f"{C.CH_TOGGLE}{entry_type}:{e['id']}"),
            _btn("×", f"{C.CH_DELETE}{entry_type}:{e['id']}"),
        ])
    if entry_type == "channel":
        rows.append([_btn("↢ إضافة قناة", C.CH_ADD_CHANNEL)])
    else:
        rows.append([_btn("↢ إضافة مجموعة", C.CH_ADD_GROUP)])
    rows.append([_btn("› رجوع", C.CH_SUBSCRIPTION)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def entry_delete_confirm_kb(entry_type: str, entry_db_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("× تأكيد الحذف", f"{C.CH_DELETE_OK}{entry_type}:{entry_db_id}")],
            [_btn("› رجوع", f"{C.CH_TAB_CHANNELS}" if entry_type == "channel" else f"{C.CH_TAB_GROUPS}")],
        ]
    )


def stats_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🔄 تحديث", C.STATS)],
            [_btn("› رجوع", C.MENU)],
        ]
    )


def broadcast_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("📤إرسال رسالة", C.BCAST_NEW)],
            [_btn("› رجوع", C.MENU)],
        ]
    )


def broadcast_center_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("✏️ مسودة جديدة", C.BCAST_NEW)],
            [_btn("📜 التاريخ", C.BCAST_HISTORY)],
            [_btn("› رجوع للقائمة", C.MENU)],
        ]
    )


def broadcast_target_kb(filters: AudienceFilter) -> InlineKeyboardMarkup:
    rows = []
    rows.append([
        _btn(f"✓ {filters.target}" if filters.target == "all" else f"  all", f"{C.BCAST_TARGET_X}all"),
        _btn(f"✓ active" if filters.target == "active" else "  active", f"{C.BCAST_TARGET_X}active"),
        _btn(f"✓ inactive" if filters.target == "inactive" else "  inactive", f"{C.BCAST_TARGET_X}inactive"),
        _btn(f"✓ blocked" if filters.target == "blocked" else "  blocked", f"{C.BCAST_TARGET_X}blocked"),
    ])
    rows.append([
        _btn(f"⟁ min {filters.account_count_min or '—'}", f"{C.BCAST_TARGET_X}accounts_min"),
        _btn(f"⟁ max {filters.account_count_max or '—'}", f"{C.BCAST_TARGET_X}accounts_max"),
    ])
    rows.append([
        _btn(f"⟡ reg {filters.registered_days_ago or '—'}", f"{C.BCAST_TARGET_X}registered_days"),
        _btn(f"⟡ seen {filters.last_seen_days_ago or '—'}", f"{C.BCAST_TARGET_X}last_seen"),
    ])
    rows.append([
        _btn(f"{'✓' if filters.exclude_admins else '×'} admins", f"{C.BCAST_TARGET_X}exclude_admins"),
        _btn(f"{'✓' if filters.exclude_previously_contacted else '×'} prev", f"{C.BCAST_TARGET_X}exclude_previous"),
    ])
    rows.append([_btn("🧪 إرسال تجريبي", C.BCAST_TEST_SEND)])
    rows.append([_btn("🔍 معاينة", C.BCAST_DRY_RUN)])
    rows.append([_btn("▶ إرسال الآن", C.BCAST_SEND_NOW)])
    rows.append([_btn("⏰ جدولة", C.BCAST_SCHEDULE)])
    rows.append([_btn("› رجوع", C.BCAST)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_confirm_kb(campaign_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("▶ إرسال الآن", C.BCAST_SEND_NOW)],
            [_btn("⏰ جدولة", C.BCAST_SCHEDULE)],
            [_btn("🧪 تجريبة", C.BCAST_TEST_SEND)],
            [_btn("× إلغاء", f"{C.BCAST_VIEW}{campaign_id}")],
        ]
    )


def broadcast_live_kb(campaign_id: int, status: str) -> InlineKeyboardMarkup:
    rows = [
        [
            _btn("⏸ إيقاف", f"{C.BCAST_PAUSE_LIVE}{campaign_id}"),
            _btn("▶ استأنف", f"{C.BCAST_RESUME_LIVE}{campaign_id}"),
            _btn("× إلغاء", f"{C.BCAST_CANCEL_LIVE}{campaign_id}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def broadcast_history_kb(page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if page > 0:
        nav.append(_btn("← السابق", f"{C.BCAST_PAGE}{page - 1}"))
    if page < total_pages - 1:
        nav.append(_btn("التالي →", f"{C.BCAST_PAGE}{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_btn("› رجوع", C.BCAST)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🌐 اللغة", C.SET_LANG)],
            [_btn("› رجوع", C.MENU)],
        ]
    )


def notifications_list_kb(
    notifications: list[dict],
    page: int,
    admin_id: int,
) -> InlineKeyboardMarkup:
    """Keyboard for the notification inbox: per-row read/dismiss + pagination.

    Each notification row gets two small buttons (✓ read / × dismiss).  If the
    number of returned rows equals the page size, a 'Next' button appears; if
    ``page > 0`` a 'Previous' button appears."""
    rows: list[list[InlineKeyboardButton]] = []
    for n in notifications:
        notif_id = n["id"]
        rows.append([
            _btn("✓", f"{C.NOTIFY_READ}{notif_id}"),
            _btn("×", f"{C.NOTIFY_DISMISS}{notif_id}"),
        ])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(_btn("← السابق", f"{C.NOTIFY_PAGE}{page - 1}"))
    if len(notifications) >= 10:
        nav.append(_btn("التالي →", f"{C.NOTIFY_PAGE}{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([
        _btn("✓ All Read", C.NOTIFY_MARK_ALL),
        _btn("⚙️ الإعدادات", f"{C.NOTIFY}:settings"),
    ])
    rows.append([_btn("› رجوع", C.MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notify_settings_kb(admin_id: int) -> InlineKeyboardMarkup:
    """Keyboard for the notification settings screen: toggle buttons for each
    event type, plus a 'mark all read' helper and back to menu."""
    from app.core.events import SYSTEM_EVENTS
    from app.bot.texts import notification_event_label

    rows: list[list[InlineKeyboardButton]] = []
    for et in sorted(SYSTEM_EVENTS):
        label = notification_event_label(et)
        rows.append([_btn(label, f"{C.NOTIFY_TOGGLE}{et}")])
    rows.append([_btn("✓ All Read", C.NOTIFY_MARK_ALL)])
    rows.append([_btn("› رجوع", C.MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
