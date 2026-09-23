"""Composition root: wires every layer into a running application (PRD §18).

Wiring only — no business logic lives here (RULES §1). Startup order follows
PRD §18: logging, data dir, DB (migrations), master key, client pool, event
bus, login flows, services, reporter, bot polling. Shutdown reverses it in a
``finally`` so a failure at any startup step still closes what opened.
"""

from __future__ import annotations

import asyncio
import logging
import stat

from aiogram import Bot

from app.bot import build_dispatcher
from app.bot.reporter import Reporter
from app.config import Config
from app.core.account_service import AccountService
from app.core.broadcast import Broadcaster
from app.core.backup_manager import BackupManager
from app.core.events import EventBus
from app.core.job_manager import JobManager
from app.core.notifications import NotificationService
from app.core.settings import UserSettings
from app.db.database import Database
from app.logging_setup import setup_logging
from app.security.crypto import SessionCrypto, load_or_create_key
from app.tg.client_pool import ClientPool
from app.tg.login import LoginFlowManager
from app.tg.transfer import TransferEngine

logger = logging.getLogger("app.main")


async def run(config: Config) -> None:
    """Run the bot until SIGINT/SIGTERM (or a fatal startup error)."""
    setup_logging(config.log_level)

    config.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        config.data_dir.chmod(stat.S_IRWXU)  # 0700, best effort
    except OSError:
        logger.warning("Could not tighten permissions on %s", config.data_dir)

    db = Database(config.db_path)
    pool: ClientPool | None = None
    logins: LoginFlowManager | None = None
    jobs: JobManager | None = None
    bot: Bot | None = None
    bus: EventBus | None = None
    broadcaster: Broadcaster | None = None
    notifier: NotificationService | None = None
    backups: BackupManager | None = None
    try:
        await db.connect()  # migrations run here

        key = load_or_create_key(config.sessions_master_key, config.key_file)
        crypto = SessionCrypto(key)

        pool = ClientPool(config.api_id, config.api_hash)
        bus = EventBus()

        logins = LoginFlowManager(
            config.api_id,
            config.api_hash,
            ttl_seconds=config.login_ttl_seconds,
        )
        logins.start_sweeper()

        accounts = AccountService(db, crypto, pool, bus=bus)
        engine = TransferEngine()
        user_settings = UserSettings(config)
        jobs = JobManager(db, pool, crypto, bus, config, engine, settings=user_settings)
        recovered = await jobs.recover()  # before any new job can start (RULES §5)
        if recovered:
            logger.info("boot recovery: %d interrupted job(s) marked", recovered)

        bot = Bot(token=config.bot_token)
        reporter = Reporter(bot, bus, config)
        reporter.subscribe()

        broadcaster = Broadcaster(db, config, bus)
        await broadcaster.recover(bot)
        broadcaster.start_sweeper(bot)

        notifier = NotificationService(db, config, bus)
        notifier.set_bot(bot)
        notifier.subscribe()

        backups = BackupManager(db, config, bus, config.data_dir)
        await backups.recover()  # resume interrupted backups before dispatch
        backups.start_sweeper()

        dispatcher = build_dispatcher(
            config=config,
            db=db,
            accounts=accounts,
            jobs=jobs,
            logins=logins,
            bus=bus,
            pool=pool,
            reporter=reporter,
            settings=user_settings,
            broadcaster=broadcaster,
            notifications=notifier,
            backups=backups,
        )

        logger.info("bot polling starting")
        try:
            # handle_signals=True: aiogram stops polling gracefully on
            # SIGINT/SIGTERM; the session is closed by the finally below.
            await dispatcher.start_polling(bot, close_bot_session=False)
        except Exception as exc:
            # Expected startup failures (bad token, no network) must surface as
            # a readable log line, not a traceback.
            logger.error("Bot startup failed: %s", exc)
            raise SystemExit(1) from exc
    finally:
        # Reverse creation order; each step tolerates the others never existing.
        if broadcaster is not None:
            await broadcaster.stop_sweeper()
            await broadcaster.shutdown()
        if backups is not None:
            await backups.stop_sweeper()
            await backups.shutdown()
        if notifier is not None:
            notifier.unsubscribe()
        if bot is not None:
            await bot.session.close()
        if jobs is not None:
            await jobs.shutdown()
        if logins is not None:
            await logins.stop_sweeper()
        if pool is not None:
            await pool.close_all()
        await db.close()
        logger.info("shutdown complete")


__all__ = ["run"]
