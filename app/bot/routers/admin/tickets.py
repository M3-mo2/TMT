"""Admin: ticket management — list (with status filter), detail, change
status / priority, reply to user (bot DM), close."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.routers.admin import callbacks as C
from app.bot.routers.admin.keyboards import (
    admin_ticket_detail_kb,
    ticket_status_change_kb,
    tickets_status_filter_kb,
)
from app.bot.routers.common import edit_or_answer
from app.bot.texts import (
    M_TICKET_CLOSED,
    M_TICKET_NOT_FOUND,
    M_TICKET_PRIORITY_SET,
    M_TICKET_STATUS_SET,
    PARSE_MODE,
    esc,
    render_admin_tickets_list,
    render_ticket_detail,
)
from app.db import repositories as repo
from app.db.database import Database

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = Router()


class AdminTicketFSM(StatesGroup):
    reply_body = State()


def _int_after(data: str, prefix: str) -> int | None:
    try:
        return int(data.removeprefix(prefix))
    except ValueError:
        return None


def _status_from_filter(callback_data: str) -> str | None:
    """Map a filter callback constant to the corresponding DB status value."""
    if callback_data == C.TICKETS_ALL:
        return None
    if callback_data == C.TICKETS_OPEN:
        return "open"
    if callback_data == C.TICKETS_IN_PROGRESS:
        return "in_progress"
    if callback_data == C.TICKETS_CLOSED:
        return "closed"
    return None


# ---------------------------------------------------------------- list


@router.callback_query(F.data == C.TICKETS)
async def cb_tickets_list(query: CallbackQuery, db: Database) -> None:
    """Admin main → all tickets (default filter: all)."""
    await query.answer()
    tickets = await repo.list_all_tickets(db)
    text = render_admin_tickets_list(tickets, status_filter=None)
    await edit_or_answer(query, text, tickets_status_filter_kb(C.TICKETS_ALL))


@router.callback_query(F.data == C.TICKETS_ALL)
@router.callback_query(F.data == C.TICKETS_OPEN)
@router.callback_query(F.data == C.TICKETS_IN_PROGRESS)
@router.callback_query(F.data == C.TICKETS_CLOSED)
async def cb_tickets_filter(query: CallbackQuery, db: Database) -> None:
    """List tickets filtered by status."""
    await query.answer()
    status = _status_from_filter(query.data or "")
    tickets = await repo.list_all_tickets(db, status_filter=status)
    text = render_admin_tickets_list(tickets, status_filter=status)
    filter_cb = C.TICKETS_ALL if status is None else query.data or C.TICKETS_ALL
    await edit_or_answer(query, text, tickets_status_filter_kb(filter_cb))


# ---------------------------------------------------------------- detail


@router.callback_query(F.data.startswith(C.TICKETS_OPEN_TICKET))
async def cb_ticket_detail(query: CallbackQuery, db: Database) -> None:
    """View a single ticket's detail with action buttons."""
    await query.answer()
    ticket_id = _int_after(query.data or "", C.TICKETS_OPEN_TICKET)
    if ticket_id is None:
        await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_status_filter_kb(C.TICKETS_ALL))
        return
    ticket = await repo.get_ticket(db, ticket_id, owner_id=None)
    if ticket is None:
        await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_status_filter_kb(C.TICKETS_ALL))
        return
    messages = await repo.list_ticket_messages(db, ticket_id)
    text = render_ticket_detail(ticket, messages)
    await edit_or_answer(
        query, text, admin_ticket_detail_kb(ticket_id, ticket["status"])
    )


# ---------------------------------------------------------------- status change


@router.callback_query(F.data.startswith(C.TICKETS_STATUS))
async def cb_ticket_status(query: CallbackQuery, db: Database) -> None:
    """Status dropdown: with no `:status` suffix show the picker;
    with `ticket_id:status` suffix apply the new status."""
    await query.answer()
    remainder = (query.data or "").removeprefix(C.TICKETS_STATUS)
    if ":" in remainder:
        ticket_id_str, new_status = remainder.split(":", 1)
        ticket = await repo.get_ticket(db, int(ticket_id_str), owner_id=None)
        if ticket is None:
            await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_status_filter_kb(C.TICKETS_ALL))
            return
        await repo.update_ticket_status(db, int(ticket_id_str), new_status)
        await edit_or_answer(query, M_TICKET_STATUS_SET, admin_ticket_detail_kb(int(ticket_id_str), new_status))
    else:
        try:
            ticket_id = int(remainder)
        except ValueError:
            await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_status_filter_kb(C.TICKETS_ALL))
            return
        ticket = await repo.get_ticket(db, ticket_id, owner_id=None)
        if ticket is None:
            await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_status_filter_kb(C.TICKETS_ALL))
            return
        await edit_or_answer(
            query,
            "اختر الحالة الجديدة:",
            ticket_status_change_kb(ticket_id, ticket["status"]),
        )


