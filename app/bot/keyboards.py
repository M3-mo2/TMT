"""Inline keyboard builders. Max ~6 buttons per screen, short Arabic labels."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callbacks import AccountCB, JobCB, MenuCB, TransferCB
from app.bot.texts import (
    BUT_ACCOUNTS,
    BUT_ADD_ACCOUNT,
    BUT_BACK,
    BUT_CANCEL,
    BUT_CONFIRM_DELETE,
    BUT_DELETE,
    BUT_HELP,
    BUT_JOBS,
    BUT_MAIN,
    BUT_REFRESH,
    BUT_START_TRANSFER,
    BUT_TRANSFER,
)
from app.bot.texts import esc as _esc
from app.core.models import Job, JobStatus

__all__ = [
    "main_menu",
    "accounts_list",
    "account_card",
    "account_delete_confirm",
    "login_cancel",
    "wizard_account_picker",
    "wizard_cancel",
    "preflight_screen",
    "job_detail",
    "jobs_list",
    "jobs_list_back",
]

Row = list[InlineKeyboardButton]


def _btn(text: str, cb: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(BUT_ACCOUNTS, MenuCB(action="accounts"))],
            [_btn(BUT_TRANSFER, MenuCB(action="transfers"))],
            [_btn(BUT_JOBS, MenuCB(action="jobs"))],
            [_btn(BUT_HELP, MenuCB(action="help"))],
        ]
    )


def accounts_list(accounts: list) -> InlineKeyboardMarkup:
    rows: list[Row] = [
        [_btn(f"› {_esc(a.display_name)}", AccountCB(action="view", account_id=a.id))]
        for a in accounts
    ]
    rows.append([_btn(BUT_ADD_ACCOUNT, AccountCB(action="add"))])
    rows.append([_btn(BUT_MAIN, MenuCB(action="main"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_card(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(BUT_DELETE, AccountCB(action="delete_confirm", account_id=account_id))],
            [_btn(BUT_BACK, AccountCB(action="list"))],
        ]
    )


def account_delete_confirm(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(BUT_CONFIRM_DELETE, AccountCB(action="delete", account_id=account_id))],
            [_btn(BUT_BACK, AccountCB(action="view", account_id=account_id))],
        ]
    )


def login_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn(BUT_CANCEL, AccountCB(action="cancel_login"))]]
    )


def wizard_account_picker(accounts: list) -> InlineKeyboardMarkup:
    rows: list[Row] = [
        [_btn(f"› {_esc(a.display_name)}", TransferCB(action="pick_account", account_id=a.id))]
        for a in accounts
    ]
    rows.append([_btn(BUT_CANCEL, TransferCB(action="cancel_wizard"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wizard_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn(BUT_CANCEL, TransferCB(action="cancel_wizard"))]]
    )


def preflight_screen(has_fail: bool, account_id: int) -> InlineKeyboardMarkup:
    """Confirm only when preflight reported no FAIL; refresh re-runs the check."""
    rows: list[Row] = []
    if not has_fail:
        rows.append(
            [_btn(BUT_START_TRANSFER, TransferCB(action="confirm", account_id=account_id))]
        )
    rows.append([_btn(BUT_REFRESH, TransferCB(action="refresh", account_id=account_id))])
    rows.append([_btn(BUT_CANCEL, TransferCB(action="cancel_wizard"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def job_detail(job: Job) -> InlineKeyboardMarkup:
    rows: list[Row] = []
    if job.status in (JobStatus.CREATED, JobStatus.VALIDATING, JobStatus.QUEUED, JobStatus.RUNNING):
        rows.append([_btn(BUT_CANCEL, JobCB(action="cancel", job_id=job.id))])
    rows.append([_btn(BUT_REFRESH, JobCB(action="view", job_id=job.id))])
    rows.append([_btn(BUT_BACK, JobCB(action="list"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def jobs_list(jobs: list) -> InlineKeyboardMarkup:
    rows: list[Row] = []
    for job in jobs:
        rows.append(
            [_btn(f"› #{job.id}", JobCB(action="view", job_id=job.id))]
        )
    rows.append([_btn(BUT_BACK, MenuCB(action="main"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def jobs_list_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_btn(BUT_BACK, MenuCB(action="main"))]]
    )
