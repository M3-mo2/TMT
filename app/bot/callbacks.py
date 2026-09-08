"""Callback-data factories for every inline button (aiogram 3 CallbackData).

Callback ids are user-controlled input — every handler must re-verify
ownership through the services (RULES §4); the factories only carry ids.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

__all__ = ["MenuCB", "AccountCB", "TransferCB", "JobCB", "SettingsCB", "GateCB"]


class MenuCB(CallbackData, prefix="menu"):
    action: str  # "main" | "accounts" | "transfers" | "help"


class AccountCB(CallbackData, prefix="acct"):
    action: str  # "list" | "add" | "cancel_login" | "view" | "delete_confirm" | "delete"
    account_id: int = 0


class TransferCB(CallbackData, prefix="xfer"):
    action: str  # "new" | "pick_account" | "cancel_wizard" | "confirm" | "refresh"
    account_id: int = 0


class JobCB(CallbackData, prefix="job"):
    action: str  # "list" | "view" | "cancel"
    job_id: int = 0


class SettingsCB(CallbackData, prefix="set"):
    action: str  # "main" | "change" | "reset"
    key: str = ""  # config key to change/reset


class GateCB(CallbackData, prefix="gate"):
    action: str  # "verify"
