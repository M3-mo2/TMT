"""Account flows: list, add (login FSM), card, delete with confirm.

Every callback re-verifies ownership through :class:`AccountService`
(callback data is user-controlled input, RULES §4).
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import AccountCB, MenuCB
from app.bot.keyboards import (
    account_card,
    account_delete_confirm,
    accounts_list,
    login_cancel,
    main_menu,
)
from app.bot.routers.common import delete_quietly, edit_or_answer
from app.bot.states import AddAccountFSM
from app.bot.texts import (
    M_ACCOUNTS_EMPTY,
    M_ACCOUNTS_TITLE,
    M_ADD_ACCOUNT_PROMPT,
    M_ASK_CODE,
    M_ASK_PASSWORD,
    M_DELETED,
    M_DELETE_CONFIRM,
    M_INVALID_PHONE,
    M_LOGIN_CANCELLED,
    M_LOGIN_EXPIRED,
    M_NOT_FOUND,
    M_RETRY_AFTER,
    M_SAVED,
    PARSE_MODE,
    esc,
    normalize_phone,
    render_account_card,
)
from app.core.account_service import AccountService, ServiceError
from app.core.models import AccountStatus
from app.tg.client_pool import ClientPool
from app.tg.errors import LoginFailure
from app.tg.login import LoginFlowManager, LoginResult

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = Router(name="accounts")

#: ClientPool scratch keys for fetching the profile of a not-yet-stored session
#: (the login code path returns only the session string; the profile is needed
#: for the UNIQUE(owner, tg_user_id) account row). Negative ids keep scratch
#: slots disjoint from real account rows and from each other (one per owner —
#: a shared key would let two concurrent logins read each other's profile).
def _temp_session_key(owner_id: int) -> int:
    return -owner_id


async def _profile_from_session(
    pool: ClientPool, owner_id: int, session_string: str
) -> LoginResult:
    client = await pool.get(_temp_session_key(owner_id), session_string)
    try:
        me = await client.get_me()
    finally:
        await pool.discard(_temp_session_key(owner_id))
    if me is None or getattr(me, "id", None) is None:
        raise LoginFailure("UNEXPECTED", "تعذر قراءة بيانات الحساب بعد الدخول.")
    display_name = " ".join(
        part
        for part in (getattr(me, "first_name", None), getattr(me, "last_name", None))
        if part
    ).strip() or getattr(me, "username", None) or str(me.id)
    return LoginResult(
        session_string=session_string,
        tg_user_id=int(me.id),
        username=getattr(me, "username", None),
        display_name=display_name,
    )


async def _login_failure(
    message: Message,
    state: FSMContext,
    logins: LoginFlowManager,
    owner_id: int,
    exc: LoginFailure,
) -> None:
    """Render a login failure; a missing flow means TTL expiry mid-flow."""
    if not logins.has_flow(owner_id):
        await state.clear()
        await message.answer(M_LOGIN_EXPIRED, parse_mode=PARSE_MODE, reply_markup=main_menu())
        return
    text = esc(exc.message)
    if exc.wait_seconds:
        text += "\n" + M_RETRY_AFTER.format(seconds=exc.wait_seconds)
    await message.answer(text, parse_mode=PARSE_MODE, reply_markup=login_cancel())


async def _save_and_show(
    message: Message,
    state: FSMContext,
    accounts: AccountService,
    owner_id: int,
    result: LoginResult,
) -> None:
    account = await accounts.save_login(owner_id, (await state.get_data()).get("phone", ""), result)
    await state.clear()
    await message.answer(M_SAVED, parse_mode=PARSE_MODE)
    await message.answer(
        render_account_card(account), parse_mode=PARSE_MODE, reply_markup=account_card(account.id)
    )


# ---------------------------------------------------------------- menu/list


@router.callback_query(MenuCB.filter(F.action == "accounts"))
@router.callback_query(AccountCB.filter(F.action == "list"))
async def show_accounts(
    query: CallbackQuery, callback_data: MenuCB | AccountCB, accounts: AccountService
) -> None:
    items = await accounts.list(query.from_user.id)  # type: ignore[union-attr]
    await query.answer()
    text = M_ACCOUNTS_TITLE if items else M_ACCOUNTS_EMPTY
    await edit_or_answer(query, text, accounts_list(items))


# ---------------------------------------------------------------- add / login


@router.callback_query(AccountCB.filter(F.action == "add"))
async def add_account(
    query: CallbackQuery, callback_data: AccountCB, state: FSMContext
) -> None:
    await query.answer()
    await state.clear()
    await state.set_state(AddAccountFSM.phone)
    await edit_or_answer(query, M_ADD_ACCOUNT_PROMPT, login_cancel())


@router.callback_query(AccountCB.filter(F.action == "cancel_login"))
async def cancel_login(
    query: CallbackQuery, callback_data: AccountCB, state: FSMContext, logins: LoginFlowManager
) -> None:
    await query.answer()
    if query.from_user is not None:
        await logins.cancel(query.from_user.id)
    await state.clear()
    await edit_or_answer(query, M_LOGIN_CANCELLED, main_menu())


@router.message(AddAccountFSM.phone)
async def phone_entered(
    message: Message, state: FSMContext, logins: LoginFlowManager
) -> None:
    if message.from_user is None:  # pragma: no cover - gated by middleware
        return
    phone = normalize_phone(message.text or "")
    if len(phone) < 5 or not phone.startswith("+"):
        await message.answer(M_INVALID_PHONE, parse_mode=PARSE_MODE, reply_markup=login_cancel())
        return
    try:
        await logins.start(message.from_user.id, phone)
    except LoginFailure as exc:
        # start() never registers a flow, so _login_failure's expiry branch
        # would mask the real reason — render the classified message directly.
        text = esc(exc.message)
        if exc.wait_seconds:
            text += "\n" + M_RETRY_AFTER.format(seconds=exc.wait_seconds)
        await message.answer(text, parse_mode=PARSE_MODE, reply_markup=login_cancel())
        return
    await state.update_data(phone=phone)
    await state.set_state(AddAccountFSM.code)
    await message.answer(M_ASK_CODE, parse_mode=PARSE_MODE, reply_markup=login_cancel())


@router.message(AddAccountFSM.code)
async def code_entered(
    message: Message,
    state: FSMContext,
    logins: LoginFlowManager,
    accounts: AccountService,
    pool: ClientPool,
) -> None:
    if message.from_user is None:  # pragma: no cover - gated by middleware
        return
    owner_id = message.from_user.id
    await delete_quietly(message)  # the code is a secret (RULES §3/§7)
    try:
        result = await logins.submit_code(owner_id, (message.text or "").strip())
    except LoginFailure as exc:
        await _login_failure(message, state, logins, owner_id, exc)
        return
    if result == "password":
        await state.set_state(AddAccountFSM.password)
        await message.answer(M_ASK_PASSWORD, parse_mode=PARSE_MODE, reply_markup=login_cancel())
        return
    try:
        profile = await _profile_from_session(pool, owner_id, result)
    except LoginFailure as exc:
        await _login_failure(message, state, logins, owner_id, exc)
        return
    await _save_and_show(message, state, accounts, owner_id, profile)


@router.message(AddAccountFSM.password)
async def password_entered(
    message: Message,
    state: FSMContext,
    logins: LoginFlowManager,
    accounts: AccountService,
) -> None:
    if message.from_user is None:  # pragma: no cover - gated by middleware
        return
    owner_id = message.from_user.id
    await delete_quietly(message)  # the password is a secret (RULES §3/§7)
    try:
        result = await logins.submit_password(owner_id, (message.text or "").strip())
    except LoginFailure as exc:
        await _login_failure(message, state, logins, owner_id, exc)
        return
    await _save_and_show(message, state, accounts, owner_id, result)


# ---------------------------------------------------------------- card / delete


@router.callback_query(AccountCB.filter(F.action == "view"))
async def view_account(
    query: CallbackQuery, callback_data: AccountCB, accounts: AccountService
) -> None:
    await query.answer()
    account = await accounts.get(query.from_user.id, callback_data.account_id)  # type: ignore[union-attr]
    if account is None:
        await edit_or_answer(query, M_NOT_FOUND, accounts_list([]))
        return
    await edit_or_answer(query, render_account_card(account), account_card(account.id))


@router.callback_query(AccountCB.filter(F.action == "delete_confirm"))
async def delete_confirm(
    query: CallbackQuery, callback_data: AccountCB, accounts: AccountService
) -> None:
    await query.answer()
    account = await accounts.get(query.from_user.id, callback_data.account_id)  # type: ignore[union-attr]
    if account is None:
        await edit_or_answer(query, M_NOT_FOUND, accounts_list([]))
        return
    await edit_or_answer(
        query,
        M_DELETE_CONFIRM.format(name=esc(account.display_name)),
        account_delete_confirm(account.id),
    )


@router.callback_query(AccountCB.filter(F.action == "delete"))
async def delete_account(
    query: CallbackQuery, callback_data: AccountCB, accounts: AccountService
) -> None:
    owner_id = query.from_user.id  # type: ignore[union-attr]
    await query.answer()
    try:
        await accounts.remove(owner_id, callback_data.account_id)
    except ServiceError as exc:
        await edit_or_answer(query, esc(exc.message), account_card(callback_data.account_id))
        return
    items = await accounts.list(owner_id)
    text = M_DELETED + "\n" + (M_ACCOUNTS_TITLE if items else M_ACCOUNTS_EMPTY)
    await edit_or_answer(query, text, accounts_list(items))
