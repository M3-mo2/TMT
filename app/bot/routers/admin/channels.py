"""Admin: mandatory subscription (channels + groups) management.

Replaces the old flat channel list with a two-tab interface.  Every write
(add/toggle/delete) calls ``reset_user_gates`` so users re-verify against the
updated mandatory set.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import entries_kb, entry_delete_confirm_kb, subscription_tabs_kb
from app.bot.texts import (
    M_ADD_ENTRY_PROMPT,
    M_ENTRY_ADDED,
    M_ENTRY_DELETE_PROMPT,
    M_ENTRY_LINK_PROMPT,
    M_ENTRY_NOT_FOUND,
    M_ENTRY_TOGGLED,
    M_MANDATORY_SUBSCRIPTION_TITLE,
    PARSE_MODE,
    render_entries_list,
)
from app.db import repositories as repo
from app.db.database import Database
from app.services.helpers import safe_edit

router = Router()


class MandatoryEntryFSM(StatesGroup):
    ref = State()
    invite_link = State()


def _int_pair(suffix: str) -> tuple[str, int] | None:
    """Parse ``type:id`` from a callback-data suffix; None if malformed."""
    parts = suffix.split(":")
    if len(parts) != 2:
        return None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return None


def _normalize_ref(ref: str) -> int | str:
    """Normalize user input so ``bot.get_chat`` can resolve it."""
    ref = ref.strip()
    if ref.lstrip("-").isdigit():
        return int(ref)
    if "t.me/" in ref:
        path = ref.split("t.me/", 1)[1].split("/")[0].split("?")[0]
        if path.startswith("+"):
            return ref  # invite link hash — API accepts directly
        return "@" + path.lstrip("@")
    if ref.startswith("@"):
        return ref
    return "@" + ref


def _extract_invite_link(chat: object) -> str | None:
    """Best-effort invite-link extraction from an aiogram Chat object."""
    il = getattr(chat, "invite_link", None)
    if il:
        if isinstance(il, str):
            return il
        return getattr(il, "invite_link", None)
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}"
    return None


async def _render_tab(cb: CallbackQuery, db: Database, entry_type: str) -> None:
    """Re-render the entry list for a given tab type."""
    entries = await repo.list_channels_by_type(db, entry_type)
    text = render_entries_list(entries, entry_type)
    await safe_edit(cb, text, parse_mode=PARSE_MODE, reply_markup=entries_kb(entries, entry_type))


@router.callback_query(F.data == C.CH_SUBSCRIPTION)
async def cb_subscription_main(cb: CallbackQuery) -> None:
    await cb.answer()
    await safe_edit(
        cb, M_MANDATORY_SUBSCRIPTION_TITLE, parse_mode=PARSE_MODE,
        reply_markup=subscription_tabs_kb(),
    )


@router.callback_query(F.data == C.CH_TAB_CHANNELS)
async def cb_channels_tab(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    await _render_tab(cb, db, "channel")


@router.callback_query(F.data == C.CH_TAB_GROUPS)
async def cb_groups_tab(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    await _render_tab(cb, db, "group")


@router.callback_query(F.data == C.CH_ADD_CHANNEL)
async def cb_add_channel_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.update_data(entry_type="channel")
    await state.set_state(MandatoryEntryFSM.ref)
    await safe_edit(cb, M_ADD_ENTRY_PROMPT, parse_mode=PARSE_MODE)


@router.callback_query(F.data == C.CH_ADD_GROUP)
async def cb_add_group_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.update_data(entry_type="group")
    await state.set_state(MandatoryEntryFSM.ref)
    await safe_edit(cb, M_ADD_ENTRY_PROMPT, parse_mode=PARSE_MODE)


@router.message(StateFilter(MandatoryEntryFSM.ref))
async def entry_ref_entered(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    entry_type = data.get("entry_type", "channel")
    ref = message.text or ""
    try:
        chat = await message.bot.get_chat(_normalize_ref(ref))
    except Exception:
        await message.answer(M_ENTRY_NOT_FOUND, parse_mode=PARSE_MODE)
        return

    title = getattr(chat, "title", None) or ref
    invite_link = _extract_invite_link(chat)

    if invite_link:
        await repo.add_channel(
            db,
            channel_id=chat.id,
            title=title,
            invite_link=invite_link,
            entry_type=entry_type,
        )
        await repo.reset_user_gates(db)
        await state.clear()
        await message.answer(M_ENTRY_ADDED, parse_mode=PARSE_MODE)
        entries = await repo.list_channels_by_type(db, entry_type)
        await message.answer(
            render_entries_list(entries, entry_type),
            parse_mode=PARSE_MODE,
            reply_markup=entries_kb(entries, entry_type),
        )
    else:
        # Invite link unknown — ask the admin to provide one.
        await state.update_data(channel_id=chat.id, title=title)
        await state.set_state(MandatoryEntryFSM.invite_link)
        await message.answer(M_ENTRY_LINK_PROMPT, parse_mode=PARSE_MODE)


@router.message(StateFilter(MandatoryEntryFSM.invite_link))
async def entry_invite_link_entered(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    entry_type = data.get("entry_type", "channel")
    await repo.add_channel(
        db,
        channel_id=data["channel_id"],
        title=data["title"],
        invite_link=message.text or "",
        entry_type=entry_type,
    )
    await repo.reset_user_gates(db)
    await state.clear()
    await message.answer(M_ENTRY_ADDED, parse_mode=PARSE_MODE)
    entries = await repo.list_channels_by_type(db, entry_type)
    await message.answer(
        render_entries_list(entries, entry_type),
        parse_mode=PARSE_MODE,
        reply_markup=entries_kb(entries, entry_type),
    )


@router.callback_query(F.data.startswith(C.CH_TOGGLE))
async def cb_toggle_entry(cb: CallbackQuery, db: Database) -> None:
    await cb.answer(M_ENTRY_TOGGLED)
    parsed = _int_pair(cb.data.removeprefix(C.CH_TOGGLE))
    if parsed is None:
        await safe_edit(cb, M_ENTRY_NOT_FOUND, parse_mode=PARSE_MODE)
        return
    entry_type, entry_db_id = parsed
    await repo.toggle_channel(db, entry_db_id)
    await repo.reset_user_gates(db)
    await _render_tab(cb, db, entry_type)


@router.callback_query(F.data.startswith(C.CH_DELETE))
async def cb_delete_entry(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    parsed = _int_pair(cb.data.removeprefix(C.CH_DELETE))
    if parsed is None:
        await safe_edit(cb, M_ENTRY_NOT_FOUND, parse_mode=PARSE_MODE)
        return
    entry_type, entry_db_id = parsed
    await safe_edit(
        cb,
        M_ENTRY_DELETE_PROMPT,
        parse_mode=PARSE_MODE,
        reply_markup=entry_delete_confirm_kb(entry_type, entry_db_id),
    )


@router.callback_query(F.data.startswith(C.CH_DELETE_OK))
async def cb_delete_entry_confirm(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    parsed = _int_pair(cb.data.removeprefix(C.CH_DELETE_OK))
    if parsed is None:
        await safe_edit(cb, M_ENTRY_NOT_FOUND, parse_mode=PARSE_MODE)
        return
    entry_type, entry_db_id = parsed
    await repo.delete_channel(db, entry_db_id)
    await repo.reset_user_gates(db)
    await _render_tab(cb, db, entry_type)
