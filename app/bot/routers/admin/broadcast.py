"""Admin: broadcast messaging."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import broadcast_kb
from app.bot.texts import PARSE_MODE
from app.services.helpers import safe_edit

router = Router()


class BroadcastFSM(StatesGroup):
    message = State()


@router.callback_query(F.data == C.BCAST)
async def cb_broadcast(cb: CallbackQuery) -> None:
    await cb.answer()
    await safe_edit(cb, "📢 قائمة البث", reply_markup=broadcast_kb())


@router.callback_query(F.data == C.BCAST_COMPOSE)
async def cb_broadcast_compose(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(BroadcastFSM.message)
    await safe_edit(cb, "📋 أرسل الرسالة التي تريد بثها:\nاستخدم <b>HTML</b> للتنسيق.")


@router.message(BroadcastFSM.message)
async def broadcast_message_entered(message: Message, state: FSMContext) -> None:
    await state.update_data(message=message.text or "")
    await state.set_state(None)
    await message.answer(
        "✅ تم الحفظ. سيتم البث في الإصدار القادم.",
        parse_mode=PARSE_MODE,
    )


@router.callback_query(F.data.startswith(C.BCAST_PAGE))
async def cb_broadcast_page(cb: CallbackQuery) -> None:
    await cb.answer()
    await safe_edit(cb, "📢 قائمة البث", reply_markup=broadcast_kb())
