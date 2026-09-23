"""Ticket flow: list, create, view, reply. Ownership is enforced by the
repositories (RULES §4)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import MenuCB, TicketCB
from app.bot.keyboards import (
    ticket_confirm_create_kb,
    ticket_confirm_reply_kb,
    ticket_create_kb,
    ticket_detail_kb,
    tickets_list_kb,
)
from app.bot.routers.common import edit_or_answer
from app.bot.states import TicketFSM
from app.bot.texts import (
    M_TICKETS_EMPTY,
    M_TICKET_CREATE_BODY,
    M_TICKET_CREATE_SUBJECT,
    M_TICKET_NOT_FOUND,
    M_TICKET_REPLY_BODY,
    M_TICKET_REPLIED,
    PARSE_MODE,
    render_ticket_create_confirm,
    render_ticket_detail,
    render_ticket_created,
    render_ticket_reply_confirm,
    render_tickets_list,
)
from app.db import repositories as repo
from app.db.database import Database

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = Router(name="tickets")


# ---------------------------------------------------------------- helpers


async def _show_ticket_detail(
    query_or_msg,
    db: Database,
    ticket_id: int,
    owner_id: int,
    state: FSMContext | None = None,
) -> None:
    """Fetch and render a ticket + its thread."""
    ticket = await repo.get_ticket(db, ticket_id, owner_id=owner_id)
    if ticket is None:
        await edit_or_answer(query_or_msg, M_TICKET_NOT_FOUND, tickets_list_kb([]))
        return
    messages = await repo.list_ticket_messages(db, ticket_id)
    text = render_ticket_detail(ticket, messages)
    await edit_or_answer(query_or_msg, text, ticket_detail_kb(ticket_id))
    if state is not None:
        await state.update_data(current_ticket_id=ticket_id)


async def _show_tickets_list(
    query_or_msg, db: Database, owner_id: int
) -> None:
    """Show the user's open/in_progress tickets (closed/resolved hidden)."""
    all_tickets = await repo.list_user_tickets(db, owner_id)
    tickets = [t for t in all_tickets if t["status"] in ("open", "in_progress")]
    if not tickets:
        await edit_or_answer(query_or_msg, M_TICKETS_EMPTY, tickets_list_kb([]))
    else:
        await edit_or_answer(
            query_or_msg, render_tickets_list(tickets), tickets_list_kb(tickets)
        )


# ---------------------------------------------------------------- callback queries


@router.callback_query(MenuCB.filter(F.action == "tickets"))
async def show_tickets(query: CallbackQuery, db: Database, state: FSMContext) -> None:
    """Main-menu → ticket list."""
    await query.answer()
    owner_id = query.from_user.id  # type: ignore[union-attr]
    await state.clear()
    await _show_tickets_list(query, db, owner_id)


@router.callback_query(TicketCB.filter(F.action == "view"))
async def view_ticket(
    query: CallbackQuery, callback_data: TicketCB, db: Database, state: FSMContext
) -> None:
    """Open a ticket's detail view."""
    await query.answer()
    owner_id = query.from_user.id  # type: ignore[union-attr]
    await _show_ticket_detail(query, db, callback_data.ticket_id, owner_id, state)


@router.callback_query(TicketCB.filter(F.action == "back"))
async def back_to_list(
    query: CallbackQuery, callback_data: TicketCB, db: Database, state: FSMContext
) -> None:
    """Return from ticket detail to the list (also used to cancel flows)."""
    await query.answer()
    owner_id = query.from_user.id  # type: ignore[union-attr]
    await state.clear()
    await _show_tickets_list(query, db, owner_id)


# ---------------------------------------------------------------- create flow


@router.callback_query(TicketCB.filter(F.action == "create"))
async def create_ticket(query: CallbackQuery, state: FSMContext) -> None:
    """Start the create-ticket wizard: ask for the subject."""
    await query.answer()
    await state.clear()
    await state.set_state(TicketFSM.create_subject)
    await edit_or_answer(query, M_TICKET_CREATE_SUBJECT, ticket_create_kb())


@router.message(TicketFSM.create_subject)
async def create_subject_entered(message: Message, state: FSMContext) -> None:
    subject = (message.text or "").strip()
    if not subject:
        await message.answer(M_TICKET_CREATE_SUBJECT, parse_mode=PARSE_MODE)
        return
    await state.update_data(subject=subject)
    await state.set_state(TicketFSM.create_body)
    await message.answer(M_TICKET_CREATE_BODY, parse_mode=PARSE_MODE)


