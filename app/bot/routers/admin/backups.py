"""Admin: backup management (admin-scoped DB snapshots)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import (
    backup_detail_kb,
    backups_list_kb,
    backups_menu_kb,
    restore_confirm_kb,
)
from app.bot.texts import (
    M_BACKUP_CANCELLED,
    M_BACKUP_CANNOT_CANCEL,
    M_BACKUP_DONE,
    M_BACKUP_EMPTY,
    M_BACKUP_INVALID_ID,
    M_BACKUP_NEW_LABEL,
    M_BACKUP_NO_FILE,
    M_BACKUP_NO_RUN_FILE,
    M_BACKUP_NOT_FOUND,
    M_BACKUP_PROMP,
    M_BACKUP_RESTORE_CONFIRM,
    M_BACKUP_RESTORED,
    M_BACKUP_RUNNING_MSG,
    M_BACKUP_SCHEDULED,
    M_BACKUP_SCHEDULE_PROMPT,
    PARSE_MODE,
    esc,
    render_backup_card,
    render_backup_dashboard,
    render_backup_list,
)
from app.core.backup_manager import BackupManager
from app.bot.routers.admin.broadcast import _parse_schedule_time
from app.core.events import SystemEvent
from app.db import repositories as repo
from app.db.database import Database
from app.services.helpers import safe_edit
from app.bot.states import BackupsFSM

router = Router()

#: How many rows the paginated list shows.
BACKUP_PAGE_SIZE = 10

#: Backup statuses that may be resumed via "run now".
_RESUMABLE = frozenset({"pending", "scheduled", "failed", "cancelled"})


def _int_after(data: str, prefix: str) -> int | None:
    """Parse an int trailing callback data; None if malformed.

    Callback data is user-controlled input, not authorization (RULES §4), so a
    forged non-numeric suffix must not raise a 500."""
    try:
        return int(data.removeprefix(prefix))
    except ValueError:
        return None


async def _dashboard_counts(db: Database) -> dict[str, int]:
    statuses = ("pending", "scheduled", "running", "completed", "failed", "cancelled")
    counts: dict[str, int] = {}
    for s in statuses:
        counts[s] = await repo.count_backups(db, s)
    return counts


# ---------------------------------------------------------------- dashboard


@router.callback_query(F.data == C.BAK)
async def cb_backups(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    counts = await _dashboard_counts(db)
    await safe_edit(
        cb,
        render_backup_dashboard(counts),
        reply_markup=backups_menu_kb(),
    )


# ---------------------------------------------------------------- create flow


@router.callback_query(F.data == C.BAK_NEW)
async def cb_backups_new(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(BackupsFSM.label)
    await state.update_data(label=None, scheduled_for=None)
    await safe_edit(cb, M_BACKUP_NEW_LABEL)


@router.message(BackupsFSM.label)
async def backups_label_entered(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return
    label = (message.text or "").strip()
    if not label:
        await message.answer("× العنوان لا يجب أن يكون فارغاً. أرسل اسماً.", parse_mode=PARSE_MODE)
        return
    await state.update_data(label=label)
    await state.set_state(BackupsFSM.pick)
    await message.answer(M_BACKUP_PROMP.format(label=esc(label)), parse_mode=PARSE_MODE)


# ---------------------------------------------------------------- pick: now / schedule


@router.callback_query(StateFilter(BackupsFSM.pick))
async def backups_pick(
    cb: CallbackQuery, state: FSMContext, backups: BackupManager | None
) -> None:
    await cb.answer()
    data = cb.data or ""
    if data == f"{C.BAK_NEW}:now":
        await _backups_pick_now(cb, state, backups)
    elif data == f"{C.BAK_NEW}:schedule":
        await state.set_state(BackupsFSM.scheduled_for)
        await safe_edit(cb, M_BACKUP_SCHEDULE_PROMPT)
    # unknown sub-action: ignore defensively (no 500)


async def _backups_pick_now(
    cb: CallbackQuery, state: FSMContext, backups: BackupManager | None
) -> None:
    if cb.from_user is None or backups is None:
        await safe_edit(cb, M_BACKUP_NOT_FOUND)
        return
    data = await state.get_data()
    label = data.get("label") or ""
    if not label:
        await safe_edit(cb, M_BACKUP_NOT_FOUND)
        return
    await state.clear()
    bid = await backups.create_backup(
        label, kind="manual", run_now=True, admin_id=cb.from_user.id
    )
    await safe_edit(cb, M_BACKUP_RUNNING_MSG.format(bid=bid))


@router.message(StateFilter(BackupsFSM.scheduled_for))
async def backups_scheduled_for_entered(
    message: Message, state: FSMContext, backups: BackupManager | None
) -> None:
    if message.from_user is None or backups is None:
        await message.answer(M_BACKUP_NOT_FOUND, parse_mode=PARSE_MODE)
        return
    iso = _parse_schedule_time(message.text or "")
    if iso is None:
        await message.answer(
            "× صيغة الوقت غير صالحة. جرب: غداً 20:00 أو +2h أو ISO.",
            parse_mode=PARSE_MODE,
        )
        return
    data = await state.get_data()
    label = data.get("label") or ""
    if not label:
        await message.answer(M_BACKUP_NOT_FOUND, parse_mode=PARSE_MODE)
        await state.clear()
        return
    bid = await backups.create_backup(
        label,
        kind="scheduled",
        scheduled_for=iso,
        admin_id=message.from_user.id,
    )
    await backups._publish_system_event(  # noqa: SLF001 - scheduled event
        bid,
        message.from_user.id,
        "backup_scheduled",
        "info",
        "⟡|تم جدولة نسخة احتياطية",
        f"⟡|النسخة <code>#{bid}</code> جدولة لـ <code>{iso}</code>.",
    )
    await state.clear()
    await message.answer(M_BACKUP_SCHEDULED.format(bid=bid, when=esc(iso)), parse_mode=PARSE_MODE)


# ---------------------------------------------------------------- list / page


@router.callback_query(F.data == f"{C.BAK_PAGE}0")
async def cb_backups_list_first(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    await _render_list_page(cb, db, 0)


@router.callback_query(F.data.startswith(C.BAK_PAGE))
async def cb_backups_page(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    page = _int_after(cb.data, C.BAK_PAGE)
    if page is None:
        page = 0
    await _render_list_page(cb, db, page)


async def _render_list_page(cb: CallbackQuery, db: Database, page: int) -> None:
    offset = page * BACKUP_PAGE_SIZE
    rows = await repo.list_backups(db, status="completed", limit=BACKUP_PAGE_SIZE)
    total = await repo.count_backups(db, status="completed")
    total_pages = max(1, (total + BACKUP_PAGE_SIZE - 1) // BACKUP_PAGE_SIZE)
    if page < 0:
        page = 0
    if page >= total_pages:
        page = max(0, total_pages - 1)
    if not rows:
        await safe_edit(cb, M_BACKUP_EMPTY, reply_markup=backups_menu_kb())
        return
    await safe_edit(
        cb,
        render_backup_list(rows, page, total_pages),
        reply_markup=backups_list_kb(rows, page, total_pages),
    )


# ---------------------------------------------------------------- open / detail


@router.callback_query(F.data.startswith(C.BAK_OPEN))
async def cb_backup_open(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    bid = _int_after(cb.data, C.BAK_OPEN)
    if bid is None:
        await safe_edit(cb, M_BACKUP_INVALID_ID, reply_markup=backups_menu_kb())
        return
    backup = await repo.get_backup(db, bid)
    if backup is None:
        await safe_edit(cb, M_BACKUP_NOT_FOUND, reply_markup=backups_menu_kb())
        return
    await safe_edit(
        cb,
        render_backup_card(backup),
        reply_markup=backup_detail_kb(backup),
    )


# ---------------------------------------------------------------- run now (resume)


@router.callback_query(F.data.startswith(C.BAK_RUN_NOW))
async def cb_backup_run_now(
    cb: CallbackQuery, db: Database, backups: BackupManager | None
) -> None:
    await cb.answer()
    bid = _int_after(cb.data, C.BAK_RUN_NOW)
    if bid is None:
        await safe_edit(cb, M_BACKUP_INVALID_ID, reply_markup=backups_menu_kb())
        return
    backup = await repo.get_backup(db, bid)
    if backup is None:
        await safe_edit(cb, M_BACKUP_NOT_FOUND, reply_markup=backups_menu_kb())
        return
    if backup["status"] not in _RESUMABLE:
        await safe_edit(cb, M_BACKUP_NO_RUN_FILE, reply_markup=backup_detail_kb(backup))
        return
    if backups is not None:
        await backups.resume(bid)
    await safe_edit(cb, M_BACKUP_RUNNING_MSG.format(bid=bid))


# ---------------------------------------------------------------- cancel


@router.callback_query(F.data.startswith(C.BAK_CANCEL))
async def cb_backup_cancel(
    cb: CallbackQuery, db: Database, backups: BackupManager | None
) -> None:
    await cb.answer()
    bid = _int_after(cb.data, C.BAK_CANCEL)
    if bid is None:
        await safe_edit(cb, M_BACKUP_INVALID_ID, reply_markup=backups_menu_kb())
        return
    backup = await repo.get_backup(db, bid)
    if backup is None:
        await safe_edit(cb, M_BACKUP_NOT_FOUND, reply_markup=backups_menu_kb())
        return
    if backups is not None:
        ok = await backups.cancel(bid)
        if ok:
            await safe_edit(cb, M_BACKUP_CANCELLED.format(bid=bid))
            return
    await safe_edit(cb, M_BACKUP_CANNOT_CANCEL, reply_markup=backup_detail_kb(backup))


# ---------------------------------------------------------------- restore


@router.callback_query(F.data.startswith(C.BAK_RESTORE))
async def cb_backup_restore(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    bid = _int_after(cb.data, C.BAK_RESTORE)
    if bid is None:
        await safe_edit(cb, M_BACKUP_INVALID_ID, reply_markup=backups_menu_kb())
        return
    backup = await repo.get_backup(db, bid)
    if backup is None:
        await safe_edit(cb, M_BACKUP_NOT_FOUND, reply_markup=backups_menu_kb())
        return
    if backup["status"] != "completed" or not backup.get("file_path"):
        await safe_edit(cb, M_BACKUP_NO_RUN_FILE, reply_markup=backup_detail_kb(backup))
        return
    await safe_edit(
        cb,
        M_BACKUP_RESTORE_CONFIRM.format(bid=bid),
        reply_markup=restore_confirm_kb(bid),
    )


@router.callback_query(F.data.startswith(C.BAK_RESTORE_OK))
async def cb_backup_restore_ok(
    cb: CallbackQuery, db: Database, backups: BackupManager | None
) -> None:
    await cb.answer()
    bid = _int_after(cb.data, C.BAK_RESTORE_OK)
    if bid is None:
        await safe_edit(cb, M_BACKUP_INVALID_ID, reply_markup=backups_menu_kb())
        return
    admin_id = cb.from_user.id if cb.from_user else 0
    if backups is None:
        await safe_edit(cb, M_BACKUP_NO_FILE, reply_markup=backups_menu_kb())
        return
    status = await backups.restore(bid, admin_id)
    if status == "restored":
        await safe_edit(cb, M_BACKUP_RESTORED.format(bid=bid))
    elif status == "no_file":
        await safe_edit(cb, M_BACKUP_NO_FILE, reply_markup=backups_menu_kb())
    elif status == "not_completed":
        await safe_edit(cb, M_BACKUP_NO_RUN_FILE, reply_markup=backups_menu_kb())
    else:
        await safe_edit(cb, M_BACKUP_NOT_FOUND, reply_markup=backups_menu_kb())
