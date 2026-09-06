"""Admin panel menu — /admin entry, admin main menu, user-menu bridge."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import admin_menu_kb
from app.bot.texts import PARSE_MODE, render_admin_menu
from app.config import Config
from app.db import repositories as repo
from app.db.database import Database
from app.services.helpers import safe_edit, safe_delete

router = Router()


async def _admin_welcome(db: Database) -> str:
    """Render the admin panel welcome banner with overview stats."""
    user_count = await repo.count_users(db)
    jobs_count = await repo.count_jobs(db)
    completed_count = await repo.count_completed_jobs(db)
    return render_admin_menu(user_count, jobs_count, completed_count)


@router.message(Command("admin"))
async def cmd_admin(message: Message, db: Database, config: Config) -> None:
    if message.from_user is None:
        return
    if message.from_user.id not in config.admin_id_list:
        await message.answer("ليس لديك صلاحية.", parse_mode=PARSE_MODE)
        return
    await message.answer(
        await _admin_welcome(db),
        parse_mode=PARSE_MODE,
        reply_markup=admin_menu_kb(),
    )


@router.callback_query(F.data == C.MENU)
async def cb_admin_menu(cb: CallbackQuery, db: Database) -> None:
    await safe_edit(
        cb,
        await _admin_welcome(db),
        reply_markup=admin_menu_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == C.CLOSE)
async def cb_close(cb: CallbackQuery) -> None:
    await safe_delete(cb)
    await cb.answer()