# ---------------------------------------------------------------- priority


@router.callback_query(F.data.startswith(C.TICKETS_PRIORITY))
async def cb_ticket_priority(query: CallbackQuery, db: Database) -> None:
    """Set ticket priority: callback data is `adm:tickets:priority:ticket_id:priority`."""
    await query.answer()
    remainder = (query.data or "").removeprefix(C.TICKETS_PRIORITY)
    parts = remainder.split(":")
    if len(parts) < 2:
        await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_status_filter_kb(C.TICKETS_ALL))
        return
    ticket_id = int(parts[0])
    priority = parts[1]
    ticket = await repo.get_ticket(db, ticket_id, owner_id=None)
    if ticket is None:
        await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_status_filter_kb(C.TICKETS_ALL))
        return
    await repo.update_ticket_priority(db, ticket_id, priority)
    await edit_or_answer(query, M_TICKET_PRIORITY_SET, admin_ticket_detail_kb(ticket_id, ticket["status"]))


# ---------------------------------------------------------------- reply to user


@router.callback_query(F.data.startswith(C.TICKETS_REPLY))
async def cb_ticket_reply_start(query: CallbackQuery, db: Database, state: FSMContext) -> None:
    """Admin presses 'reply to user': ask for the message body."""
    await query.answer()
    ticket_id = _int_after(query.data or "", C.TICKETS_REPLY)
    if ticket_id is None:
        await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_status_filter_kb(C.TICKETS_ALL))
        return
    ticket = await repo.get_ticket(db, ticket_id, owner_id=None)
    if ticket is None:
        await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_status_filter_kb(C.TICKETS_ALL))
        return
    await state.set_state(AdminTicketFSM.reply_body)
    await state.update_data(current_ticket_id=ticket_id)
    await edit_or_answer(
        query,
        f"↢ أرسل نص الرد على التذكرة <code>#{ticket_id}</code>:",
        admin_ticket_detail_kb(ticket_id, ticket["status"]),
    )


@router.message(AdminTicketFSM.reply_body)
async def admin_reply_body_entered(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    """Admin entered the reply body: create the message and DM the ticket owner."""
    data = await state.get_data()
    ticket_id = data.get("current_ticket_id", 0)
    body = (message.text or "").strip()
    if not body:
        await message.answer(
            f"↢ أرسل نص الرد على التذكرة <code>#{ticket_id}</code>:",
            parse_mode=PARSE_MODE,
        )
        return
    admin_id = message.from_user.id if message.from_user else 0
    await repo.create_ticket_message(
        db, ticket_id=ticket_id,
        sender_id=admin_id, sender_role="admin", body=body,
    )
    ticket = await repo.get_ticket(db, ticket_id, owner_id=None)
    if ticket is not None:
        try:
            await bot.send_message(
                chat_id=ticket["owner_id"],
                text=f"📨 رد من الإدارة على تذكرتك <code>#{ticket_id}</code>:\n{esc(body)}",
                parse_mode=PARSE_MODE,
            )
        except Exception:  # pragma: no cover - DM may fail if user blocked the bot
            logger.warning("Could not DM user %s about ticket #%d", ticket["owner_id"], ticket_id)
    await state.clear()
    await message.answer(
        "✅ تم إرسال الرد وتواصل الإدارة مع صاحب التذكرة.",
        parse_mode=PARSE_MODE,
    )


# ---------------------------------------------------------------- close


@router.callback_query(F.data.startswith(C.TICKETS_CLOSE))
async def cb_ticket_close(query: CallbackQuery, db: Database) -> None:
    """Close a ticket: set status='closed'."""
    await query.answer()
    ticket_id = _int_after(query.data or "", C.TICKETS_CLOSE)
    if ticket_id is None:
        await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_status_filter_kb(C.TICKETS_ALL))
        return
    await repo.update_ticket_status(db, ticket_id, "closed")
    await edit_or_answer(query, M_TICKET_CLOSED, tickets_status_filter_kb(C.TICKETS_ALL))
