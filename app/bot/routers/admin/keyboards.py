"""Admin keyboards — builder functions returning InlineKeyboardMarkup."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.bot.routers.admin import callbacks as C
from app.bot.texts import user_full_name


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def admin_menu_kb() -> InlineKeyboardMarkup:
    """Top-level admin menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _btn("👥 المستخدمون", C.USERS),
                _btn("📢 القنوات", C.CHANNELS),
            ],
            [
                _btn("📊 الإحصاءات", C.STATS),
                _btn("📢 البث", C.BCAST),
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


def channels_kb(channels: list[dict]) -> InlineKeyboardMarkup:
    """List of mandatory channels with toggle/delete actions."""
    rows = []
    for ch in channels:
        status = "✅" if ch["is_active"] else "❌"
        rows.append([
            _btn(f"{status} {ch['title']}", f"{C.CHANNEL_TOGGLE}{ch['id']}"),
        ])
    rows.append([_btn("➕ إضافة قناة", C.CHANNEL_ADD)])
    rows.append([_btn("› رجوع", C.MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_delete_confirm_kb(channel_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("× تأكيد الحذف", f"{C.CHANNEL_DEL_OK}{channel_id}")],
            [_btn("› رجوع", C.CHANNELS)],
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
            [_btn("📤إرسال رسالة", C.BCAST_COMPOSE)],
            [_btn("› رجوع", C.MENU)],
        ]
    )


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🌐 اللغة", C.SET_LANG)],
            [_btn("› رجوع", C.MENU)],
        ]
    )
