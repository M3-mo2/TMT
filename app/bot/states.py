"""FSM state groups for the two multi-step flows."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup

__all__ = ["AddAccountFSM", "TransferFSM", "SettingsFSM", "TicketFSM"]


class AddAccountFSM(StatesGroup):
    phone = State()
    code = State()
    password = State()


class TransferFSM(StatesGroup):
    account = State()
    source = State()
    dest = State()


class SettingsFSM(StatesGroup):
    value = State()


class TicketFSM(StatesGroup):
    create_subject = State()
    create_body = State()
    create_confirm = State()
    reply_body = State()
    reply_confirm = State()
