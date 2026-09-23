"""Admin Router — mounts all admin sub-routers with IsAdmin filter."""
from __future__ import annotations

from aiogram import Router

from app.bot.routers.admin.filters import IsAdmin
from app.bot.routers.admin import menu, stats, broadcast, users, search, channels, backups, settings, notifications, tickets

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

router.include_router(tickets.router)
router.include_router(menu.router)
router.include_router(stats.router)
router.include_router(broadcast.router)
router.include_router(users.router)
router.include_router(search.router)
router.include_router(channels.router)
router.include_router(backups.router)
router.include_router(settings.router)
router.include_router(notifications.router)
