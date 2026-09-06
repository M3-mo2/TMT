"""Admin: backup management."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.routers.admin import callbacks as C
from app.services.helpers import safe_edit

router = Router()


@router.callback_query(F.data == C.BAK or F.data.startswith(C.BAK))
async def cb_backups(cb: CallbackQuery) -> None:
    await cb.answer()
    await safe_edit(cb, "💾|إدارة النسخ الاحتياطية")
