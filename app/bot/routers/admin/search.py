"""Admin: search / deep search across entities."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.routers.admin import callbacks as C
from app.services.helpers import safe_edit

router = Router()


@router.callback_query(F.data.startswith(C.SEARCH))
async def cb_search(cb: CallbackQuery) -> None:
    await cb.answer()
    await safe_edit(cb, "🔎 أرسل معرف المستخدم أو معرف العملية للبحث.")
