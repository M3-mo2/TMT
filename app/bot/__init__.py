"""Bot layer (aiogram): composition entry point.

``build_dispatcher`` is the composition contract Task 6 wires: it assembles the
routers (FSM-filtered routers first, admin router, the catch-all common router
last), the user gate middleware, and injects every service as dispatcher
workflow data (available to handlers as named parameters). The bot never
imports telethon directly; Telegram-facing work goes through core services
and the tg modules passed in here.

Workflow data keys: ``config``, ``db``, ``accounts``, ``jobs``, ``logins``,
``pool``, ``reporter``, ``settings`` (UserSettings for transfer param overrides).

Admin access is determined by ``config.admin_ids`` — users whose Telegram id
is in that list bypass the IsAdmin filter on the admin router.
"""

from __future__ import annotations

from typing import Any

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.middlewares import UserGateMiddleware
from app.bot.routers import common, transfers
from app.bot.routers import accounts as accounts_router
from app.bot.routers import jobs as jobs_router
from app.bot.routers import settings as settings_router
from app.bot.routers.admin import router as admin_router
from app.config import Config
from app.core.account_service import AccountService
from app.core.broadcast import Broadcaster
from app.core.events import EventBus
from app.core.job_manager import JobManager
from app.core.settings import UserSettings
from app.db.database import Database
from app.tg.client_pool import ClientPool
from app.tg.login import LoginFlowManager

__all__ = ["build_dispatcher"]


def build_dispatcher(
    *,
    config: Config,
    db: Database,
    accounts: AccountService,
    jobs: JobManager,
    logins: LoginFlowManager,
    bus: EventBus,
    pool: ClientPool,
    reporter: Any | None = None,
    settings: UserSettings | None = None,
    broadcaster: Broadcaster | None = None,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["config"] = config
    dp["db"] = db
    dp["accounts"] = accounts
    dp["jobs"] = jobs
    dp["logins"] = logins
    dp["pool"] = pool
    dp["reporter"] = reporter
    dp["settings"] = settings
    dp["broadcaster"] = broadcaster

    dp.update.outer_middleware(UserGateMiddleware(db))

    # FSM-filtered routers first; the catch-all common router must be last.
    dp.include_router(accounts_router.router)
    dp.include_router(transfers.router)
    dp.include_router(jobs_router.router)
    dp.include_router(settings_router.router)
    dp.include_router(admin_router)
    dp.include_router(common.router)
    return dp
