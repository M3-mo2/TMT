"""Admin: mandatory subscription channels management."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import channels_kb, channel_delete_confirm_kb
from app.bot.texts import PARSE_MODE
from app.db import repositories as repo
from app.db.database import Database
from app.services.helpers import safe_edit

router = Router()


class ChannelAddFSM(StatesGroup):
    channel_id = State()
    title = State()
    invite_link = State()


@router.callback_query(F.data == C.CHANNELS)
async def cb_channels_list(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    channels = await repo.list_channels(db)
    if not channels:
        text = "⬜ لم يتم إضافة قنوات بعد."
    else:
        lines = ["📢 قنوات الاشتراك الإجباري:"]
        for ch in channels:
            status = "✅" if ch["is_active"] else "❌"
            lines.append(f"{status} {ch['title']} — {ch['invite_link']}")
        text = "\n".join(lines)
    await safe_edit(cb, text, reply_markup=channels_kb(channels))


@router.callback_query(F.data == C.CHANNEL_ADD)
async def cb_channel_add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(ChannelAddFSM.channel_id)
    await safe_edit(
        cb,
        "📋 أرسل معرف القناة (مثال: -1001234567890):",
    )


@router.message(ChannelAddFSM.channel_id)
async def channel_id_entered(message: Message, state: FSMContext) -> None:
    await state.update_data(channel_id=int(message.text or 0))
    await state.set_state(ChannelAddFSM.title)
    await message.answer("📋 أرسل عنوان القناة:", parse_mode=PARSE_MODE)


@router.message(ChannelAddFSM.title)
async def channel_title_entered(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text or "")
    await state.set_state(ChannelAddFSM.invite_link)
    await message.answer("📋 أرسل رابط الدعوة:", parse_mode=PARSE_MODE)


@router.message(ChannelAddFSM.invite_link)
async def channel_link_entered(
    message: Message, state: FSMContext, db: Database
) -> None:
    data = await state.get_data()
    await repo.add_channel(
        db,
        channel_id=data["channel_id"],
        title=data["title"],
        invite_link=message.text or "",
    )
    await state.clear()
    channels = await repo.list_channels(db)
    text = "✅ تم إضافة القناة."
    await message.answer(text, parse_mode=PARSE_MODE)
    await message.answer("📢 قنوات الاشتراك الإجباري:", parse_mode=PARSE_MODE, reply_markup=channels_kb(channels))


@router.callback_query(F.data.startswith(C.CHANNEL_DEL))
async def cb_channel_delete(cb: CallbackQuery) -> None:
    await cb.answer()
    channel_id = int(cb.data.replace(C.CHANNEL_DEL, ""))
    await safe_edit(
        cb,
        "⚠️ هل أنت متأكد؟",
        reply_markup=channel_delete_confirm_kb(channel_id),
    )


@router.callback_query(F.data.startswith(C.CHANNEL_DEL_OK))
async def cb_channel_delete_confirm(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    channel_id = int(cb.data.replace(C.CHANNEL_DEL_OK, ""))
    await repo.delete_channel(db, channel_id)
    channels = await repo.list_channels(db)
    await safe_edit(
        cb,
        "✅ تم حذف القناة.",
        reply_markup=channels_kb(channels),
    )


@router.callback_query(F.data.startswith(C.CHANNEL_TOGGLE))
async def cb_channel_toggle(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    channel_id = int(cb.data.replace(C.CHANNEL_TOGGLE, ""))
    await repo.toggle_channel(db, channel_id)
    channels = await repo.list_channels(db)
    text = "🔄 تم تحديث حالة القناة."
    await safe_edit(cb, text, reply_markup=channels_kb(channels))
