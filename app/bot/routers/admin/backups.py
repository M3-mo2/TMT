"""Admin: backup management — periodic backups, history, export, restore.

Manages :class:`~app.core.backup.BackupService` from the admin panel.  The
feature toggle, interval and send-target live in ``app_settings`` (read/merged
with Config defaults in the service).  Periodic scheduling is driven by the
service's own sweeper; this router only renders state and dispatches on-demand
actions: create / export / restore / delete / upload-restore.

Layer rule (RULES §1): the admin ``bot/`` layer never imports Telethon — all
DB work goes through ``BackupService`` (which lives in ``core/`` and owns the
file-system backup/restore logic).  Bot objects are passed per-call.
"""

from __future__ import annotations

import logging
import math

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import (
    backup_detail_kb,
    backup_history_kb,
    backup_interval_back_kb,
    backup_menu_kb,
    backup_restore_confirm_kb,
    backup_settings_kb,
)
from app.bot.texts import (
    M_BACKUP_CREATED,
    M_BACKUP_CREATE_FAILED,
    M_BACKUP_DELETED,
    M_BACKUP_INTERVAL_INVALID,
    M_BACKUP_INTERVAL_PROMPT,
    M_BACKUP_INTERVAL_SAVED,
    M_BACKUP_RESTORE_BLOCKED,
    M_BACKUP_RESTORE_DONE,
    M_BACKUP_RESTORE_FAILED,
    M_BACKUPS_SUMMARY,
    M_BACKUPS_TITLE,
    PARSE_MODE,
    esc,
    render_backups_settings,
    render_backups_list,
    render_backup_card,
)
from app.core.backup import BackupService
from app.core.models import BackupSettings
from app.db import repositories as repo
from app.db.database import Database
from app.services.helpers import safe_edit

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = Router()

BACKUPS_PER_PAGE = 12


class BackupFSM(StatesGroup):
    """Transient admin input states for the backup panel.

    Dedicated group (not the shared ``SettingsFSM``) so it cannot collide with
    the per-user transfer-settings flow, mirroring ``MandatoryEntryFSM`` in
    ``channels.py``.
    """

    interval = State()
    archive = State()


def _int_after(data: str, prefix: str) -> int | None:
    try:
        return int(data.removeprefix(prefix))
    except ValueError:
        return None


def _back_kb(data: str = C.BAK) -> InlineKeyboardMarkup:
    from app.bot.routers.admin.keyboards import _btn

    return InlineKeyboardMarkup(inline_keyboard=[[_btn("› رجوع", data)]])


def _interval_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("› رجوع", C.BAK_SETTINGS)]])


async def _settings_view(backup: BackupService) -> tuple[BackupSettings, str]:
    settings = await backup.get_settings()
    last = await backup.last_backup_created_at()
    count = await backup.count_backups()
    text = render_backups_settings(
        enabled=settings.enabled,
        interval_hours=settings.interval_hours,
        chat_id=settings.chat_id,
        last_backup=last,
        backup_count=count,
    )
    return settings, text


# ------------------------------------------------------------------ root + settings


@router.callback_query(F.data == C.BAK)
async def cb_backups_root(cb: CallbackQuery, backup: BackupService) -> None:
    """Backups root: settings shortcut, history, on-demand, upload."""
    await cb.answer()
    _, text = await _settings_view(backup)
    await safe_edit(
        cb,
        "\n".join([M_BACKUPS_TITLE, "", text, "", M_BACKUPS_SUMMARY]),
        reply_markup=backup_menu_kb(),
    )


@router.callback_query(F.data == C.BAK_SETTINGS)
async def cb_backup_settings(cb: CallbackQuery, backup: BackupService) -> None:
    """Render the backup settings screen."""
    await cb.answer()
    settings, text = await _settings_view(backup)
    await safe_edit(cb, text, reply_markup=backup_settings_kb(settings.enabled))


@router.callback_query(F.data == C.BAK_TOGGLE)
async def cb_backup_toggle(cb: CallbackQuery, backup: BackupService) -> None:
    await cb.answer()
    settings = await backup.get_settings()
    await backup.set_setting(repo.BACKUP_KEY_ENABLED, "false" if settings.enabled else "true")
    await safe_edit(
        cb,
        await _render_settings(cb, backup),
        reply_markup=backup_settings_kb(not settings.enabled),
    )


@router.callback_query(F.data == C.BAK_INTERVAL)
async def cb_backup_interval(cb: CallbackQuery, state: FSMContext, backup: BackupService) -> None:
    await cb.answer()
    settings = await backup.get_settings()
    await state.set_state(BackupFSM.interval)
    await safe_edit(
        cb,
        M_BACKUP_INTERVAL_PROMPT.format(current=settings.interval_hours),
        reply_markup=_interval_back_kb(),
    )


