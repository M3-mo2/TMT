"""IsAdmin filter — checks the user's ID against ADMIN_IDS from .env."""
from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from app.config import Config


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery, config: Config) -> bool:
        user = event.from_user
        if user is None:
            return False
        return user.id in config.admin_id_list
