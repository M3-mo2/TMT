"""Admin: user management — list, detail card, block, notify, account delete."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import (
    account_del_confirm_kb,
    user_detail_kb,
    users_list_kb,
)
from app.bot.texts import M_ACCOUNT_BUSY, PARSE_MODE, render_user_card, user_full_name
from app.core.models import UserStats
from app.db import repositories as repo
from app.db.database import Database
from app.services.helpers import safe_edit

router = Router()

USERS_PER_PAGE = 9


class AdminNotifyFSM(StatesGroup):
    waiting_text = State()


def _back_to_users_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="› رجوع للقائمة", callback_data=C.USERS)]]
    )


def _open_user_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="› عرض البطاقة", callback_data=f"{C.USER_OPEN}{uid}")]
        ]
    )


def _cancel_notify_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ إلغاء", callback_data=f"{C.USER_NOTIFY_CANCEL}{uid}")]
        ]
    )


def _int_after(data: str, prefix: str) -> int | None:
    """Parse an int trailing callback data; None if malformed.

    Callback data is user-controlled input, not authorization (RULES §4),
    so a forged non-numeric suffix must not raise a 500."""
    try:
        return int(data.removeprefix(prefix))
    except ValueError:
        return None


async def _user_card_payload(
    db: Database, uid: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Fetch a user + their accounts/stats and build (text, keyboard).

    Returns None when the user does not exist. Accounts are owner-scoped
    (RULES §4): the admin reads another user's accounts via their owner_id."""
    user = await repo.get_user(db, uid)
    if user is None:
        return None
    accounts = await repo.list_accounts(db, uid)
    stats = UserStats(
        accounts=len(accounts),
        jobs=await repo.count_jobs_for_user(db, uid),
        completed=await repo.count_completed_jobs_for_user(db, uid),
        failed=await repo.count_failed_jobs_for_user(db, uid),
        active=await repo.count_active_jobs_for_user(db, uid),
    )
    kb = user_detail_kb(
        user,
        [
            {"id": a.id, "tg_user_id": a.tg_user_id, "tg_username": a.tg_username}
            for a in accounts
        ],
    )
    return render_user_card(user, [a.to_public() for a in accounts], stats), kb


async def _render_users_page(cb: CallbackQuery, db: Database, page: int) -> None:
    total = await repo.count_users(db)
    per_page = USERS_PER_PAGE
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page < 0:
        page = 0
    if page >= total_pages:
        page = max(0, total_pages - 1)
    users = await repo.list_all_users(db, limit=per_page, offset=page * per_page)
    await safe_edit(
        cb,
        f"👥 المستخدمون ({total})",
        reply_markup=users_list_kb(users, page, total_pages),
    )