@router.message(StateFilter(BackupFSM.interval))
async def backup_interval_entered(message: Message, state: FSMContext, backup: BackupService) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(M_BACKUP_INTERVAL_INVALID, parse_mode=PARSE_MODE)
        return
    value = int(raw)
    if not (1 <= value <= 168):
        await message.answer(M_BACKUP_INTERVAL_INVALID, parse_mode=PARSE_MODE)
        return
    await backup.set_setting(repo.BACKUP_KEY_INTERVAL, str(value))
    await message.answer(M_BACKUP_INTERVAL_SAVED, parse_mode=PARSE_MODE)
    await state.clear()


# ------------------------------------------------------------------ on-demand + history


@router.callback_query(F.data == C.BAK_NEW)
async def cb_backup_new(cb: CallbackQuery, backup: BackupService) -> None:
    await cb.answer("⟡|جارٍ إنشاء النسخة...")
    result = await backup.run_now()
    if result.ok:
        await safe_edit(cb, M_BACKUP_CREATED, reply_markup=backup_menu_kb())
    else:
        await safe_edit(
            cb,
            M_BACKUP_CREATE_FAILED.format(error=esc(result.error or "خطأ غير معروف")),
            reply_markup=backup_menu_kb(),
        )


@router.callback_query(F.data == C.BAK_HISTORY)
async def cb_backup_history(cb: CallbackQuery, backup: BackupService) -> None:
    await _render_history(cb, backup, page=0)


@router.callback_query(F.data.startswith(C.BAK_PAGE))
async def cb_backup_page(cb: CallbackQuery, backup: BackupService) -> None:
    page = _int_after(cb.data, C.BAK_PAGE)
    await _render_history(cb, backup, page=page or 0)


async def _render_history(cb: CallbackQuery, backup: BackupService, page: int) -> None:
    await cb.answer()
    total = await backup.count_backups()
    items = await backup.list_backups(limit=BACKUPS_PER_PAGE, offset=page * BACKUPS_PER_PAGE)
    total_pages = max(1, math.ceil(total / BACKUPS_PER_PAGE)) if total else 1
    await safe_edit(
        cb,
        render_backups_list(items, page, total_pages),
        reply_markup=backup_history_kb(page, total_pages),
    )


@router.callback_query(F.data.startswith(C.BAK_OPEN))
async def cb_backup_open(cb: CallbackQuery, backup: BackupService) -> None:
    bid = _int_after(cb.data, C.BAK_OPEN)
    await cb.answer()
    if bid is None:
        await safe_edit(cb, "× غير موجود.", reply_markup=_back_kb())
        return
    rec = await repo.get_backup(backup._db, bid)
    if rec is None:
        await safe_edit(cb, "× غير موجود.", reply_markup=_back_kb())
        return
    row = {
        "id": rec.id,
        "filename": rec.filename,
        "file_size": rec.file_size,
        "status": rec.status.value,
        "created_at": rec.created_at,
        "sent_to": rec.sent_to,
        "error": rec.error,
    }
    await safe_edit(cb, render_backup_card(row), reply_markup=backup_detail_kb(bid))


# ------------------------------------------------------------------ export


@router.callback_query(F.data.startswith(C.BAK_EXPORT))
async def cb_backup_export(cb: CallbackQuery, backup: BackupService) -> None:
    bid = _int_after(cb.data, C.BAK_EXPORT)
    await cb.answer("⟡|جارٍ الإرسال...")
    admin_id = cb.from_user.id if cb.from_user else 0
    if bid is None or not await backup.export_to(bid, admin_id):
        await safe_edit(cb, M_BACKUP_RESTORE_FAILED.format(error="غير موجود"),
                        reply_markup=_back_kb(f"{C.BAK_OPEN}{bid}" if bid is not None else C.BAK))
        return
    await safe_edit(
        cb,
        M_BACKUP_SENT.format(chat_id=admin_id),
        reply_markup=_back_kb(f"{C.BAK_OPEN}{bid}" if bid is not None else C.BAK),
    )


# ------------------------------------------------------------------ delete


@router.callback_query(F.data.startswith(C.BAK_DELETE))
async def cb_backup_delete_confirm(cb: CallbackQuery, backup: BackupService) -> None:
    bid = _int_after(cb.data, C.BAK_DELETE)
    await cb.answer()
    if bid is None:
        await safe_edit(cb, "× غير موجود.", reply_markup=_back_kb(C.BAK))
        return
    rec = await repo.get_backup(backup._db, bid)
    if rec is None:
        await safe_edit(cb, "× غير موجود.", reply_markup=_back_kb(C.BAK))
        return
    await safe_edit(
        cb,
        f"× حذف النسخة <b>#{bid}</b> بصيغة "
        f"<code>{esc(rec.filename)}</code>؟",
        reply_markup=backup_restore_confirm_kb(bid),
    )


