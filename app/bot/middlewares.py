"""Outer middleware: user upsert, block gate, mandatory-subscription gate,
private-chat gate (PRD A1).

Runs on every Update before any handler. Injects ``db`` into the handler data.
Non-private chats get an answer only for ``/start``; everything else in groups
is ignored. Callback data is user-controlled input, never authorization
(RULES §4) — handlers re-verify ownership via the services.

When ``bus`` is provided, the middleware publishes a
:class:`~app.core.events.SystemEvent` of type ``user_joined`` whenever a user
who was not previously in the database sends their first update (see
docs/notifications/NotificationSystem.md §4.1).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update

from app.bot.gate import check_membership
from app.bot.keyboards import gate_kb
from app.bot.texts import (
    M_BLOCKED,
    M_GATE_BLOCKED_ALERT,
    M_GATE_PLEASE_VERIFY,
    M_PRIVATE_ONLY,
    PARSE_MODE,
    esc,
    render_gate_screen,
)
from app.core.events import SystemEvent
from app.db import repositories as repo
from app.db.database import Database

logger = logging.getLogger(__name__)

__all__ = ["UserGateMiddleware"]


class UserGateMiddleware(BaseMiddleware):
    def __init__(self, db: Database, bus: Any | None = None) -> None:
        self._db = db
        self._bus = bus
        self._gate_shown: set[int] = set()

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

        is_new = await repo.upsert_user(
            self._db,
            user.id,
            first_name=user.first_name,
            last_name=user.last_name,
            username=user.username,
        )
        if is_new and self._bus is not None:
            await self._bus.publish(
                SystemEvent(
                    event_type="user_joined",
                    severity="info",
                    title="👤|مستخدم جديد",
                    body=self._render_join_body(user),
                    data={"user_id": user.id, "username": user.username or ""},
                )
            )
        if await repo.is_user_blocked(self._db, user.id):
            await self._refuse(inner)
            return None

        # Allow the gate-verify callback to reach the handler even before the
        # gate is cleared (so the user can prove membership).
        if isinstance(inner, CallbackQuery) and inner.data == "gate:verify":
            data["db"] = self._db
            return await handler(event, data)

        # Mandatory subscription gate
        if not await repo.is_gate_cleared(self._db, user.id):
            mandatory = await repo.active_channels(self._db)
            if mandatory:
                bot = data.get("bot")
                if bot is not None:
                    all_joined = await check_membership(bot, user.id, mandatory)
                    if all_joined:
                        await repo.set_gate_cleared(self._db, user.id)
                        self._gate_shown.discard(user.id)
                    else:
                        await self._show_gate(inner, bot, mandatory, user.id)
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
    def _render_join_body(user: Any) -> str:
        """Render the Arabic body text for a ``user_joined`` system event.

        User-controlled values (name, username) are escaped via ``texts.esc``
        before interpolation (RULES §7 — no raw user input in messages)."""
        display = user.full_name if hasattr(user, "full_name") and user.full_name \
            else " ".join(p for p in (user.first_name, getattr(user, "last_name", None)) if p)
        if not display:
            display = "مستخدم"
        parts = [f"👤|الاسم ↼ <b>{esc(display)}</b>"]
        if user.username:
            parts.append(f"›|اسم المستخدم ↼ <code>@{esc(user.username)}</code>")
        parts.append(f"›|المعرف ↼ <code>{user.id}</code>")
        return "\n".join(parts)

    async def _show_gate(
        self,
        inner: Message | CallbackQuery,
        bot: Any,
        mandatory: list[dict[str, Any]],
        user_id: int,
    ) -> None:
        """Send (or remind) the mandatory-subscription gate screen."""
        if isinstance(inner, CallbackQuery):
            # Inline button tap while blocked — just alert, don't re-edit.
            await inner.answer(M_GATE_BLOCKED_ALERT, show_alert=True)
        else:
            # New message — show the full gate screen once per session.
            if user_id in self._gate_shown:
                await inner.answer(M_GATE_PLEASE_VERIFY, parse_mode=PARSE_MODE)
            else:
                text = render_gate_screen(mandatory)
                await inner.answer(text, parse_mode=PARSE_MODE, reply_markup=gate_kb(mandatory))
                self._gate_shown.add(user_id)

    @staticmethod
    async def _refuse(inner: Message | CallbackQuery) -> None:
        try:
            if isinstance(inner, CallbackQuery):
                await inner.answer(M_BLOCKED, show_alert=True)
            else:
                await inner.answer(M_BLOCKED, parse_mode=PARSE_MODE)
        except Exception:
            logger.warning("blocked-user refusal failed", exc_info=True)
