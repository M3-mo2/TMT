"""Common handlers: /start, /help, /cancel, main-menu callbacks, fallback.

Included last so FSM-filtered routers match before the catch-all fallback.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.callbacks import MenuCB
from app.bot.keyboards import main_menu
from app.bot.texts import (
    M_CANCELED,
    M_ERR_GENERIC,
    M_HELP,
    M_MAIN,
    PARSE_MODE,
)
from app.tg.login import LoginFlowManager

logger = logging.getLogger(__name__)

__all__ = ["router", "edit_or_answer", "delete_quietly"]

router = Router(name="common")


async def edit_or_answer(
    query: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Edit the callback's message, falling back to a callback answer when the
    message is inaccessible or unchanged (double-tap / refresh)."""
    message = query.message
    try:
        if isinstance(message, Message):
            await message.edit_text(text, parse_mode=PARSE_MODE, reply_markup=reply_markup)
        else:  # pragma: no cover - inaccessible message
            await query.answer(M_ERR_GENERIC, show_alert=True)
    except Exception:
        logger.info("callback edit fell back to answer", exc_info=True)
        try:
            await query.answer()
        except Exception:  # pragma: no cover - defensive
            pass


async def delete_quietly(message: Message) -> None:
    """Delete the user's message (login code/password) — best effort."""
    try:
        await message.delete()
    except Exception:
        logger.info("user message deletion failed", exc_info=True)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(M_MAIN, parse_mode=PARSE_MODE, reply_markup=main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(M_HELP, parse_mode=PARSE_MODE, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, logins: LoginFlowManager) -> None:
    await state.clear()
    if message.from_user is not None:
        await logins.cancel(message.from_user.id)
    await message.answer(M_CANCELED, parse_mode=PARSE_MODE, reply_markup=main_menu())


@router.callback_query(MenuCB.filter(F.action == "main"))
async def cb_main(query: CallbackQuery, callback_data: MenuCB, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    await edit_or_answer(query, M_MAIN, main_menu())


@router.callback_query(MenuCB.filter(F.action == "help"))
async def cb_help(query: CallbackQuery, callback_data: MenuCB) -> None:
    await query.answer()
    await edit_or_answer(query, M_HELP, main_menu())


@router.message()
async def fallback(message: Message, state: FSMContext) -> None:
    """Any unmatched text returns the main menu."""
    await state.clear()
    await message.answer(M_MAIN, parse_mode=PARSE_MODE, reply_markup=main_menu())