@router.callback_query(F.data == C.USERS)
async def cb_users_list_root(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    await _render_users_page(cb, db, 0)


@router.callback_query(F.data.startswith(C.USER_PAGE))
async def cb_users_page(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    page = _int_after(cb.data, C.USER_PAGE)
    if page is None:
        page = 0
    await _render_users_page(cb, db, page)


@router.callback_query(F.data.startswith(C.USER_OPEN))
async def cb_user_open(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    uid = _int_after(cb.data, C.USER_OPEN)
    if uid is None:
        await safe_edit(cb, "⚠️ المستخدم غير موجود.", reply_markup=_back_to_users_kb())
        return
    payload = await _user_card_payload(db, uid)
    if payload is None:
        await safe_edit(cb, "⚠️ المستخدم غير موجود.", reply_markup=_back_to_users_kb())
        return
    text, kb = payload
    await safe_edit(cb, text, reply_markup=kb)


@router.callback_query(F.data.startswith(C.USER_TOGGLE_BLOCK))
async def cb_user_toggle_block(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    uid = _int_after(cb.data, C.USER_TOGGLE_BLOCK)
    if uid is None:
        await safe_edit(cb, "⚠️ المستخدم غير موجود.", reply_markup=_back_to_users_kb())
        return
    user = await repo.get_user(db, uid)
    if user is None:
        await safe_edit(cb, "⚠️ المستخدم غير موجود.", reply_markup=_back_to_users_kb())
        return
    blocked = not bool(user["is_blocked"])
    await repo.set_user_blocked(db, uid, blocked)
    await repo.audit(
        db,
        "user_block_toggled",
        owner_id=uid,
        detail={"blocked": blocked, "actor_admin": cb.from_user.id if cb.from_user else None},
    )
    payload = await _user_card_payload(db, uid)
    if payload is not None:
        await safe_edit(cb, payload[0], reply_markup=payload[1])


@router.callback_query(F.data.startswith(C.USER_NOTIFY))
async def cb_user_notify(cb: CallbackQuery, state: FSMContext, db: Database) -> None:
    await cb.answer()
    uid = _int_after(cb.data, C.USER_NOTIFY)
    if uid is None:
        await safe_edit(cb, "⚠️ المستخدم غير موجود.", reply_markup=_back_to_users_kb())
        return
    user = await repo.get_user(db, uid)
    if user is None:
        await safe_edit(cb, "⚠️ المستخدم غير موجود.", reply_markup=_back_to_users_kb())
        return
    await state.update_data(target_user_id=uid)
    await state.set_state(AdminNotifyFSM.waiting_text)
    await safe_edit(
        cb,
        "📝 أرسل الرسالة الآن — سيتم إرسال نسخة بالتنسيق إلى المستخدم.\n↩️ للإلغاء.",
        reply_markup=_cancel_notify_kb(uid),
    )


@router.callback_query(F.data.startswith(C.USER_NOTIFY_CANCEL))
async def cb_user_notify_cancel(
    cb: CallbackQuery, state: FSMContext, db: Database
) -> None:
    await state.clear()
    uid = _int_after(cb.data, C.USER_NOTIFY_CANCEL)
    if uid is None:
        await safe_edit(cb, "⚠️ غير موجود.", reply_markup=_back_to_users_kb())
        return
    payload = await _user_card_payload(db, uid)
    if payload is None:
        await safe_edit(cb, "⚠️ المستخدم غير موجود.", reply_markup=_back_to_users_kb())
        return
    await safe_edit(cb, payload[0], reply_markup=payload[1])


@router.message(StateFilter(AdminNotifyFSM.waiting_text))
async def admin_notify_text(message: Message, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    uid = int(data["target_user_id"])
    user = await repo.get_user(db, uid)
    if user is None:
        await state.clear()
        await message.answer("⚠️ المستخدم غير موجود.", reply_markup=_back_to_users_kb())
        return
    try:
        await message.bot.copy_message(
            chat_id=uid,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramAPIError:
        await state.clear()
        await message.reply(
            f"⚠️ لم أتمكن من إرسال الرسالة للمستخدم <code>{uid}</code> "
            "(قد يكون قد حظر البوت).",
            parse_mode=PARSE_MODE,
        )
        return
    await repo.audit(
        db,
        "user_message_sent",
        owner_id=uid,
        detail={"actor_admin": message.from_user.id if message.from_user else None},
    )
    await state.clear()
    await message.reply(
        f"✅ تم إرسال النسخة إلى <b>{user_full_name(user)}</b>.",
        reply_markup=_open_user_kb(uid),
        parse_mode=PARSE_MODE,
    )


@router.callback_query(F.data.startswith(C.USER_DEL_ACC_CONFIRM))
async def cb_account_delete_confirm(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    acc_id = _int_after(cb.data, C.USER_DEL_ACC_CONFIRM)
    if acc_id is None:
        await safe_edit(cb, "⚠️ الحساب غير موجود.", reply_markup=_back_to_users_kb())
        return
    acc = await db.fetch_one("SELECT id, owner_id FROM accounts WHERE id=?", (acc_id,))
    if acc is None:
        await safe_edit(cb, "⚠️ الحساب غير موجود.", reply_markup=_back_to_users_kb())
        return
    await safe_edit(
        cb,
        f"🗑️ حذف الحساب <code>#{acc_id}</code> من المستخدم <code>{acc['owner_id']}</code>؟",
        reply_markup=account_del_confirm_kb(acc_id, acc["owner_id"]),
    )


@router.callback_query(F.data.startswith(C.USER_DEL_ACC_OK))
async def cb_account_delete_ok(cb: CallbackQuery, db: Database) -> None:
    await cb.answer()
    acc_id = _int_after(cb.data, C.USER_DEL_ACC_OK)
    if acc_id is None:
        await safe_edit(cb, "⚠️ الحساب غير موجود.", reply_markup=_back_to_users_kb())
        return
    acc = await db.fetch_one("SELECT id, owner_id FROM accounts WHERE id=?", (acc_id,))
    if acc is None:
        await safe_edit(cb, "⚠️ الحساب غير موجود.", reply_markup=_back_to_users_kb())
        return
    uid = acc["owner_id"]
    if await repo.count_active_jobs_for_account(db, acc_id) > 0:
        await safe_edit(cb, M_ACCOUNT_BUSY, reply_markup=_back_to_users_kb())
        return
    await repo.delete_account_with_audit(db, uid, acc_id)
    await repo.audit(
        db,
        "admin_account_deleted",
        owner_id=uid,
        account_id=acc_id,
        detail={"actor_admin": cb.from_user.id if cb.from_user else None},
    )
    payload = await _user_card_payload(db, uid)
    if payload is not None:
        await safe_edit(cb, payload[0], reply_markup=payload[1])
    else:
        await safe_edit(cb, "⚠️ المستخدم غير موجود.", reply_markup=_back_to_users_kb())
