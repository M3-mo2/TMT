"""Common handlers: /start, /help, /cancel, main-menu callbacks, fallback.

Included last so FSM-filtered routers match before the catch-all fallback.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup

from app.bot.callbacks import GateCB, MenuCB
from app.bot.gate import check_membership
from app.bot.keyboards import help_back, main_menu
from app.bot.texts import (
    M_CANCELED,
    M_ERR_GENERIC,
    M_GATE_NOT_VERIFIED,
    M_GATE_VERIFIED,
    M_HELP,
    PARSE_MODE,
    render_main_menu,
)
from app.core.account_service import AccountService
from app.core.job_manager import JobManager
from app.db import repositories as repo
from app.db.database import Database
from app.tg.login import LoginFlowManager

logger = logging.getLogger(__name__)

__all__ = ["router", "edit_or_answer", "delete_quietly"]

router = Router(name="common")


async def edit_or_answer(
    query: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None
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


@router.callback_query(GateCB.filter(F.action == "verify"))
async def cb_gate_verify(
    query: CallbackQuery, callback_data: GateCB, db: Database, bot: Bot,
    accounts: AccountService, jobs: JobManager,
) -> None:
    """User pressed '✅ تحقق من الاشتراك' on the gate screen."""
    user_id = query.from_user.id if query.from_user else 0
    mandatory = await repo.active_channels(db)
    if not mandatory:
        await query.answer()
    elif await check_membership(bot, user_id, mandatory):
        await repo.set_gate_cleared(db, user_id)
        await query.answer(M_GATE_VERIFIED)
    else:
        await query.answer(M_GATE_NOT_VERIFIED, show_alert=True)
        return
    account_count = len(await accounts.list(user_id))
    job_count = len(await jobs.list_jobs(user_id, limit=100))
    display_name = query.from_user.full_name if query.from_user else "مستخدم"
    text = render_main_menu(display_name, account_count, job_count, user_id)
    await edit_or_answer(query, text, main_menu())


@router.message(CommandStart())
async def cmd_start(
    message: Message, accounts: AccountService, jobs: JobManager
) -> None:
    if message.from_user is not None:
        owner_id = message.from_user.id
        display_name = message.from_user.full_name
        account_count = len(await accounts.list(owner_id))
        job_count = len(await jobs.list_jobs(owner_id, limit=100))
        text = render_main_menu(display_name, account_count, job_count, owner_id)
    else:
        text = render_main_menu("مستخدم", 0, 0, 0)
    await message.answer(text, parse_mode=PARSE_MODE, reply_markup=main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(M_HELP, parse_mode=PARSE_MODE, reply_markup=help_back())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, logins: LoginFlowManager) -> None:
    await state.clear()
    if message.from_user is not None:
        await logins.cancel(message.from_user.id)
    await message.answer(M_CANCELED, parse_mode=PARSE_MODE, reply_markup=main_menu())


@router.callback_query(MenuCB.filter(F.action == "main"))
async def cb_main(
    query: CallbackQuery, callback_data: MenuCB, state: FSMContext,
    accounts: AccountService, jobs: JobManager,
) -> None:
    await state.clear()
    await query.answer()
    if query.from_user is not None:
        owner_id = query.from_user.id
        display_name = query.from_user.full_name
        account_count = len(await accounts.list(owner_id))
        job_count = len(await jobs.list_jobs(owner_id, limit=100))
        text = render_main_menu(display_name, account_count, job_count, owner_id)
    else:
        text = render_main_menu("مستخدم", 0, 0, 0)
    await edit_or_answer(query, text, main_menu())


@router.callback_query(MenuCB.filter(F.action == "help"))
async def cb_help(query: CallbackQuery, callback_data: MenuCB) -> None:
    await query.answer()
    await edit_or_answer(query, M_HELP, help_back())


@router.message()
async def fallback(
    message: Message, state: FSMContext, accounts: AccountService, jobs: JobManager
) -> None:
    """Any unmatched text returns the main menu."""
    await state.clear()
    if message.from_user is not None:
        owner_id = message.from_user.id
        display_name = message.from_user.full_name
        account_count = len(await accounts.list(owner_id))
        job_count = len(await jobs.list_jobs(owner_id, limit=100))
        text = render_main_menu(display_name, account_count, job_count, owner_id)
    else:
        text = render_main_menu("مستخدم", 0, 0, 0)
    await message.answer(text, parse_mode=PARSE_MODE, reply_markup=main_menu())
