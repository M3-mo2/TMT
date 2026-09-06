"""Admin: settings."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import settings_kb
from app.services.helpers import safe_edit

router = Router()


@router.callback_query(F.data == C.SETTINGS)
async def cb_settings(cb: CallbackQuery) -> None:
    await cb.answer()
    await safe_edit(cb, "⚙️ الإعدادات", reply_markup=settings_kb())
