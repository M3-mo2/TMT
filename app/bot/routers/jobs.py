"""Job screens: last 10 jobs, detail card, cancel. Ownership is enforced by
JobManager (get/cancel are owner-scoped); account names via AccountService."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.callbacks import JobCB, MenuCB
from app.bot.keyboards import job_detail, jobs_list, jobs_list_back, main_menu
from app.bot.routers.common import edit_or_answer
from app.bot.texts import (
    M_CANCELED,
    M_JOB_CANCELLED,
    M_JOB_NOT_CANCELLABLE,
    M_JOBS_EMPTY,
    M_NOT_FOUND,
    M_DELETED_ACCOUNT,
    PARSE_MODE,
    render_job_card,
    render_jobs_list,
)
from app.core.account_service import AccountService
from app.core.job_manager import JobManager
from app.core.models import JobStatus

logger = logging.getLogger(__name__)

__all__ = ["router"]

router = Router(name="jobs")


async def _account_name(accounts: AccountService, owner_id: int, account_id: int | None) -> str | None:
    if account_id is None:
        return None
    account = await accounts.get(owner_id, account_id)
    return account.display_name if account else None


@router.callback_query(MenuCB.filter(F.action == "jobs"))
@router.callback_query(JobCB.filter(F.action == "list"))
async def show_jobs(
    query: CallbackQuery, callback_data: MenuCB | JobCB, jobs: JobManager
) -> None:
    await query.answer()
    items = await jobs.list_jobs(query.from_user.id, limit=10)  # type: ignore[union-attr]
    if not items:
        await edit_or_answer(query, M_JOBS_EMPTY, jobs_list_back())
        return
    await edit_or_answer(query, render_jobs_list(items), jobs_list(items))


@router.callback_query(JobCB.filter(F.action == "view"))
async def view_job(
    query: CallbackQuery,
    callback_data: JobCB,
    jobs: JobManager,
    accounts: AccountService,
) -> None:
    await query.answer()
    owner_id = query.from_user.id  # type: ignore[union-attr]
    job = await jobs.get_job(owner_id, callback_data.job_id)
    if job is None:
        await edit_or_answer(query, M_NOT_FOUND, jobs_list_back())
        return
    name = await _account_name(accounts, owner_id, job.account_id)
    await edit_or_answer(query, render_job_card(job, name), job_detail(job))


@router.callback_query(JobCB.filter(F.action == "cancel"))
async def cancel_job(
    query: CallbackQuery,
    callback_data: JobCB,
    jobs: JobManager,
    accounts: AccountService,
) -> None:
    owner_id = query.from_user.id  # type: ignore[union-attr]
    cancelled = await jobs.cancel_job(owner_id, callback_data.job_id)
    await query.answer(M_JOB_CANCELLED if cancelled else M_JOB_NOT_CANCELLABLE)
    job = await jobs.get_job(owner_id, callback_data.job_id)
    if job is None:
        await edit_or_answer(query, M_NOT_FOUND, main_menu())
        return
    name = await _account_name(accounts, owner_id, job.account_id)
    note = M_CANCELED if job.status is JobStatus.CANCELLED else ""
    text = render_job_card(job, name)
    if note:
        text = f"{note}\n{text}"
    await edit_or_answer(query, text, job_detail(job))
