"""Outer middleware: user upsert, block gate, private-chat gate (PRD A1).

Runs on every Update before any handler. Injects ``db`` into the handler data.
Non-private chats get an answer only for ``/start``; everything else in groups
is ignored. Callback data is user-controlled input, never authorization
(RULES §4) — handlers re-verify ownership via the services.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update

from app.bot.texts import M_BLOCKED, M_PRIVATE_ONLY, PARSE_MODE
from app.db import repositories as repo
from app.db.database import Database

logger = logging.getLogger(__name__)

__all__ = ["UserGateMiddleware"]


class UserGateMiddleware(BaseMiddleware):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        inner = event.event
        user = getattr(inner, "from_user", None)
        if user is None:
            return None  # channel posts / anonymous events: no user to gate

        await repo.upsert_user(
            self._db,
            user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
        )
        if await repo.is_user_blocked(self._db, user.id):
            await self._refuse(inner)
            return None

        chat = getattr(inner, "chat", None) or getattr(
            getattr(inner, "message", None), "chat", None
        )
        if chat is not None and chat.type != "private":
            # Groups: only /start is answered (with a redirect), rest is ignored.
            if event.event_type == "message" and (inner.text or "").startswith("/start"):
                await inner.answer(M_PRIVATE_ONLY, parse_mode=PARSE_MODE)
            return None

        data["db"] = self._db
        return await handler(event, data)

    @staticmethod
    async def _refuse(inner: Message | CallbackQuery) -> None:
        try:
            if isinstance(inner, CallbackQuery):
                await inner.answer(M_BLOCKED, show_alert=True)
            else:
                await inner.answer(M_BLOCKED, parse_mode=PARSE_MODE)
        except Exception:
            logger.warning("blocked-user refusal failed", exc_info=True)