@router.callback_query(F.data.startswith(C.BAK_DELETE_OK))
async def cb_backup_delete_ok(cb: CallbackQuery, backup: BackupService) -> None:
    bid = _int_after(cb.data, C.BAK_DELETE_OK)
    await cb.answer()
    if bid is None:
        await safe_edit(cb, "× غير موجود.", reply_markup=_back_kb(C.BAK))
        return
    deleted = await backup.delete_backup(bid)
    if not deleted:
        await safe_edit(cb, "× غير موجود.", reply_markup=_back_kb(C.BAK))
        return
    await safe_edit(cb, M_BACKUP_DELETED, reply_markup=_back_kb(C.BAK))


# ------------------------------------------------------------------ restore from a listed backup


@router.callback_query(F.data.startswith(C.BAK_RESTORE))
async def cb_backup_restore_prompt(cb: CallbackQuery, backup: BackupService) -> None:
    bid = _int_after(cb.data, C.BAK_RESTORE)
    await cb.answer()
    if bid is None:
        await safe_edit(cb, "× غير موجود.", reply_markup=_back_kb(C.BAK))
        return
    rec = await repo.get_backup(backup._db, bid)
    if rec is None:
        await safe_edit(cb, "× غير موجود.", reply_markup=_back_kb(C.BAK))
        return
    await safe_edit(
        cb,
        f"× تأكيد استعادة النسخة <b>#{bid}</code> — "
        "سيتم استبدال قاعدة البيانات الحالية بالكامل.\n\n"
        "⚠️|هذا الإجراء لا يمكن التراجع عنه.",
        reply_markup=backup_restore_confirm_kb(bid),
    )


@router.callback_query(F.data.startswith(C.BAK_RESTORE_CONFIRM))
async def cb_backup_restore_do(cb: CallbackQuery, backup: BackupService) -> None:
    bid = _int_after(cb.data, C.BAK_RESTORE_CONFIRM)
    await cb.answer("⟡|جارٍ الاستعادة...")
    if bid is None:
        await safe_edit(cb, "× غير موجود.", reply_markup=_back_kb(C.BAK))
        return
    archive_path = await backup.archive_path_for(bid)
    if archive_path is None:
        await safe_edit(
            cb,
            M_BACKUP_RESTORE_FAILED.format(error="الملف غير موجود"),
            reply_markup=_back_kb(f"{C.BAK_OPEN}{bid}"),
        )
        return
    result = await backup.restore_from_file(archive_path)
    if result.ok:
        await safe_edit(
            cb,
            M_BACKUP_RESTORE_DONE,
            reply_markup=_back_kb(C.BAK),
        )
    elif result.error == "active_jobs":
        await safe_edit(
            cb,
            M_BACKUP_RESTORE_BLOCKED,
            reply_markup=_back_kb(f"{C.BAK_OPEN}{bid}"),
        )
    else:
        await safe_edit(
            cb,
            M_BACKUP_RESTORE_FAILED.format(error=esc(result.error or "خطأ غير معروف"))
            + (f"\n{result.detail}" if result.detail else ""),
            reply_markup=_back_kb(f"{C.BAK_OPEN}{bid}"),
        )


# ------------------------------------------------------------------ restore from upload


@router.callback_query(F.data == C.BAK_UPLOAD_START)
async def cb_backup_upload_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(BackupFSM.archive)
    await safe_edit(cb, M_BACKUPS_TITLE + "\n\n↢ أرسل ملف الأرشيف المضغوط (.zip) لاستعادته.",
                    reply_markup=_interval_back_kb())


@router.message(StateFilter(BackupFSM.archive))
async def backup_receive_upload(message: Message, state: FSMContext, backup: BackupService) -> None:
    doc = message.document
    if doc is None or not doc.file_name or not doc.file_name.lower().endswith(".zip"):
        await message.answer("× أرسل أرشيف .zip صالح.", parse_mode=PARSE_MODE)
        return
    dest = backup.dir / f"upload-{doc.file_unique_id}.zip"
    await message.bot.download(doc, destination=dest)
    await state.clear()
    result = await backup.restore_from_file(dest)
    try:
        dest.unlink(missing_ok=True)
    except OSError:
        pass
    if result.ok:
        await message.answer(M_BACKUP_RESTORE_DONE, parse_mode=PARSE_MODE)
    elif result.error == "active_jobs":
        await message.answer(M_BACKUP_RESTORE_BLOCKED, parse_mode=PARSE_MODE)
    else:
        text = M_BACKUP_RESTORE_FAILED.format(error=esc(result.error or "خطأ غير معروف"))
        if result.detail:
            text += f"\n{result.detail}"
        await message.answer(text, parse_mode=PARSE_MODE)