@router.message(TicketFSM.create_body)
async def create_body_entered(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()
    if not body:
        await message.answer(M_TICKET_CREATE_BODY, parse_mode=PARSE_MODE)
        return
    await state.update_data(body=body)
    await state.set_state(TicketFSM.create_confirm)
    data = await state.get_data()
    text = render_ticket_create_confirm(data.get("subject", ""), "normal", body)
    await message.answer(
        text, parse_mode=PARSE_MODE, reply_markup=ticket_confirm_create_kb()
    )


@router.callback_query(TicketCB.filter(F.action == "confirm_create"))
async def confirm_create(query: CallbackQuery, state: FSMContext, db: Database) -> None:
    owner_id = query.from_user.id  # type: ignore[union-attr]
    data = await state.get_data()
    subject: str = data.get("subject", "")
    body: str = data.get("body", "")
    if not subject or not body:
        await edit_or_answer(query, M_TICKET_CREATE_SUBJECT, ticket_create_kb())
        return
    ticket_id = await repo.create_ticket(db, owner_id=owner_id, subject=subject)
    await repo.create_ticket_message(
        db, ticket_id=ticket_id,
        sender_id=owner_id, sender_role="user", body=body,
    )
    await state.clear()
    tickets = await repo.list_user_tickets(db, owner_id)
    success = render_ticket_created(ticket_id, "open", "normal")
    if tickets:
        await edit_or_answer(
            query,
            f"{success}\n\n{render_tickets_list(tickets)}",
            tickets_list_kb(tickets),
        )
    else:
        await edit_or_answer(query, success, tickets_list_kb([]))


# ---------------------------------------------------------------- reply flow


@router.callback_query(TicketCB.filter(F.action == "reply"))
async def reply_to_ticket(
    query: CallbackQuery, callback_data: TicketCB, state: FSMContext, db: Database
) -> None:
    """Inline '↢ تذاكري' button: start the reply flow."""
    await query.answer()
    owner_id = query.from_user.id  # type: ignore[union-attr]
    ticket = await repo.get_ticket(db, callback_data.ticket_id, owner_id=owner_id)
    if ticket is None:
        await edit_or_answer(query, M_TICKET_NOT_FOUND, tickets_list_kb([]))
        return
    await state.update_data(current_ticket_id=callback_data.ticket_id)
    await state.set_state(TicketFSM.reply_body)
    await edit_or_answer(
        query,
        M_TICKET_REPLY_BODY.format(ticket_id=callback_data.ticket_id),
        ticket_create_kb(),
    )


@router.message(TicketFSM.reply_body)
async def reply_body_entered(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()
    if not body:
        data = await state.get_data()
        ticket_id = data.get("current_ticket_id", 0)
        await message.answer(
            M_TICKET_REPLY_BODY.format(ticket_id=ticket_id),
            parse_mode=PARSE_MODE,
        )
        return
    await state.update_data(reply_body=body)
    await state.set_state(TicketFSM.reply_confirm)
    data = await state.get_data()
    ticket_id = data.get("current_ticket_id", 0)
    text = render_ticket_reply_confirm(body)
    await message.answer(
        text, parse_mode=PARSE_MODE,
        reply_markup=ticket_confirm_reply_kb(ticket_id),
    )


@router.callback_query(TicketCB.filter(F.action == "confirm_reply"))
async def confirm_reply(
    query: CallbackQuery, callback_data: TicketCB, state: FSMContext, db: Database
) -> None:
    await query.answer()
    owner_id = query.from_user.id  # type: ignore[union-attr]
    data = await state.get_data()
    body: str = data.get("reply_body", "")
    if not body:
        await edit_or_answer(
            query,
            M_TICKET_REPLY_BODY.format(ticket_id=callback_data.ticket_id),
            ticket_create_kb(),
        )
        return
    await repo.create_ticket_message(
        db, ticket_id=callback_data.ticket_id,
        sender_id=owner_id, sender_role="user", body=body,
    )
    await state.clear()
    await query.message.answer(M_TICKET_REPLIED, parse_mode=PARSE_MODE)  # type: ignore[union-attr]
    await _show_ticket_detail(
        query.message, db, callback_data.ticket_id, owner_id
    )
