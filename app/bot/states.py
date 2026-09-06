"""FSM state groups for the two multi-step flows."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup

__all__ = ["AddAccountFSM", "TransferFSM", "SettingsFSM"]


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
