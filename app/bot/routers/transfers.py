"""Transfer wizard: pick account → source → dest → preflight → confirm.

FSM data stays JSON-safe: only the account id and the raw source/dest refs are
stored (never ResolvedEntity objects); every step that needs entities
re-resolves the refs fresh against the account's pooled client, so stale
objects cannot leak into a job.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import MenuCB, TransferCB
from app.bot.keyboards import main_menu, preflight_screen, wizard_account_picker, wizard_cancel
from app.bot.routers.common import edit_or_answer
from app.bot.states import TransferFSM
from app.bot.texts import (
    M_ASK_SOURCE,
    M_CHECKING,
    M_ERR_GENERIC,
    M_MAIN,
    M_NOT_FOUND,
    M_NO_ACTIVE_ACCOUNTS,
    M_PICK_ACCOUNT,
    M_SAME_GROUP,
    M_SOURCE_RESOLVED,
    M_TRANSFER_STARTED,
    M_WIZARD_CANCELLED,
    M_WIZARD_TITLE,
    PARSE_MODE,
    esc,
    render_preflight,
)
from app.config import Config
from app.core.account_service import AccountService, ServiceError
from app.core.job_manager import JobManager
from app.core.models import AccountStatus, CheckStatus
from app.tg import resolver
from app.tg.client_pool import ClientPool
from app.tg.resolver import ResolutionError
from app.tg.preflight import run_preflight

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = Router(name="transfers")


async def _resolve(
    accounts: AccountService,
    pool: ClientPool,
    owner_id: int,
    account_id: int,
    raw: str,
):
    """Resolve ``raw`` through the account's pooled client. ServiceError
    (ownership/session) and ResolutionError carry Arabic messages verbatim."""
    session = await accounts.get_session(owner_id, account_id)
    client = await pool.get(account_id, session)
    return await resolver.resolve_entity(client, raw)


def _wizard_data(data: dict) -> tuple[int | None, str | None, str | None]:
    return (
        data.get("account_id"),
        data.get("source_ref"),
        data.get("dest_ref"),
    )


# ---------------------------------------------------------------- entry


@router.callback_query(MenuCB.filter(F.action == "transfers"))
@router.callback_query(TransferCB.filter(F.action == "new"))
async def start_wizard(
    query: CallbackQuery, callback_data: MenuCB | TransferCB, state: FSMContext, accounts: AccountService
) -> None:
    await query.answer()
    items = [
        a
        for a in await accounts.list(query.from_user.id)  # type: ignore[union-attr]
        if a.status is AccountStatus.ACTIVE
    ]
    await state.clear()
    if not items:
        await edit_or_answer(query, M_NO_ACTIVE_ACCOUNTS, main_menu())
        return
    await state.set_state(TransferFSM.account)
    await edit_or_answer(
        query, f"{M_WIZARD_TITLE}\n{M_PICK_ACCOUNT}", wizard_account_picker(items)
    )


@router.callback_query(TransferCB.filter(F.action == "cancel_wizard"))
async def cancel_wizard(query: CallbackQuery, callback_data: TransferCB, state: FSMContext) -> None:
    await query.answer()
    await state.clear()
    await edit_or_answer(query, M_WIZARD_CANCELLED, main_menu())


@router.callback_query(TransferCB.filter(F.action == "pick_account"))
async def pick_account(
    query: CallbackQuery, callback_data: TransferCB, state: FSMContext, accounts: AccountService
) -> None:
    await query.answer()
    account = await accounts.get(query.from_user.id, callback_data.account_id)  # type: ignore[union-attr]
    if account is None or account.status is not AccountStatus.ACTIVE:
        await edit_or_answer(query, M_NOT_FOUND, main_menu())
        return
    await state.update_data(account_id=account.id)
    await state.set_state(TransferFSM.source)
    await edit_or_answer(query, M_ASK_SOURCE, wizard_cancel())


# ---------------------------------------------------------------- source / dest


@router.message(TransferFSM.source)
async def source_entered(
    message: Message, state: FSMContext, accounts: AccountService, pool: ClientPool
) -> None:
    if message.from_user is None:  # pragma: no cover - gated by middleware
        return
    raw = (message.text or "").strip()
    if not raw:
        return
    account_id = (await state.get_data()).get("account_id")
    if account_id is None:
        await state.clear()
        await message.answer(M_WIZARD_CANCELLED, parse_mode=PARSE_MODE, reply_markup=main_menu())
        return
    try:
        entity = await _resolve(accounts, pool, message.from_user.id, int(account_id), raw)
    except (ServiceError, ResolutionError) as exc:
        await message.answer(esc(exc.message), parse_mode=PARSE_MODE, reply_markup=wizard_cancel())
        return
    await state.update_data(source_ref=entity.raw_ref or raw)
    await state.set_state(TransferFSM.dest)
    await message.answer(
        M_SOURCE_RESOLVED.format(title=esc(entity.title)),
        parse_mode=PARSE_MODE,
        reply_markup=wizard_cancel(),
    )


async def _run_check(
    message: Message,
    state: FSMContext,
    accounts: AccountService,
    pool: ClientPool,
    config: Config,
    owner_id: int,
    account_id: int,
    source_ref: str,
    dest_ref: str | None,
    dest_raw: str | None,
) -> bool:
    """Resolve both sides, guard equality, run preflight, render the report.

    Returns True when the wizard advanced past the check (or re-rendered it),
    False when the user should stay on the current step."""
    try:
        source = await _resolve(accounts, pool, owner_id, account_id, source_ref)
        dest = await _resolve(
            accounts, pool, owner_id, account_id, dest_raw or (dest_ref or "")
        )
    except (ServiceError, ResolutionError) as exc:
        await message.answer(esc(exc.message), parse_mode=PARSE_MODE, reply_markup=wizard_cancel())
        return False
    if source.id == dest.id:
        await message.answer(M_SAME_GROUP, parse_mode=PARSE_MODE, reply_markup=wizard_cancel())
        return False
    placeholder = await message.answer(M_CHECKING, parse_mode=PARSE_MODE)
    try:
        checks = await run_preflight(
            client=await pool.get(account_id, await accounts.get_session(owner_id, account_id)),
            source=source,
            dest=dest,
            max_members=config.max_members_per_job,
        )
    except Exception:
        logger.warning("preflight crashed for owner %s", owner_id, exc_info=True)
        checks = []
        await message.answer(M_ERR_GENERIC, parse_mode=PARSE_MODE)
        return False
    has_fail = any(c.status is CheckStatus.FAIL for c in checks)
    if dest_ref is None:
        await state.update_data(dest_ref=dest.raw_ref or (dest_raw or ""))
    try:
        await placeholder.edit_text(
            render_preflight(checks),
            parse_mode=PARSE_MODE,
            reply_markup=preflight_screen(has_fail, account_id),
        )
    except Exception:  # pragma: no cover - placeholder edit is best-effort
        logger.info("preflight placeholder edit failed", exc_info=True)
    return True


@router.message(TransferFSM.dest)
async def dest_entered(
    message: Message,
    state: FSMContext,
    accounts: AccountService,
    pool: ClientPool,
    config: Config,
) -> None:
    if message.from_user is None:  # pragma: no cover - gated by middleware
        return
    raw = (message.text or "").strip()
    if not raw:
        return
    data = await state.get_data()
    account_id, source_ref, _ = _wizard_data(data)
    if account_id is None or not source_ref:
        await state.clear()
        await message.answer(M_WIZARD_CANCELLED, parse_mode=PARSE_MODE, reply_markup=main_menu())
        return
    advanced = await _run_check(
        message, state, accounts, pool, config, message.from_user.id,
        int(account_id), source_ref, None, raw,
    )
    if advanced:
        await state.set_state(None)


# ---------------------------------------------------------------- refresh / confirm


@router.callback_query(TransferCB.filter(F.action == "refresh"))
async def refresh_check(
    query: CallbackQuery,
    callback_data: TransferCB,
    state: FSMContext,
    accounts: AccountService,
    pool: ClientPool,
    config: Config,
) -> None:
    await query.answer()
    if query.message is None or query.from_user is None:
        return
    data = await state.get_data()
    account_id, source_ref, dest_ref = _wizard_data(data)
    if account_id is None or not source_ref or not dest_ref:
        await state.clear()
        await edit_or_answer(query, M_WIZARD_CANCELLED, main_menu())
        return
    await _run_check(
        query.message, state, accounts, pool, config, query.from_user.id,
        int(account_id), source_ref, dest_ref, None,
    )


@router.callback_query(TransferCB.filter(F.action == "confirm"))
async def confirm_transfer(
    query: CallbackQuery,
    callback_data: TransferCB,
    state: FSMContext,
    accounts: AccountService,
    pool: ClientPool,
    jobs: JobManager,
    config: Config,
    reporter: Any = None,
) -> None:
    await query.answer()
    if query.from_user is None:  # pragma: no cover - gated by middleware
        return
    owner_id = query.from_user.id
    data = await state.get_data()
    account_id, source_ref, dest_ref = _wizard_data(data)
    if account_id is None or not source_ref or not dest_ref:
        await state.clear()
        await edit_or_answer(query, M_WIZARD_CANCELLED, main_menu())
        return
    try:
        source = await _resolve(accounts, pool, owner_id, int(account_id), source_ref)
        dest = await _resolve(accounts, pool, owner_id, int(account_id), dest_ref)
        # No same-group re-check here: preflight's dest_diff is the single rule
        # (the confirm keyboard only exists when preflight had no FAIL, and the
        # job runner re-runs preflight and fails on dest_diff regardless).
        job = await jobs.create_job(owner_id, int(account_id), source, dest)
    except (ServiceError, ResolutionError) as exc:
        await edit_or_answer(query, esc(exc.message), wizard_cancel())
        return
    await state.clear()
    if reporter is not None and query.message is not None:
        try:
            await reporter.register(job.id, query.message.chat.id)
        except Exception:
            logger.warning("reporter register failed for job %s", job.id, exc_info=True)
    await edit_or_answer(query, M_TRANSFER_STARTED, main_menu())
