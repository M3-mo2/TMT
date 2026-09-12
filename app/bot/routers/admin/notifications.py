"""Admin: notification inbox — list, read, dismiss, toggle settings."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import _btn, notifications_list_kb, notify_settings_kb
from app.bot.texts import (
    render_notifications_list,
    render_notify_settings,
)
from app.config import Config
from app.core.events import SYSTEM_EVENTS
from app.db import repositories as repo
from app.db.database import Database
from app.services.helpers import safe_edit

router = Router()

NOTIFS_PER_PAGE = 10


def _int_after(data: str, prefix: str) -> int | None:
    """Parse an int trailing callback data; None if malformed."""
    try:
        return int(data.removeprefix(prefix))
    except ValueError:
        return None


def _back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn("› رجوع", C.MENU)]]
    )


async def _render_page(cb: CallbackQuery, db: Database, page: int = 0) -> None:
    admin_id = cb.from_user.id if cb.from_user else 0
    unread = await repo.count_unread_notifications(db, admin_id)
    notifs = await repo.list_notifications(
        db, admin_id, limit=NOTIFS_PER_PAGE,
        offset=page * NOTIFS_PER_PAGE, include_dismissed=True,
    )
    await safe_edit(
        cb,
        render_notifications_list(notifs, unread),
        reply_markup=notifications_list_kb(notifs, page=page, admin_id=admin_id),
    )


@router.callback_query(F.data == C.NOTIFY)
async def cb_notify_list(cb: CallbackQuery, db: Database) -> None:
    """Show the notifications inbox (first page)."""
    await cb.answer()
    await _render_page(cb, db, page=0)


@router.callback_query(F.data.startswith(C.NOTIFY_PAGE))
async def cb_notify_page(cb: CallbackQuery, db: Database) -> None:
    """Paginated notification list."""
    await cb.answer()
    page = _int_after(cb.data, C.NOTIFY_PAGE)
    if page is None:
        page = 0
    await _render_page(cb, db, page=page)


@router.callback_query(F.data.startswith(C.NOTIFY_READ))
async def cb_notify_read(cb: CallbackQuery, db: Database) -> None:
    """Mark a single notification as read."""
    await cb.answer()
    notif_id = _int_after(cb.data, C.NOTIFY_READ)
    if notif_id is None:
        await safe_edit(cb, "⚠️ غير موجود.", reply_markup=_back_to_menu_kb())
        return
    await repo.mark_notification_read(db, notif_id)
    await _render_page(cb, db, page=0)


@router.callback_query(F.data == C.NOTIFY_MARK_ALL)
async def cb_notify_mark_all(cb: CallbackQuery, db: Database) -> None:
    """Mark all notifications as read."""
    await cb.answer()
    admin_id = cb.from_user.id if cb.from_user else 0
    await repo.mark_all_notifications_read(db, admin_id)
    await _render_page(cb, db, page=0)


@router.callback_query(F.data.startswith(C.NOTIFY_DISMISS))
async def cb_notify_dismiss(cb: CallbackQuery, db: Database) -> None:
    """Dismiss (hide) a notification."""
    await cb.answer()
    notif_id = _int_after(cb.data, C.NOTIFY_DISMISS)
    if notif_id is None:
        await safe_edit(cb, "⚠️ غير موجود.", reply_markup=_back_to_menu_kb())
        return
    await repo.dismiss_notification(db, notif_id)
    await _render_page(cb, db, page=0)


@router.callback_query(F.data == f"{C.NOTIFY}:settings")
async def cb_notify_settings(cb: CallbackQuery, db: Database, config: Config) -> None:
    """Show the notification settings screen with per-event toggles."""
    await cb.answer()
    admin_id = cb.from_user.id if cb.from_user else 0
    disabled: set[str] = set()
    for et in SYSTEM_EVENTS:
        if not await repo.is_notification_enabled(db, admin_id, et):
            disabled.add(et)
    enabled_types = sorted(SYSTEM_EVENTS - disabled)
    await safe_edit(
        cb,
        render_notify_settings(config.admin_id_list, enabled_types),
        reply_markup=notify_settings_kb(admin_id),
    )


@router.callback_query(F.data.startswith(C.NOTIFY_TOGGLE))
async def cb_notify_toggle(cb: CallbackQuery, db: Database, config: Config) -> None:
    """Toggle a notification event type for this admin."""
    await cb.answer()
    admin_id = cb.from_user.id if cb.from_user else 0
    event_type = cb.data.removeprefix(C.NOTIFY_TOGGLE)
    if event_type not in SYSTEM_EVENTS:
        await safe_edit(cb, "⚠️ نوع إشعار غير معروف.", reply_markup=notify_settings_kb(admin_id))
        return
    currently_enabled = await repo.is_notification_enabled(db, admin_id, event_type)
    await repo.set_notification_setting(db, admin_id, event_type, not currently_enabled)
    disabled: set[str] = set()
    for et in SYSTEM_EVENTS:
        if not await repo.is_notification_enabled(db, admin_id, et):
            disabled.add(et)
    enabled_types = sorted(SYSTEM_EVENTS - disabled)
    await safe_edit(
        cb,
        render_notify_settings(config.admin_id_list, enabled_types),
        reply_markup=notify_settings_kb(admin_id),
    )